import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pyarrow.parquet as pq
from rgwitness.audit import audit_bytes
from rgwitness.__main__ import main
from rgwitness.report import json_report
from test_audit import fixture, alter_footer_integer


class SnapshotTests(unittest.TestCase):
    def assert_identity(self, report, data):
        self.assertEqual(report.get('input_snapshot'), {
            'byte_length': len(data),
            'sha256': hashlib.sha256(data).hexdigest(),
        })

    def test_exact_bytes_for_all_report_verdicts(self):
        original = fixture([10, 20, None])
        for data, verdict in (
                (original, 'CONSISTENT'),
                (alter_footer_integer(original, 10, 15), 'CONTRADICTED'),
                (fixture([10, 20, None], statistics=False), 'INCOMPLETE')):
            with self.subTest(verdict=verdict):
                report = audit_bytes(data)
                self.assertEqual(report['verdict'], verdict)
                self.assert_identity(report, data)

    def test_footer_change_with_same_decoded_values_changes_identity(self):
        original = fixture([10, 20, None])
        changed = alter_footer_integer(original, 10, 0)
        values = lambda data: pq.read_table(io.BytesIO(data)).to_pydict()
        self.assertEqual(values(original), values(changed))
        first, second = audit_bytes(original), audit_bytes(changed)
        self.assertEqual(first['columns'][0]['observed'], second['columns'][0]['observed'])
        self.assert_identity(first, original)
        self.assert_identity(second, changed)
        self.assertNotEqual(first['input_snapshot']['sha256'], second['input_snapshot']['sha256'])

    def test_same_bytes_and_json_conversion_preserve_identity(self):
        data = fixture([10, 20, None])
        report = audit_bytes(data)
        serialized = json.loads(json.dumps(json_report(report), allow_nan=False))
        self.assert_identity(report, data)
        self.assert_identity(serialized, data)
        self.assert_identity(audit_bytes(bytes(bytearray(data))), data)

    def test_snapshot_survives_path_replacement_after_read(self):
        original = fixture([10, 20, None])
        replacement = fixture([90, 100, None])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.parquet'
            path.write_bytes(original)
            def replace_then_audit(data):
                path.write_bytes(replacement)
                return audit_bytes(data)
            output = io.StringIO()
            with patch('rgwitness.__main__.audit_bytes', side_effect=replace_then_audit):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main([str(path)]), 0)
            self.assertEqual(path.read_bytes(), replacement)
            self.assert_identity(json.loads(output.getvalue()), original)

    def test_isolation_and_renaming_do_not_change_identity(self):
        data = fixture([10, 20, None])
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ('first.parquet', 'renamed.parquet')]
            reports = []
            for path in paths:
                path.write_bytes(data)
                for options in ([], ['--isolate']):
                    result = subprocess.run([sys.executable, '-m', 'rgwitness', str(path)] + options,
                                            capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    report = json.loads(result.stdout)
                    self.assert_identity(report, data)
                    reports.append(report)
            self.assertTrue(all(report == reports[0] for report in reports))
            self.assertNotIn(str(directory), json.dumps(reports))

    def test_failed_admission_has_no_snapshot_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'invalid.parquet'
            path.write_bytes(b'not parquet')
            for options in ([], ['--isolate']):
                with self.subTest(options=options):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(main([str(path)] + options), 2)
                    self.assertNotIn('input_snapshot', json.loads(output.getvalue()))
