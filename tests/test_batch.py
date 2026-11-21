import contextlib
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rgwitness.__main__ import main
from test_audit import alter_footer_integer, fixture


class BatchTests(unittest.TestCase):
    def invoke(self, args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = main(args)
        return code, json.loads(stream.getvalue())

    def test_single_file_contract_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'one.parquet'
            path.write_bytes(fixture([10, 20, None]))
            code, report = self.invoke([str(path)])
            self.assertEqual(code, 0)
            self.assertEqual(report['format'], 'rgwitness/experimental-v1')
            self.assertNotIn('files', report)
            self.assertNotIn('path', report)

    def test_real_mixed_batch_keeps_all_reports_and_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            items = [('good', fixture([10, 20, None])),
                     ('bad', alter_footer_integer(fixture([10, 20, None]), 10, 15)),
                     ('missing-stats', fixture([1, 2], statistics=False)),
                     ('invalid', b'not parquet')]
            paths = []
            for name, data in items:
                path = Path(directory) / (name + '.parquet')
                path.write_bytes(data)
                paths.append(str(path))
            paths.append(str(Path(directory) / 'missing.parquet'))
            code, report = self.invoke(['--batch'] + paths)
            self.assertEqual(code, 2)
            self.assertEqual(report['format'], 'rgwitness/batch-v1')
            self.assertEqual(report['file_count'], 5)
            self.assertEqual(report['verdict'], 'INVALID')
            self.assertEqual(report['verdict_counts'],
                             {'CONSISTENT': 1, 'CONTRADICTED': 1, 'INCOMPLETE': 1, 'INVALID': 2})
            self.assertEqual([e['position'] for e in report['files']], list(range(1, 6)))
            self.assertEqual([e['path'] for e in report['files']], paths)
            for entry, path in zip(report['files'], paths):
                _, independent = self.invoke([path])
                self.assertEqual(entry['report'], independent)
            for entry, (_, data) in zip(report['files'][:3], items):
                self.assertEqual(entry['report']['input_snapshot'],
                                 {'byte_length': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
            for entry in report['files'][3:]:
                self.assertNotIn('input_snapshot', entry['report'])

    def test_aggregate_precedence_all_combinations(self):
        from rgwitness.batch import audit_files
        codes = {'CONSISTENT': 0, 'CONTRADICTED': 1, 'INVALID': 2, 'INCOMPLETE': 3}
        priority = {'CONSISTENT': 0, 'INCOMPLETE': 1, 'CONTRADICTED': 2, 'INVALID': 3}
        for size in (1, 2, 3):
            for values in itertools.product(codes, repeat=size):
                reports = [{'verdict': value} for value in values]
                with self.subTest(values=values), patch(
                        'rgwitness.worker.audit_in_worker', side_effect=reports):
                    report = audit_files(['one'] * size, isolate=True)
                self.assertEqual(report['verdict'], max(values, key=priority.get))
                self.assertEqual(sum(report['verdict_counts'].values()), size)
                self.assertEqual([e['report'] for e in report['files']], reports)

    def test_real_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            for data, code in [(fixture([10, 20]), 0),
                               (alter_footer_integer(fixture([10, 20]), 10, 15), 1),
                               (b'not parquet', 2), (fixture([1, 2], statistics=False), 3)]:
                path = Path(directory) / 'input.parquet'
                path.write_bytes(data)
                actual, report = self.invoke(['--batch', str(path), str(path)])
                self.assertEqual(actual, code)
                self.assertEqual(report['file_count'], 2)

    def test_duplicate_paths_are_not_deduplicated_or_reused(self):
        from rgwitness.batch import audit_files
        first = fixture([1, 2])
        second = fixture([3, 4])
        with patch('rgwitness.batch.read_input', side_effect=[first, second]) as read:
            report = audit_files(['same.parquet', 'same.parquet'])
        self.assertEqual(read.call_count, 2)
        self.assertEqual([e['path'] for e in report['files']], ['same.parquet'] * 2)
        self.assertNotEqual(report['files'][0]['report']['input_snapshot'],
                            report['files'][1]['report']['input_snapshot'])

    def test_order_and_string_paths_preserved_without_expansion(self):
        from rgwitness.batch import audit_files
        paths = ['z.parquet', './a.parquet', '~/literal.parquet', '*.parquet', 'line\nname.parquet']
        with patch('rgwitness.batch.read_input', side_effect=OSError('missing')) as read:
            report = audit_files(iter(paths))
        self.assertEqual([e['path'] for e in report['files']], paths)
        self.assertEqual([call.args[0] for call in read.call_args_list], paths)
        self.assertEqual(report['verdict_counts']['INVALID'], len(paths))

    def test_path_objects_and_lossless_scalars(self):
        from rgwitness.batch import audit_files
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'large-int.parquet'
            path.write_bytes(fixture([2**63 - 1, None]))
            report = audit_files([path])
            self.assertEqual(report['files'][0]['path'], str(path))
            bound = report['files'][0]['report']['columns'][0]['observed']['lower']
            self.assertEqual(bound, {'kind': 'integer', 'value': str(2**63 - 1)})
            json.dumps(report, allow_nan=False)

    def test_limit_accepts_32_and_rejects_33_before_reading(self):
        from rgwitness.batch import audit_files
        with patch('rgwitness.batch.read_input', side_effect=OSError('missing')) as read:
            report = audit_files(['missing'] * 32)
            self.assertEqual(report['file_count'], 32)
            self.assertEqual(read.call_count, 32)
            read.reset_mock()
            with self.assertRaises(ValueError):
                audit_files(['missing'] * 33)
            read.assert_not_called()

    def test_infinite_generator_is_bounded_before_reading(self):
        from rgwitness.batch import audit_files
        consumed = []
        def paths():
            while True:
                consumed.append(1)
                yield 'missing'
        with patch('rgwitness.batch.read_input') as read:
            with self.assertRaises(ValueError):
                audit_files(paths())
            read.assert_not_called()
        self.assertEqual(len(consumed), 33)

    def test_invalid_api_arguments_never_read_inputs(self):
        from rgwitness.batch import audit_files
        bad_paths = [[], 'one.parquet', Path('one.parquet'), b'one', [b'one'], ['one', 12]]
        with patch('rgwitness.batch.read_input') as read:
            for paths in bad_paths:
                with self.subTest(paths=paths), self.assertRaises((ValueError, TypeError)):
                    audit_files(paths)
            for options in ({'timeout': 2}, {'memory_mib': 64},
                            {'isolate': True, 'timeout': 0},
                            {'isolate': True, 'timeout': float('inf')},
                            {'isolate': True, 'timeout': float('nan')},
                            {'isolate': True, 'memory_mib': True},
                            {'isolate': True, 'memory_mib': -1},
                            {'isolate': True, 'memory_mib': 1.5}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    audit_files(['one'], **options)
            read.assert_not_called()

    def test_bad_cli_arguments_have_no_report(self):
        for args in (['--batch'], ['a', 'b'], ['--batch'] + ['a'] * 33,
                     ['--batch', 'a', '--timeout', '1'],
                     ['--batch', 'a', '--isolate', '--timeout', 'nan']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as raised:
                    main(args)
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(output.getvalue(), '')

    def test_one_input_failure_does_not_omit_later_inputs(self):
        from rgwitness.batch import audit_files
        data = fixture([1, 2])
        with patch('rgwitness.batch.read_input', side_effect=[OSError('cannot read'), data]):
            report = audit_files(['first', 'second'])
        self.assertEqual(report['verdict'], 'INVALID')
        self.assertEqual([e['report']['verdict'] for e in report['files']],
                         ['INVALID', 'CONSISTENT'])

    def test_interruption_does_not_emit_a_partial_manifest(self):
        with patch('rgwitness.input.read_input', side_effect=KeyboardInterrupt):
            # Patch the batch binding too if this module was imported earlier.
            with patch('rgwitness.batch.read_input', side_effect=KeyboardInterrupt), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(KeyboardInterrupt):
                    main(['--batch', 'one', 'two'])
                self.assertEqual(output.getvalue(), '')

    def test_real_isolation_matches_in_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.parquet'
            path.write_bytes(fixture([10, 20, None]))
            args = ['--batch', str(path), str(Path(directory) / 'missing')]
            direct = self.invoke(args)
            isolated = self.invoke(args + ['--isolate'])
            self.assertEqual(isolated, direct)

    def test_worker_options_are_per_file_and_timeout_keeps_other_reports(self):
        from rgwitness.batch import audit_files
        with patch('rgwitness.worker.audit_in_worker', side_effect=[
                {'verdict': 'INVALID', 'error': 'audit worker exceeded wall-clock timeout'},
                {'verdict': 'CONSISTENT'}]) as worker:
            report = audit_files(['one', 'two'], isolate=True, timeout=0.25, memory_mib=4096)
        self.assertEqual(report['verdict'], 'INVALID')
        self.assertEqual(len(report['files']), 2)
        for call in worker.call_args_list:
            self.assertEqual(call.kwargs, {'timeout': 0.25, 'memory_mib': 4096})

    def test_real_worker_timeouts_are_retained_for_every_member(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.parquet'
            path.write_bytes(fixture([1, 2]))
            result = subprocess.run([sys.executable, '-m', 'rgwitness', '--batch',
                                     '--isolate', '--timeout', '0.000001', str(path), str(path)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            self.assertEqual(result.returncode, 2, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report['file_count'], 2)
            self.assertEqual(report['verdict_counts']['INVALID'], 2)
            for entry in report['files']:
                self.assertIn('timeout', entry['report']['error'])
                self.assertNotIn('input_snapshot', entry['report'])
            self.assertEqual(result.stderr, b'')

    def test_directory_is_invalid_but_later_file_is_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'valid.parquet'
            path.write_bytes(fixture([1, 2]))
            code, report = self.invoke(['--batch', directory, str(path)])
            self.assertEqual(code, 2)
            self.assertEqual([e['report']['verdict'] for e in report['files']],
                             ['INVALID', 'CONSISTENT'])

    def test_short_output_write_is_failure(self):
        class ShortWriter(io.StringIO):
            def write(self, value):
                return super().write(value[:10])
        with contextlib.redirect_stdout(ShortWriter()):
            self.assertEqual(main(['--batch', '/missing/one', '/missing/two']), 2)

    @unittest.skipUnless(os.path.exists('/dev/full'), 'requires /dev/full')
    def test_real_batch_output_failure(self):
        with open('/dev/full', 'wb') as target:
            result = subprocess.run([sys.executable, '-m', 'rgwitness', '--batch',
                                     '/missing/one', '/missing/two'], stdout=target,
                                    stderr=subprocess.PIPE, timeout=15)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stderr, b'')

    def test_real_cli_preserves_unusual_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '-line\nname.parquet'
            path.write_bytes(fixture([1, 2]))
            result = subprocess.run([sys.executable, '-m', 'rgwitness', '--batch', '--', str(path)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['files'][0]['path'], str(path))
