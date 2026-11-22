"""Real categorical Parquet files; compare decoded values, not category codes."""
import copy
import hashlib
import io
import json
import random
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from footer_fixture import wrong_null_count
from rgwitness.audit import audit_bytes
from rgwitness.batch import audit_files
from rgwitness.schema import describe_schema, valid_schema
from rgwitness.report import json_report
from rgwitness.worker import audit_in_worker


def dictionary_file(*, binary=False, index=pa.int8(), ordered=False,
                    codes=None, categories=None, statistics=True,
                    row_group_size=None, store_schema=True, large=False):
    if categories is None:
        categories = ['zzz9', 'aaa0', 'unused-middle']
    if binary:
        categories = [v.encode() if isinstance(v, str) else v for v in categories]
    value_type = ((pa.large_binary() if large else pa.binary()) if binary else
                  (pa.large_string() if large else pa.string()))
    if codes is None:
        codes = [0, 1, None, 0]
    array = pa.DictionaryArray.from_arrays(pa.array(codes, type=index),
            pa.array(categories, type=value_type), ordered=ordered)
    output = io.BytesIO()
    pq.write_table(pa.table({'x': array}), output, compression='NONE',
                   write_statistics=statistics, row_group_size=row_group_size,
                   store_schema=store_schema)
    return output.getvalue()


def decoded(data):
    return pq.ParquetFile(io.BytesIO(data)).read().column(0).to_pylist()


def changed_bound(data, old=b'aaa0', new=b'mmm0'):
    # This writer emits one modern bound, located only within the footer.
    size = int.from_bytes(data[-8:-4], 'little')
    start = len(data) - 8 - size
    footer = data[start:-8]
    assert len(old) == len(new) and footer.count(old) == 1
    return data[:start] + footer.replace(old, new) + data[-8:]


