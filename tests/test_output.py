import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rgwitness.__main__ import main
from test_audit import fixture


class BrokenStream(io.StringIO):
    def __init__(self, stage, error):
        super().__init__()
        self.stage, self.error = stage, error

    def write(self, value):
        if self.stage == 'write':
            raise self.error('output failed')
        if self.stage == 'short':
            return len(value) - 1
        return super().write(value)

    def flush(self):
        if self.stage == 'flush':
            raise self.error('output failed')
        return super().flush()


class OutputTests(unittest.TestCase):
    def test_callable_write_and_flush_failures(self):
        for stage in ('write', 'flush', 'short'):
            for error in (OSError, ValueError, AttributeError):
                with self.subTest(stage=stage, error=error):
                    stream = BrokenStream(stage, error)
                    with contextlib.redirect_stdout(stream):
                        self.assertEqual(main(['/missing/rgwitness.parquet']), 2)
                        self.assertIs(sys.stdout, stream)
                    self.assertFalse(stream.closed)

    def test_callable_closed_stream(self):
        stream = io.StringIO()
        stream.close()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(main(['/missing/rgwitness.parquet']), 2)
            self.assertIs(sys.stdout, stream)

    def test_callable_success_flushes(self):
        stream = BrokenStream('none', OSError)
        with contextlib.redirect_stdout(stream), patch.object(stream, 'flush', wraps=stream.flush) as flush:
            self.assertEqual(main(['/missing/rgwitness.parquet']), 2)
            flush.assert_called_once()
        self.assertEqual(json.loads(stream.getvalue())['verdict'], 'INVALID')

    def paths(self, directory):
        good = Path(directory) / 'good.parquet'
        good.write_bytes(fixture([1, 2, None]))
        return [str(good), str(Path(directory) / 'absent.parquet')]

    @unittest.skipUnless(os.name == 'posix', 'POSIX pipe descriptors')
    def test_real_closed_pipe_process(self):
        with tempfile.TemporaryDirectory() as directory:
            for path in self.paths(directory):
                with self.subTest(path=path):
                    read, write = os.pipe()
                    os.close(read)
                    with os.fdopen(write, 'wb') as target:
                        result = subprocess.run([sys.executable, '-m', 'rgwitness', path],
                                                stdout=target, stderr=subprocess.PIPE, timeout=15)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stderr, b'')

    @unittest.skipUnless(os.path.exists('/dev/full'), 'requires /dev/full')
    def test_real_full_device_process(self):
        with tempfile.TemporaryDirectory() as directory:
            for path in self.paths(directory):
                with self.subTest(path=path):
                    with open('/dev/full', 'wb') as target:
                        result = subprocess.run([sys.executable, '-m', 'rgwitness', path],
                                                stdout=target, stderr=subprocess.PIPE, timeout=15)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stderr, b'')

    def test_report_status_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            good, bad = self.paths(directory)
            for path, code, verdict in ((good, 0, 'CONSISTENT'), (bad, 2, 'INVALID')):
                with self.subTest(verdict=verdict):
                    result = subprocess.run([sys.executable, '-m', 'rgwitness', path],
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
                    self.assertEqual(result.returncode, code, result.stderr)
                    self.assertEqual(json.loads(result.stdout)['verdict'], verdict)
                    self.assertEqual(result.stderr, b'')
