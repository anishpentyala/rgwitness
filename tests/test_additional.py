import contextlib
import datetime as dt
import decimal
import io
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

import pyarrow as pa
import pyarrow.parquet as pq
from rgwitness.audit import audit_bytes
from rgwitness.core import Claim, compare, observe
from rgwitness.__main__ import main
from rgwitness.report import json_report
from test_audit import fixture, alter_footer_integer


class AdditionalTests(unittest.TestCase):
    def test_decoded_row_limit(self):
        with self.assertRaisesRegex(ValueError, 'decoded row'):
            observe([[1, 2, 3]], limit=2)

    def test_external_chunks_refused(self):
        data = fixture([1,2,3])
        metadata = pq.read_metadata(io.BytesIO(data))
        metadata.set_file_path('/no-reading-this-file.parquet')
        output = io.BytesIO()
        metadata.write_metadata_file(output)
        with self.assertRaisesRegex(ValueError, 'external'):
            audit_bytes(output.getvalue())

    def test_declared_row_limit(self):
        from unittest.mock import patch
        with patch('rgwitness.audit.MAX_ROWS', 2):
            with self.assertRaisesRegex(ValueError, 'row budget'):
                audit_bytes(fixture([1,2,3]))

    def test_byte_limit(self):
        from unittest.mock import patch
        with patch('rgwitness.audit.MAX_BYTES', 3):
            with self.assertRaisesRegex(ValueError, '64 MiB'):
                audit_bytes(b'1234')


    def test_real_wrong_null_count(self):
        from footer_fixture import wrong_null_count
        data = wrong_null_count(fixture([10, 20, None]), 0)
        self.assertEqual(pq.ParquetFile(io.BytesIO(data)).read().column(0).to_pylist(), [10,20,None])
        result = audit_bytes(data)
        self.assertEqual(result['columns'][0]['violations'], ['null_count'])
        self.assertEqual(result['verdict'], 'CONTRADICTED')

    def test_supported_types(self):
        cases = [
            (pa.bool_(), [True, None, False]),
            (pa.binary(), [b'\x00', b'\xff', None]),
            (pa.string(), ['\x00', '\U00010000', '\ue000']),
            (pa.decimal128(20, 3), [decimal.Decimal('1.234'), decimal.Decimal('-2.000'), None]),
            (pa.date32(), [dt.date(2020, 1, 1), None, dt.date(2024, 1, 1)]),
            (pa.timestamp('us', 'UTC'), [dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc), None]),
            (pa.time64('us'), [dt.time(1, 2, 3), None]),
            (pa.large_string(), ['a', 'z', None]),
            (pa.large_binary(), [b'a', b'z', None]),
        ]
        for kind, values in cases:
            with self.subTest(kind=kind):
                report = audit_bytes(fixture(values, kind))
                self.assertEqual(report['verdict'], 'CONSISTENT')
                json.dumps(json_report(report), allow_nan=False)

    def test_unsigned_json_exact(self):
        result = json_report(audit_bytes(fixture([2**64-1], pa.uint64())))
        self.assertEqual(result['columns'][0]['observed']['upper'],
                         {'kind': 'integer', 'value': '18446744073709551615'})

    def test_dotted_name_refused(self):
        output = io.BytesIO()
        pq.write_table(pa.table({'x.y': [1, 2]}), output)
        with self.assertRaisesRegex(ValueError, 'dotted'):
            audit_bytes(output.getvalue())

    def test_duplicate_names_refused(self):
        output = io.BytesIO()
        pq.write_table(pa.Table.from_arrays([pa.array([1]), pa.array([2])], names=['x', 'x']), output)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            audit_bytes(output.getvalue())

    def test_nonempty_bounds_on_nulls(self):
        self.assertEqual(compare(Claim(2, 2, True, 0, 10), observe([[None, None]]))['violations'],
                         ['bounds_without_values'])

    def test_randomized_independent_expected(self):
        rng = random.Random(812)
        for _ in range(500):
            values = [rng.choice([None, rng.randrange(-1000, 1001)]) for _ in range(rng.randrange(1, 50))]
            good = [x for x in values if x is not None]
            lower, upper = rng.randrange(-1200, 1200), rng.randrange(-1200, 1200)
            nulls = rng.randrange(len(values)+1)
            claim = Claim(len(values), nulls, True, lower, upper)
            result = compare(claim, observe([values]))
            expected = []
            if nulls != values.count(None): expected.append('null_count')
            if lower > upper: expected.append('reversed_bounds')
            elif not good: expected.append('bounds_without_values')
            else:
                if any(x < lower for x in good): expected.append('lower_excludes_value')
                if any(x > upper for x in good): expected.append('upper_excludes_value')
            self.assertEqual(result['violations'], expected)

    def test_multibatch(self):
        output = io.BytesIO()
        pq.write_table(pa.table({'x': list(range(70000))}), output, row_group_size=100000)
        result = audit_bytes(output.getvalue())
        self.assertEqual(result['verdict'], 'CONSISTENT')
        self.assertEqual(result['columns'][0]['observed']['upper'], 69999)

    def test_cli_outcomes(self):
        cases = [(fixture([10,20,None]),0,'CONSISTENT'),
                 (alter_footer_integer(fixture([10,20,None]),10,15),1,'CONTRADICTED'),
                 (fixture([10,20,None], statistics=False),3,'INCOMPLETE'),
                 (b'broken',2,'INVALID')]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'input.parquet'
            for data, code, verdict in cases:
                path.write_bytes(data)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = main([str(path)])
                self.assertEqual(result,code)
                self.assertEqual(json.loads(output.getvalue())['verdict'],verdict)

    @unittest.skipUnless(hasattr(os, 'mkfifo') and hasattr(os, 'O_NONBLOCK'), 'POSIX FIFO')
    def test_writerless_fifo_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory)/'fifo'
            os.mkfifo(fifo)
            result = subprocess.run([sys.executable,'-m','rgwitness',str(fifo)],
                                    capture_output=True,text=True,timeout=3)
            self.assertEqual(result.returncode,2)
            self.assertEqual(json.loads(result.stdout)['verdict'],'INVALID')

    def test_directory_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with contextlib.redirect_stdout(output): result = main([directory])
            self.assertEqual(result,2)
            self.assertEqual(json.loads(output.getvalue())['verdict'],'INVALID')

    def test_empty_file_is_incomplete(self):
        self.assertEqual(audit_bytes(fixture([]))['verdict'], 'INCOMPLETE')

    def test_mixed_supported_and_unsupported(self):
        output = io.BytesIO()
        pq.write_table(pa.table({'x': [1,2], 'y':[1.0,2.0]}), output)
        result = audit_bytes(output.getvalue())
        self.assertEqual(result['verdict'],'INCOMPLETE')
        self.assertEqual(result['checked_columns'],1)


if __name__ == '__main__':
    unittest.main()
