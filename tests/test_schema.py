import contextlib
import copy
import hashlib
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
from test_audit import alter_footer_integer, fixture

from rgwitness.__main__ import main
from rgwitness.audit import audit_bytes
from rgwitness.batch import audit_files
from rgwitness.schema import describe_schema, valid_schema
from rgwitness.worker import audit_in_worker


def schema_file(schema, *, statistics=True):
    output = io.BytesIO()
    table = pa.Table.from_arrays([pa.array([], type=f.type) for f in schema], schema=schema)
    pq.write_table(table, output, write_statistics=statistics)
    return output.getvalue()


def interpreted(data):
    # Independent oracle: use Arrow's native structural equality, not the
    # production descriptor or difference algorithm.
    return pq.ParquetFile(io.BytesIO(data)).schema_arrow


class SchemaTests(unittest.TestCase):
    def invoke(self, arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(arguments)
        return code, json.loads(output.getvalue())

    def check_pair(self, first, second, **options):
        with patch('rgwitness.batch.read_input', side_effect=[first, second]):
            return audit_files(['first', 'second'], require_schema_match=True, **options)

    def test_opt_in_does_not_change_ordinary_evidence(self):
        data = fixture([10, 20, None])
        plain = audit_bytes(data)
        enhanced = audit_bytes(data, include_schema=True)
        schema = enhanced.pop('schema')
        self.assertEqual(enhanced, plain)
        self.assertEqual(schema, {'format': 'rgwitness/schema-v1', 'fields': [
            {'name': 'x', 'nullable': True,
             'type': {'kind': 'integer', 'bits': 64, 'signed': True}}]})
        self.assertTrue(valid_schema(schema))

    def test_type_sign_and_width_are_distinct(self):
        for left, right in ((pa.int32(), pa.int64()), (pa.int32(), pa.uint32()),
                            (pa.string(), pa.large_string()), (pa.binary(), pa.large_binary()),
                            (pa.float32(), pa.float64()), (pa.date32(), pa.timestamp('ms')),
                            (pa.time32('ms'), pa.time64('ns')),
                            (pa.decimal128(8, 2), pa.decimal128(9, 2)),
                            (pa.decimal128(8, 2), pa.decimal128(8, 3)),
                            (pa.decimal128(8, 2), pa.decimal256(8, 2)),
                            (pa.timestamp('us'), pa.timestamp('ns')),
                            (pa.timestamp('us', 'UTC'), pa.timestamp('us', 'Asia/Kolkata')),
                            (pa.timestamp('us'), pa.timestamp('us', 'UTC'))):
            with self.subTest(left=left, right=right):
                first = schema_file(pa.schema([pa.field('x', left)]))
                second = schema_file(pa.schema([pa.field('x', right)]))
                self.assertFalse(interpreted(first).equals(interpreted(second)))
                report = self.check_pair(first, second)
                check = report['schema_check']
                self.assertEqual(check['verdict'], 'CONTRADICTED')
                self.assertEqual(check['comparisons'][0]['differences'][0]['property'], 'type')
                self.assertEqual(report['verdict'], 'CONTRADICTED')
                self.assertEqual(report['verdict_counts']['CONTRADICTED'], 0)

    def test_order_names_count_and_nullability(self):
        original = pa.schema([pa.field('x', pa.int32()), pa.field('y', pa.string())])
        variations = [pa.schema([original[1], original[0]]),
                      pa.schema([pa.field('renamed', pa.int32()), original[1]]),
                      pa.schema([original[0]]),
                      pa.schema(list(original) + [pa.field('z', pa.bool_())]),
                      pa.schema([pa.field('x', pa.int32(), nullable=False), original[1]])]
        for changed in variations:
            with self.subTest(changed=changed):
                report = self.check_pair(schema_file(original), schema_file(changed))
                self.assertEqual(report['schema_check']['verdict'], 'CONTRADICTED')
                self.assertTrue(report['schema_check']['comparisons'][0]['differences'])

    def test_schema_and_field_metadata_excluded_and_not_disclosed(self):
        first = pa.schema([pa.field('x', pa.int32(), metadata={b'secret-field': b'first'})],
                          metadata={b'secret-file': b'first'})
        second = pa.schema([pa.field('x', pa.int32(), metadata={b'secret-field': b'second'})],
                           metadata={b'secret-file': b'second'})
        report = self.check_pair(schema_file(first), schema_file(second))
        self.assertEqual(report['schema_check']['verdict'], 'CONSISTENT')
        self.assertEqual(report['schema_check']['comparisons'], [{'position': 2, 'differences': []}])
        self.assertNotIn('secret-', json.dumps(report))

    def test_same_path_separate_snapshots_not_reopened_for_schema(self):
        first = schema_file(pa.schema([pa.field('x', pa.int32())]))
        second = schema_file(pa.schema([pa.field('x', pa.int64())]))
        with patch('rgwitness.batch.read_input', side_effect=[first, second]) as read:
            report = audit_files(['same', 'same'], require_schema_match=True)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(report['schema_check']['verdict'], 'CONTRADICTED')
        for entry, data in zip(report['files'], (first, second)):
            self.assertEqual(entry['report']['input_snapshot']['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(entry['report']['schema'], describe_schema(interpreted(data)))

    def test_path_changes_after_read_do_not_change_schema_claim(self):
        first = schema_file(pa.schema([pa.field('x', pa.int32())]))
        replacement = schema_file(pa.schema([pa.field('different', pa.string())]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.parquet'
            path.write_bytes(first)
            def replace_then_audit(data, **options):
                path.write_bytes(replacement)
                return audit_bytes(data, **options)
            with patch('rgwitness.__main__.audit_bytes', side_effect=replace_then_audit):
                code, report = self.invoke(['--include-schema', str(path)])
            self.assertEqual(code, 3)
            self.assertEqual(report['schema'], describe_schema(interpreted(first)))
            self.assertEqual(report['input_snapshot']['sha256'], hashlib.sha256(first).hexdigest())
            self.assertEqual(path.read_bytes(), replacement)

    def test_first_completed_member_reference_and_invalid_gap(self):
        data = fixture([1, 2])
        with patch('rgwitness.batch.read_input', side_effect=[OSError('missing'), data, data]):
            report = audit_files(['bad', 'good', 'same'], require_schema_match=True)
        self.assertEqual(report['verdict'], 'INVALID')
        check = report['schema_check']
        self.assertEqual(check['reference_position'], 2)
        self.assertEqual(check['unavailable_positions'], [1])
        self.assertEqual(check['verdict'], 'INCOMPLETE')
        self.assertEqual(check['comparisons'], [{'position': 3, 'differences': []}])

    def test_invalid_members_do_not_mask_available_mismatches(self):
        first = schema_file(pa.schema([pa.field('x', pa.int32())]))
        second = schema_file(pa.schema([pa.field('x', pa.int64())]))
        with patch('rgwitness.batch.read_input', side_effect=[first, OSError('missing'), second]):
            report = audit_files(['one', 'bad', 'two'], require_schema_match=True)
        self.assertEqual(report['verdict'], 'INVALID')
        self.assertEqual(report['schema_check']['verdict'], 'CONTRADICTED')
        self.assertEqual(report['schema_check']['unavailable_positions'], [2])
        self.assertEqual(report['schema_check']['comparisons'][0]['position'], 3)

    def test_all_invalid_and_single_member_contracts(self):
        with patch('rgwitness.batch.read_input', side_effect=OSError('missing')):
            report = audit_files(['one', 'two'], require_schema_match=True)
        self.assertIsNone(report['schema_check']['reference_position'])
        self.assertEqual(report['schema_check']['unavailable_positions'], [1, 2])
        self.assertEqual(report['schema_check']['verdict'], 'INCOMPLETE')
        with patch('rgwitness.batch.read_input', return_value=fixture([1, 2])):
            report = audit_files(['one'], require_schema_match=True)
        self.assertEqual(report['schema_check']['reference_position'], 1)
        self.assertEqual(report['schema_check']['comparisons'], [])
        self.assertEqual(report['schema_check']['verdict'], 'CONSISTENT')

    def test_statistics_contradiction_and_gaps_remain_separate(self):
        data = fixture([10, 20, None])
        for changed, expected in ((alter_footer_integer(data, 10, 15), 'CONTRADICTED'),
                                  (fixture([1, 2], statistics=False), 'INCOMPLETE')):
            report = self.check_pair(data, changed)
            self.assertEqual(report['verdict'], expected)
            self.assertEqual(report['schema_check']['verdict'], 'CONSISTENT')
            self.assertEqual(report['files'][1]['report']['verdict'], expected)

    def test_bounded_inputs_and_options_fail_before_any_read(self):
        with patch('rgwitness.batch.read_input') as read:
            with self.assertRaises(ValueError):
                audit_files(['a'] * 33, require_schema_match=True)
            for value in (1, None, 'yes', [], {}):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    audit_files(['a'], require_schema_match=value)
            read.assert_not_called()
        for function in (audit_bytes, audit_in_worker):
            with self.assertRaises(ValueError):
                function(b'unused', include_schema=1)

    def test_cli_usage_restrictions(self):
        for options in (['--require-schema-match', 'one'],
                        ['--batch', '--include-schema', 'one']):
            with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as raised:
                    main(options)
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(output.getvalue(), '')

    def test_real_worker_and_cli_match_and_files_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ('one', 'two')]
            for path, kind in zip(paths, (pa.int32(), pa.int64())):
                path.write_bytes(schema_file(pa.schema([pa.field('x', kind)])))
            original = [path.read_bytes() for path in paths]
            args = ['--batch', '--require-schema-match'] + [str(path) for path in paths]
            direct = self.invoke(args)
            self.assertEqual(direct[0], 1)
            self.assertEqual(self.invoke(args + ['--isolate']), direct)
            self.assertEqual(self.invoke(args + ['--isolate', '--memory-mib', '4096']), direct)
            single = self.invoke(['--include-schema', str(paths[0])])
            self.assertEqual(single, self.invoke(['--include-schema', '--isolate', str(paths[0])]))
            self.assertEqual([path.read_bytes() for path in paths], original)

    def test_real_schema_worker_timeout_retains_all_members(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input'
            path.write_bytes(fixture([1, 2]))
            code, report = self.invoke(['--batch', '--require-schema-match', '--isolate',
                                        '--timeout', '0.000001', str(path), str(path)])
        self.assertEqual(code, 2)
        self.assertEqual(report['schema_check']['unavailable_positions'], [1, 2])
        for entry in report['files']:
            self.assertNotIn('schema', entry['report'])
            self.assertNotIn('input_snapshot', entry['report'])

    def test_real_worker_option_like_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '--include-schema'
            path.write_bytes(fixture([1, 2]))
            environment = dict(os.environ)
            environment['PYTHONPATH'] = str(Path.cwd())
            result = subprocess.run([sys.executable, '-m', 'rgwitness', '--include-schema',
                                     '--isolate', '--memory-mib', '4096', '--', path.name],
                                    cwd=directory, env=environment, capture_output=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('schema', json.loads(result.stdout))

    def test_seeded_schema_comparison_matches_native_arrow_equality(self):
        rng = random.Random(442701)
        kinds = [pa.int8(), pa.uint8(), pa.int32(), pa.uint32(), pa.int64(),
                 pa.bool_(), pa.string(), pa.large_string(), pa.binary(), pa.large_binary(),
                 pa.float32(), pa.float64(), pa.date32(), pa.time32('ms'), pa.time64('ns'),
                 pa.timestamp('us'), pa.timestamp('ns'), pa.timestamp('us', 'UTC'),
                 pa.timestamp('us', 'Asia/Kolkata'), pa.decimal128(9, 2),
                 pa.decimal128(9, 3), pa.decimal256(40, 8), pa.null()]
        schemas = [pa.schema([pa.field('x', kind)]) for kind in kinds]
        for _ in range(35):
            schemas.append(pa.schema([pa.field(name, rng.choice(kinds[:-1]), nullable=rng.choice([True, False]))
                                      for name in rng.sample(['x', 'y', 'Ω', 'line\nname'], rng.randint(1, 4))]))
        files = [schema_file(schema) for schema in schemas]
        native = [interpreted(data) for data in files]
        for _ in range(200):
            a, b = rng.randrange(len(files)), rng.randrange(len(files))
            with self.subTest(a=a, b=b):
                report = self.check_pair(files[a], files[b])
                expected = 'CONSISTENT' if native[a].equals(native[b], check_metadata=False) else 'CONTRADICTED'
                self.assertEqual(report['schema_check']['verdict'], expected)
                for entry in report['files']:
                    self.assertTrue(valid_schema(entry['report']['schema']))

    def test_worker_missing_or_malformed_schema_never_passes(self):
        good = describe_schema(pa.schema([pa.field('x', pa.int32())]))
        malformed = [None, [], {}, {'format': 'other', 'fields': []},
                     {'format': 'rgwitness/schema-v1', 'fields': [None]}]
        for transformation in (
                lambda s: s['fields'][0].update(nullable=1),
                lambda s: s['fields'][0].update(name=None),
                lambda s: s['fields'][0]['type'].update(bits=True),
                lambda s: s['fields'][0]['type'].update(signed='yes'),
                lambda s: s['fields'][0]['type'].update(kind='future'),
                lambda s: s['fields'][0]['type'].update(extra=1)):
            candidate = copy.deepcopy(good)
            transformation(candidate)
            malformed.append(candidate)
        for schema in malformed:
            body = json.dumps({'verdict': 'CONSISTENT', 'schema': schema}).encode()
            result = subprocess.CompletedProcess([], 0, body, b'')
            with self.subTest(schema=schema), patch('rgwitness.worker.subprocess.run', return_value=result):
                report = audit_in_worker('unused', include_schema=True)
            self.assertEqual(report['verdict'], 'INVALID')


if __name__ == '__main__':
    unittest.main()
