import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from rgwitness.__main__ import main
from rgwitness.worker import audit_in_worker
from test_audit import alter_footer_integer, fixture


class WorkerTests(unittest.TestCase):
    def test_real_verdicts_match_in_process(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, data, code in (
                    ('good', fixture([10, 20, None]), 0),
                    ('bad', alter_footer_integer(fixture([10, 20, None]), 10, 15), 1),
                    ('missing-stats', fixture([1, 2], statistics=False), 3),
                    ('invalid', b'not parquet', 2)):
                with self.subTest(name=name):
                    path = Path(directory) / (name + '.parquet')
                    path.write_bytes(data)
                    reports = []
                    for options in ([], ['--isolate']):
                        stream = io.StringIO()
                        with contextlib.redirect_stdout(stream):
                            self.assertEqual(main([str(path)] + options), code)
                        reports.append(json.loads(stream.getvalue()))
                    self.assertEqual(reports[0], reports[1])

    def test_real_worker_timeout_via_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'good.parquet'
            path.write_bytes(fixture([1, 2, None]))
            result = subprocess.run([sys.executable, '-m', 'rgwitness', str(path),
                                     '--isolate', '--timeout', '0.000001'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            self.assertEqual(result.returncode, 2, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report['verdict'], 'INVALID')
            self.assertIn('timeout', report['error'])
            self.assertEqual(result.stderr, b'')

    def test_leading_dash_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '-input.parquet'
            path.write_bytes(fixture([1, 2, None]))
            # A relative path keeps the leading dash through both process hops.
            original = Path.cwd()
            try:
                os.chdir(directory)
                environment = dict(os.environ)
                environment['PYTHONPATH'] = str(original) + os.pathsep + environment.get('PYTHONPATH', '')
                result = subprocess.run([sys.executable, '-m', 'rgwitness', '--isolate',
                                         '--', '-input.parquet'],
                                        env=environment, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, timeout=15)
            finally:
                os.chdir(original)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['verdict'], 'CONSISTENT')

    def test_real_missing_path(self):
        self.assertEqual(audit_in_worker('/missing/rgwitness-worker.parquet')['verdict'], 'INVALID')

    def test_malformed_status_and_incomplete_reports_never_pass(self):
        for status, output in ((0, b''), (0, b'{'), (0, b'[]'),
                               (0, b'{"verdict":"INVALID"}'),
                               (0, b'{"verdict":"unknown"}'),
                               (0, b'{"verdict":[]}'),
                               (0, b'\xff'), (-signal.SIGTERM, b'{"verdict":"CONSISTENT"}'),
                               (9, b'{"verdict":"CONSISTENT"}')):
            with self.subTest(status=status, output=output):
                result = subprocess.CompletedProcess([], status, output, b'')
                with patch('rgwitness.worker.subprocess.run', return_value=result):
                    self.assertEqual(audit_in_worker('unused')['verdict'], 'INVALID')

    def test_start_failure(self):
        with patch('rgwitness.worker.subprocess.run', side_effect=OSError('cannot exec')):
            self.assertEqual(audit_in_worker('unused')['verdict'], 'INVALID')

    def test_argument_validation(self):
        for options in (['--timeout', '1'], ['--memory-mib', '64'],
                        ['--isolate', '--timeout', 'nan'],
                        ['--isolate', '--timeout', 'inf'],
                        ['--isolate', '--timeout', '0'],
                        ['--isolate', '--timeout', '-1'],
                        ['--isolate', '--memory-mib', '0']):
            with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(['unused'] + options)
                self.assertEqual(raised.exception.code, 2)
        for timeout in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                audit_in_worker('unused', timeout=timeout)
        for memory in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                audit_in_worker('unused', memory_mib=memory)
        with patch('rgwitness.worker.sys.platform', 'win32'):
            with self.assertRaisesRegex(ValueError, 'only on Linux'):
                audit_in_worker('unused', memory_mib=64)

    @unittest.skipUnless(os.name == 'posix', 'POSIX process liveness check')
    def test_real_timeout_kills_and_reaps_child(self):
        run = subprocess.run
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'pid'
            program = ('import os, pathlib, sys, time; '
                       'pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); '
                       'time.sleep(3)')
            def hanging_worker(command, **kwargs):
                return run([sys.executable, '-c', program, str(pid_file)], **kwargs)
            started = time.monotonic()
            with patch('rgwitness.worker.subprocess.run', side_effect=hanging_worker):
                report = audit_in_worker('unused', timeout=1.0)
            self.assertEqual(report['verdict'], 'INVALID')
            self.assertIn('timeout', report['error'])
            self.assertLess(time.monotonic() - started, 10)
            self.assertTrue(pid_file.exists(), 'child must have started before timeout')
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)

    @unittest.skipUnless(os.name == 'posix', 'POSIX signal termination')
    def test_real_crash_discards_partial_pass(self):
        run = subprocess.run
        program = ('import os, signal, sys; '
                   'sys.stdout.write(\'{"verdict":"CONSISTENT"}\'); '
                   'sys.stdout.flush(); os.kill(os.getpid(), signal.SIGKILL)')
        def crashing_worker(command, **kwargs):
            return run([sys.executable, '-c', program], **kwargs)
        with patch('rgwitness.worker.subprocess.run', side_effect=crashing_worker):
            report = audit_in_worker('unused')
        self.assertEqual(report['verdict'], 'INVALID')
        self.assertIn('without a complete report', report['error'])

    @unittest.skipUnless(sys.platform == 'linux', 'Linux RLIMIT_AS')
    def test_real_memory_limit_positive_and_negative_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'good.parquet'
            path.write_bytes(fixture([1, 2, None]))
            self.assertEqual(audit_in_worker(path)['verdict'], 'CONSISTENT')
            self.assertEqual(audit_in_worker(path, memory_mib=4096)['verdict'], 'CONSISTENT')
            report = audit_in_worker(path, memory_mib=64)
            self.assertEqual(report['verdict'], 'INVALID')

    @unittest.skipUnless(os.path.exists('/dev/full'), 'requires /dev/full')
    def test_isolated_output_failure_does_not_become_worker_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'good.parquet'
            path.write_bytes(fixture([1, 2, None]))
            with open('/dev/full', 'wb') as target:
                result = subprocess.run([sys.executable, '-m', 'rgwitness', str(path), '--isolate'],
                                        stdout=target, stderr=subprocess.PIPE, timeout=30)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stderr, b'')