class DictionaryTests(unittest.TestCase):
    def audit(self, data, **options):
        try:
            return audit_bytes(data, **options)
        except ValueError as exc:
            self.fail('flat byte-array dictionary should be admitted: ' + str(exc))

    def pair(self, first, second):
        with patch('rgwitness.batch.read_input', side_effect=[first, second]):
            return audit_files(['first', 'second'], require_schema_match=True)

    def test_string_and_binary_use_values_not_codes_or_unused_categories(self):
        for binary in (False, True):
            with self.subTest(binary=binary):
                data = dictionary_file(binary=binary)
                report = self.audit(data)
                self.assertEqual(report['verdict'], 'CONSISTENT')
                observed = report['columns'][0]['observed']
                self.assertEqual(observed, {'rows': 4, 'nulls': 1,
                    'lower': b'aaa0' if binary else 'aaa0',
                    'upper': b'zzz9' if binary else 'zzz9'})
                self.assertEqual(decoded(data),
                    [b'zzz9', b'aaa0', None, b'zzz9'] if binary else
                    ['zzz9', 'aaa0', None, 'zzz9'])

    def test_seeded_decoded_extrema_against_python_value_oracle(self):
        randomizer = random.Random(41091)
        vocabulary = ['', 'a', 'zzz9', '\u03bb', '\U0001f642', '\u0000tail', 'unused']
        for case in range(200):
            categories = randomizer.choices(vocabulary, k=randomizer.randrange(1, 9))
            codes = randomizer.choices([None] + list(range(len(categories))),
                                      k=randomizer.randrange(33))
            binary = bool(case % 2)
            original = [None if code is None else categories[code] for code in codes]
            if binary:
                original = [None if value is None else value.encode() for value in original]
            non_null = [value for value in original if value is not None]
            expected = {'rows': len(original), 'nulls': original.count(None),
                        'lower': min(non_null) if non_null else None,
                        'upper': max(non_null) if non_null else None}
            data = dictionary_file(categories=categories, codes=codes, binary=binary,
                                   ordered=bool(case % 3))
            with self.subTest(case=case):
                self.assertEqual(decoded(data), original)
                report = self.audit(data)
                self.assertEqual(report['columns'][0]['observed'], expected)
                self.assertEqual(report['verdict'], 'CONSISTENT' if codes else 'INCOMPLETE')

    def test_ordered_categories_do_not_change_parquet_value_order(self):
        data = dictionary_file(ordered=True)
        report = self.audit(data, include_schema=True)
        self.assertEqual(report['columns'][0]['observed']['lower'], 'aaa0')
        self.assertEqual(report['verdict'], 'CONSISTENT')
        self.assertTrue(report['schema']['fields'][0]['type']['ordered'])

    def test_signed_and_unsigned_index_widths_preserved_in_descriptor(self):
        for signed in (True, False):
            for bits in (8, 16, 32, 64):
                index = getattr(pa, ('int' if signed else 'uint') + str(bits))()
                with self.subTest(index=index):
                    report = self.audit(dictionary_file(index=index), include_schema=True)
                    self.assertEqual(report['schema']['fields'][0]['type'], {
                        'kind': 'dictionary',
                        'index_type': {'kind': 'integer', 'bits': bits, 'signed': signed},
                        'value_type': {'kind': 'string'}, 'ordered': False})
                    self.assertTrue(valid_schema(report['schema']))

    def test_real_corrupt_bounds_preserve_rows_and_are_contradicted(self):
        for binary in (False, True):
            original = dictionary_file(binary=binary)
            for old, new, violation in ((b'aaa0', b'mmm0', 'lower_excludes_value'),
                                        (b'zzz9', b'mmm0', 'upper_excludes_value')):
                altered = changed_bound(original, old, new)
                with self.subTest(binary=binary, violation=violation):
                    self.assertEqual(decoded(altered), decoded(original))
                    report = self.audit(altered)
                    self.assertEqual(report['verdict'], 'CONTRADICTED')
                    self.assertEqual(report['columns'][0]['violations'], [violation])

    def test_wrong_null_count_does_not_become_a_pass(self):
        original = dictionary_file()
        altered = wrong_null_count(original, 0)
        self.assertEqual(decoded(altered), decoded(original))
        report = self.audit(altered)
        self.assertEqual(report['verdict'], 'CONTRADICTED')
        self.assertIn('null_count', report['columns'][0]['violations'])

    def test_missing_stats_remains_incomplete(self):
        report = self.audit(dictionary_file(statistics=False))
        self.assertEqual(report['verdict'], 'INCOMPLETE')
        self.assertIn('bounds_missing', report['columns'][0]['unchecked'])
        self.assertIn('null_count_missing', report['columns'][0]['unchecked'])

    def test_all_null_and_empty_files(self):
        nulls = self.audit(dictionary_file(codes=[None, None], categories=[]))
        self.assertEqual(nulls['verdict'], 'CONSISTENT')
        self.assertEqual(nulls['columns'][0]['observed']['nulls'], 2)
        empty = self.audit(dictionary_file(codes=[], categories=[]))
        self.assertEqual(empty['rows'], 0)
        self.assertEqual(empty['verdict'], 'INCOMPLETE')

    def test_multiple_groups_and_decode_batches(self):
        for size in (30000, 100000):
            report = self.audit(dictionary_file(codes=[0, 1, None] * 22000,
                                                row_group_size=size))
            self.assertEqual(report['rows'], 66000)
            self.assertEqual(report['verdict'], 'CONSISTENT')
            self.assertEqual(sum(c['observed']['rows'] for c in report['columns']), 66000)

    def test_plain_decoding_without_stored_arrow_schema_is_unchanged(self):
        data = dictionary_file(store_schema=False)
        self.assertTrue(pa.types.is_string(pq.ParquetFile(io.BytesIO(data)).schema_arrow[0].type))
        self.assertEqual(audit_bytes(data)['verdict'], 'CONSISTENT')

    def test_large_dictionary_values_describe_decoder_normalization(self):
        for binary in (False, True):
            data = dictionary_file(binary=binary, large=True)
            schema = pq.ParquetFile(io.BytesIO(data)).schema_arrow
            report = self.audit(data, include_schema=True)
            self.assertEqual(report['schema'], describe_schema(schema))
            self.assertEqual(report['schema']['fields'][0]['type']['value_type'],
                             {'kind': 'binary' if binary else 'string'})

    def test_exact_schema_type_differences_not_silently_coerced(self):
        original = dictionary_file()
        variants = [dictionary_file(index=pa.int16()), dictionary_file(index=pa.uint8()),
                    dictionary_file(binary=True), dictionary_file(ordered=True),
                    dictionary_file(store_schema=False)]
        for changed in variants:
            with self.subTest(changed=hashlib.sha256(changed).hexdigest()):
                self.assertFalse(pq.ParquetFile(io.BytesIO(original)).schema_arrow.equals(
                    pq.ParquetFile(io.BytesIO(changed)).schema_arrow))
                report = self.pair(original, changed)
                self.assertEqual(report['verdict'], 'CONTRADICTED')
                self.assertEqual(report['schema_check']['verdict'], 'CONTRADICTED')
                self.assertEqual(report['verdict_counts']['CONTRADICTED'], 0)

    def test_equal_types_do_not_claim_equal_dictionary_contents(self):
        first = dictionary_file()
        second = dictionary_file(categories=['other', 'values', 'UNUSED_SECRET'])
        report = self.pair(first, second)
        self.assertEqual(report['verdict'], 'CONSISTENT')
        self.assertNotIn('UNUSED_SECRET', json.dumps(report['files'][1]['report']['schema']))
        self.assertNotEqual(decoded(first), decoded(second))

    def test_worker_and_batch_match_direct_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'categorical.parquet'
            data = dictionary_file(binary=True, ordered=True, index=pa.uint16())
            path.write_bytes(data)
            direct = self.audit(data, include_schema=True)
            self.assertEqual(audit_in_worker(path, include_schema=True),
                             json_report(direct))
            batch = audit_files([path, path], isolate=True, require_schema_match=True)
            self.assertEqual(batch['verdict'], 'CONSISTENT')
            self.assertEqual(batch['files'][0]['report']['input_snapshot']['sha256'],
                             hashlib.sha256(data).hexdigest())

    def test_worker_rejects_malformed_dictionary_descriptors(self):
        good = self.audit(dictionary_file(), include_schema=True)
        good = json_report(good)
        changes = [('ordered', 1), ('index_type', {'kind': 'integer', 'bits': True, 'signed': True}),
                   ('index_type', {'kind': 'integer', 'bits': 8, 'signed': 'yes'}),
                   ('value_type', {'kind': 'dictionary'}), ('value_type', {'kind': 'float', 'bits': 64}),
                   ('value_type', {'kind': 'string', 'extra': 1})]
        for key, value in changes:
            altered = copy.deepcopy(good)
            altered['schema']['fields'][0]['type'][key] = value
            self.assertFalse(valid_schema(altered['schema']))
            result = subprocess.CompletedProcess([], 0, json.dumps(altered).encode(), b'')
            with patch('rgwitness.worker.subprocess.run', return_value=result):
                self.assertEqual(audit_in_worker('unused', include_schema=True)['verdict'], 'INVALID')

    def test_numeric_dictionary_producers_keep_existing_scalar_paths(self):
        for kind, values, verdict in ((pa.int64(), [10, 20], 'CONSISTENT'),
                                      (pa.float64(), [1.0, 2.0], 'INCOMPLETE')):
            array = pa.DictionaryArray.from_arrays(pa.array([0, 1, None], type=pa.int8()),
                                                    pa.array(values, type=kind))
            output = io.BytesIO()
            pq.write_table(pa.table({'x': array}), output)
            data = output.getvalue()
            actual_type = pq.ParquetFile(io.BytesIO(data)).schema_arrow[0].type
            self.assertEqual(actual_type, kind)
            self.assertEqual(audit_bytes(data)['verdict'], verdict)

    def test_nested_and_non_byte_dictionary_descriptors_remain_refused(self):
        for kind in (pa.dictionary(pa.int8(), pa.list_(pa.string())),
                     pa.dictionary(pa.int8(), pa.int64())):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                describe_schema(pa.schema([pa.field('x', kind)]))
        output = io.BytesIO()
        pq.write_table(pa.table({'x': pa.array([['a']], type=pa.list_(pa.string()))}), output)
        with self.assertRaises(ValueError):
            audit_bytes(output.getvalue())


if __name__ == '__main__':
    unittest.main()
