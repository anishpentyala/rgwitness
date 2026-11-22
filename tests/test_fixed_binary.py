"""Fixed-width binary values, with independent real altered-footer controls."""
import copy
import hashlib
import io
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from fixed_binary_cases import case
from test_dictionary import changed_bound
from rgwitness.audit import audit_bytes
from rgwitness.batch import audit_files
from rgwitness.report import json_report
from rgwitness.schema import describe_schema, valid_schema
from rgwitness.worker import audit_in_worker


def fixed_file(values, width=4, *, statistics=True, row_group_size=None,
               store_schema=True):
    output = io.BytesIO()
    pq.write_table(pa.table({'x': pa.array(values, type=pa.binary(width))}), output,
                   compression='NONE', write_statistics=statistics,
                   row_group_size=row_group_size, store_schema=store_schema)
    return output.getvalue()


def decoded(data):
    return pq.ParquetFile(io.BytesIO(data)).read().column(0).to_pylist()


class FixedBinaryTests(unittest.TestCase):
    def audit(self, data, **options):
        try:
            return audit_bytes(data, **options)
        except ValueError as exc:
            self.fail('admitted fixed-size binary should be audited: ' + str(exc))

    def pair(self, first, second):
        with patch('rgwitness.batch.read_input', side_effect=[first, second]):
            return audit_files(['first', 'second'], require_schema_match=True)

    def test_fixed_widths_and_unsigned_byte_order(self):
        for width in (1, 4, 16, 32, 257):
            values = [b'\xff' * width, b'\x00' * width, None, b'\x80' * width]
            with self.subTest(width=width):
                data = fixed_file(values, width)
                result = self.audit(data, include_schema=True)
                self.assertEqual(result['verdict'], 'CONSISTENT')
                self.assertEqual(decoded(data), values)
                column = result['columns'][0]
                self.assertEqual(column['observed'], {'rows': 4, 'nulls': 1,
                    'lower': b'\x00' * width, 'upper': b'\xff' * width})
                self.assertEqual(column['comparison'],
                    {'representation': 'unsigned_bytes', 'byte_width': width})
                self.assertEqual(result['schema']['fields'][0]['type'],
                    {'kind': 'fixed_size_binary', 'byte_width': width})
                self.assertTrue(valid_schema(result['schema']))

    def test_seeded_binary_extrema_oracle(self):
        randomizer = random.Random(62519)
        for index in range(200):
            width = randomizer.choice((1, 2, 4, 16, 32))
            values = [None if randomizer.randrange(5) == 0 else
                      bytes(randomizer.randrange(256) for _ in range(width))
                      for _ in range(randomizer.randrange(35))]
            present = [value for value in values if value is not None]
            with self.subTest(index=index, width=width):
                data = fixed_file(values, width)
                self.assertEqual(decoded(data), values)
                result = self.audit(data)
                self.assertEqual(result['verdict'], 'CONSISTENT' if values else 'INCOMPLETE')
                self.assertEqual(result['columns'][0]['observed'], {
                    'rows': len(values), 'nulls': values.count(None),
                    'lower': min(present, key=lambda value: int.from_bytes(value, 'big')) if present else None,
                    'upper': max(present, key=lambda value: int.from_bytes(value, 'big')) if present else None})

    def test_corrupt_complete_bounds_keep_rows_but_contradict(self):
        original = fixed_file([b'aaa0', b'zzz9', None])
        for old, new, violation in ((b'aaa0', b'mmm0', 'lower_excludes_value'),
                                    (b'zzz9', b'mmm0', 'upper_excludes_value')):
            with self.subTest(violation=violation):
                altered = changed_bound(original, old, new)
                self.assertEqual(decoded(altered), decoded(original))
                result = self.audit(altered)
                self.assertEqual(result['verdict'], 'CONTRADICTED')
                self.assertEqual(result['columns'][0]['violations'], [violation])
                self.assertNotEqual(result['input_snapshot']['sha256'],
                                    hashlib.sha256(original).hexdigest())

    def test_conservative_complete_bounds_are_not_corruption(self):
        data = fixed_file([b'aaa0', b'zzz9', None])
        data = changed_bound(data, b'aaa0', b'0000')
        data = changed_bound(data, b'zzz9', b'~~~~')
        self.assertEqual(self.audit(data)['verdict'], 'CONSISTENT')

    def test_static_independent_fixture_has_expected_rows(self):
        values = [b'\x00\x7f\x80\xff', b'\x7f\xff\x00\x80',
                  b'\x80\x00\xff\x7f', b'\xff\x80\x7f\x00', None]
        for name in ('known', 'absent_order', 'unknown_order', 'lower_only',
                     'upper_only', 'bad_lower_only', 'bad_upper_only', 'wrong_nulls'):
            with self.subTest(name=name):
                self.assertEqual(decoded(case(name)), values)
        self.assertEqual(self.audit(case('known'))['verdict'], 'CONSISTENT')

    def test_missing_and_unknown_orders_leave_explicit_gaps(self):
        for name in ('absent_order', 'unknown_order'):
            with self.subTest(name=name):
                result = self.audit(case(name), include_schema=True)
                self.assertEqual(result['verdict'], 'INCOMPLETE')
                self.assertEqual(result['checked_columns'], 0)
                self.assertEqual(result['unchecked'][0]['reason'],
                                 'unsupported_fixed_binary_order')
                self.assertEqual(result['schema']['fields'][0]['type']['byte_width'], 4)

    def test_partial_bounds_stay_incomplete_without_losing_contradictions(self):
        for name in ('lower_only', 'upper_only', 'bad_lower_only', 'bad_upper_only'):
            with self.subTest(name=name):
                result = self.audit(case(name))
                self.assertEqual(result['verdict'],
                                 'CONTRADICTED' if name.startswith('bad_') else 'INCOMPLETE')
                self.assertIn('bounds_missing', result['columns'][0]['unchecked'])
                if name.startswith('bad_'):
                    self.assertEqual(result['columns'][0]['violations'],
                        ['lower_excludes_value' if 'lower' in name else 'upper_excludes_value'])

    def test_partial_bound_with_no_values_is_never_a_pass(self):
        for name in ('null_lower_only', 'null_upper_only'):
            with self.subTest(name=name):
                data = case(name)
                self.assertEqual(decoded(data), [None, None])
                result = self.audit(data)
                self.assertEqual(result['verdict'], 'CONTRADICTED')
                self.assertIn('bounds_without_values', result['columns'][0]['violations'])
                self.assertIn('bounds_missing', result['columns'][0]['unchecked'])

    def test_mixed_column_positions_keep_float_and_fixed_evidence_separate(self):
        table = pa.table({'first': [1, None, 3],
                          'fixed': pa.array([b'zzzz', None, b'aaaa'], type=pa.binary(4)),
                          'float': [2.5, None, -4.5],
                          'last': pa.array([True, None, False])})
        output = io.BytesIO()
        pq.write_table(table, output)
        result = self.audit(output.getvalue(), include_schema=True)
        self.assertEqual(result['verdict'], 'INCOMPLETE')
        self.assertEqual(result['checked_columns'], 4)
        self.assertEqual(result['violation_count'], 0)
        by_name = {column['column']: column for column in result['columns']}
        self.assertEqual(by_name['fixed']['observed']['lower'], b'aaaa')
        self.assertEqual(by_name['first']['observed']['upper'], 3)
        self.assertEqual(by_name['float']['observed']['lower'], -4.5)
        self.assertIn('nan_count_missing', by_name['float']['unchecked'])
        self.assertIn('float NaN', result['scope'])

    def test_new_fixed_byte_admission_inherits_footer_budget(self):
        fixed = fixed_file([b'aaaa'])
        output = io.BytesIO()
        pq.write_table(pa.table({'x': [1]}), output)
        with patch('rgwitness.footer.MAX_FOOTER_BYTES', 8):
            with self.assertRaisesRegex(ValueError, 'footer size budget'):
                audit_bytes(fixed)
            self.assertEqual(audit_bytes(output.getvalue())['verdict'], 'CONSISTENT')

    def test_interval_annotation_is_unchecked_not_unsigned_binary(self):
        annotated = case('interval_repro')
        plain = case('interval_plain')
        self.assertEqual(hashlib.sha256(annotated).hexdigest(),
                         '24277b1540ea199a12c71ea8b3139be14e09c140f1db00d8cdd857b31c2c2e4c')
        self.assertEqual(decoded(annotated), decoded(plain))
        def pages(data):
            footer_size = int.from_bytes(data[-8:-4], 'little')
            return data[:-8-footer_size]
        self.assertEqual(pages(annotated), pages(plain))
        self.assertEqual(self.audit(plain)['verdict'], 'CONSISTENT')
        result = self.audit(annotated, include_schema=True)
        self.assertEqual(result['verdict'], 'INCOMPLETE')
        self.assertEqual(result['checked_columns'], 0)
        self.assertEqual(result['violation_count'], 0)
        self.assertEqual(result['unchecked'][0]['reason'],
                         'unsupported_fixed_binary_annotation')
        self.assertEqual(result['unchecked'][0]['annotation'],
                         {'converted_type': 21, 'logical_type_present': False})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'interval.parquet'
            path.write_bytes(annotated)
            self.assertEqual(audit_in_worker(path, include_schema=True), json_report(result))
        batch = self.pair(annotated, plain)
        self.assertEqual(batch['schema_check']['verdict'], 'CONSISTENT')
        self.assertEqual(batch['verdict'], 'INCOMPLETE')

    def test_any_converted_or_logical_annotation_leaves_a_gap(self):
        from rgwitness.footer import float_evidence
        for name in ('logical_empty', 'logical_unknown'):
            raw = float_evidence(case(name))['schema'][1]
            self.assertIsNone(raw['converted_type'])
            self.assertTrue(raw['logical_type_present'])
            result = self.audit(case(name))
            self.assertEqual(result['verdict'], 'INCOMPLETE')
            self.assertEqual(result['unchecked'][0]['reason'],
                             'unsupported_fixed_binary_annotation')
        data = fixed_file([b'aaaa', b'zzzz'])
        for change in ({'converted_type': 0}, {'converted_type': 21},
                       {'converted_type': -1}, {'logical_type_present': True},
                       {'converted_type': 21, 'logical_type_present': True}):
            def evidence(snapshot):
                value = float_evidence(snapshot)
                value['schema'][1].update(change)
                return value
            with self.subTest(change=change), patch('rgwitness.footer.float_evidence',
                                                    side_effect=evidence):
                result = self.audit(data)
                self.assertEqual(result['verdict'], 'INCOMPLETE')
                self.assertEqual(result['checked_columns'], 0)
                self.assertEqual(result['unchecked'][0]['reason'],
                                 'unsupported_fixed_binary_annotation')

    def test_physical_schema_is_bound_to_the_flat_decoded_field(self):
        from rgwitness.footer import float_evidence
        data = fixed_file([b'aaaa'])
        for change in ('missing', 'length', 'root_children', 'name',
                       'type_length', 'physical_type', 'num_children'):
            def evidence(snapshot):
                value = float_evidence(snapshot)
                if change == 'missing':
                    value['schema'] = None
                elif change == 'length':
                    value['schema'].pop()
                elif change == 'root_children':
                    value['schema'][0]['num_children'] = 2
                else:
                    value['schema'][1][change] = {'name': b'y', 'type_length': 5,
                        'physical_type': 6, 'num_children': 1}[change]
                return value
            with self.subTest(change=change), patch('rgwitness.footer.float_evidence',
                                                    side_effect=evidence):
                with self.assertRaises(ValueError):
                    audit_bytes(data)

    def test_wrong_null_count_is_not_silently_accepted(self):
        result = self.audit(case('wrong_nulls'))
        self.assertEqual(result['verdict'], 'CONTRADICTED')
        self.assertEqual(result['columns'][0]['violations'], ['null_count'])

    def test_bound_width_and_raw_alignment_are_validated(self):
        for name in ('bad_lower_width', 'bad_upper_width', 'wrong_num_values',
                     'wrong_path', 'wrong_physical'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    audit_bytes(case(name))

    def test_missing_stats_all_null_and_empty(self):
        for values, statistics, expected in (([b'aaaa', None], False, 'INCOMPLETE'),
                                             ([None, None], True, 'CONSISTENT'),
                                             ([], True, 'INCOMPLETE')):
            with self.subTest(values=values, statistics=statistics):
                result = self.audit(fixed_file(values, statistics=statistics))
                self.assertEqual(result['verdict'], expected)
                if not statistics:
                    self.assertIn('null_count_missing', result['columns'][0]['unchecked'])
                    self.assertIn('bounds_missing', result['columns'][0]['unchecked'])

    def test_multiple_row_groups_and_decoder_batches(self):
        values = [b'zzzz', b'aaaa', None] * 22000
        result = self.audit(fixed_file(values))
        self.assertEqual(result['verdict'], 'CONSISTENT')
        self.assertEqual(result['columns'][0]['observed']['rows'], 66000)
        result = self.audit(fixed_file(values[:7], row_group_size=3))
        self.assertEqual(result['row_groups'], 3)
        self.assertEqual(result['checked_columns'], 3)
        self.assertEqual(result['verdict'], 'CONSISTENT')

    def test_schema_distinguishes_width_and_variable_binary(self):
        first = fixed_file([b'aaaa'])
        other_width = fixed_file([b'aa'], 2)
        output = io.BytesIO()
        pq.write_table(pa.table({'x': pa.array([b'aaaa'], type=pa.binary())}), output)
        for second in (other_width, output.getvalue()):
            result = self.pair(first, second)
            self.assertEqual(result['verdict'], 'CONTRADICTED')
            difference = result['schema_check']['comparisons'][0]['differences'][0]
            self.assertEqual(difference['property'], 'type')
            self.assertEqual(difference['reference'],
                             {'kind': 'fixed_size_binary', 'byte_width': 4})
        self.assertEqual(self.pair(first, first)['schema_check']['verdict'], 'CONSISTENT')

    def test_schema_validator_rejects_invalid_widths(self):
        valid = describe_schema(pa.schema([('x', pa.binary(4))]))
        self.assertTrue(valid_schema(valid))
        for value in (0, -1, True, False, 4.0, '4', None, [], {}, 2**31):
            with self.subTest(value=value):
                invalid = copy.deepcopy(valid)
                invalid['fields'][0]['type']['byte_width'] = value
                self.assertFalse(valid_schema(invalid))
        invalid = copy.deepcopy(valid)
        invalid['fields'][0]['type']['extra'] = 1
        self.assertFalse(valid_schema(invalid))

    def test_stored_arrow_schema_is_not_required(self):
        data = fixed_file([b'aaaa', None, b'zzzz'], store_schema=False)
        result = self.audit(data, include_schema=True)
        self.assertEqual(result['verdict'], 'CONSISTENT')
        self.assertEqual(result['schema']['fields'][0]['type']['byte_width'], 4)

    def test_direct_worker_batch_and_cli_agree(self):
        data = fixed_file([b'\x00\x80\xff\x00', None, b'\xff\x00\x80\xff'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixed.parquet'
            path.write_bytes(data)
            expected = json_report(self.audit(data, include_schema=True))
            self.assertEqual(audit_in_worker(path, include_schema=True), expected)
            batch = audit_files([path, path], isolate=True, require_schema_match=True)
            self.assertEqual(batch['verdict'], 'CONSISTENT')
            self.assertEqual([entry['report'] for entry in batch['files']], [expected, expected])
            run = subprocess.run([sys.executable, '-m', 'rgwitness', '--include-schema', str(path)],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout), expected)
            self.assertEqual(expected['columns'][0]['observed']['lower'],
                             {'kind': 'bytes', 'hex': '0080ff00'})
            json.dumps(expected, allow_nan=False)

    def test_new_admission_does_not_include_uuid_extension_or_nested_values(self):
        for kind, values in ((pa.uuid(), [b'\x00' * 16]),
                             (pa.list_(pa.binary(4)), [[b'aaaa']])):
            with self.subTest(kind=kind):
                output = io.BytesIO()
                pq.write_table(pa.table({'x': pa.array(values, type=kind)}), output)
                with self.assertRaises(ValueError):
                    audit_bytes(output.getvalue())


if __name__ == '__main__':
    unittest.main()
