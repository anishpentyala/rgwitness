"""Fixed real-file controls generated with Apache Thrift, not our reader."""
import contextlib
import io
import json
import math
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from rgwitness.__main__ import main
from rgwitness.audit import audit_bytes
from rgwitness.floats import compare_float
from rgwitness.report import json_report

from float_cases import MANIFEST, fixture_bytes


class FloatTests(unittest.TestCase):
    def test_real_footer_controls_and_unchanged_values(self):
        for case in MANIFEST['cases']:
            with self.subTest(file=case['file']):
                data = fixture_bytes(case['file'])
                values = pq.ParquetFile(io.BytesIO(data)).read().column(0).to_pylist()
                code = '<f' if case['width'] == 32 else '<d'
                bits = [None if v is None else struct.pack(code, v).hex() for v in values]
                self.assertEqual(bits, case['decoded'])
                result = audit_bytes(data)
                self.assertEqual(result['verdict'], case['verdict'])
                json.dumps(json_report(result), allow_nan=False)
                if case['scenario'] == 'bad_lower':
                    self.assertEqual(result['columns'][0]['violations'], ['lower_excludes_value'])
                if case['scenario'] == 'bad_upper':
                    self.assertEqual(result['columns'][0]['violations'], ['upper_excludes_value'])
                if case['scenario'] == 'wrong_nan':
                    self.assertEqual(result['columns'][0]['violations'], ['nan_count'])
                if case['scenario'] == 'writer_missing_nan':
                    self.assertEqual(result['columns'][0]['unchecked'], ['nan_count_missing'])
                if case['scenario'] in ('missing_order', 'total_order', 'unknown_order'):
                    self.assertEqual(result['checked_columns'], 0)
                    self.assertEqual(result['unchecked'][0]['reason'], 'unsupported_float_order')

    def test_signed_zeros_are_compatible_not_total_ordered(self):
        for width in (32, 64):
            result = audit_bytes(fixture_bytes(f'{width}-signed_zero.parquet'))
            col = result['columns'][0]
            self.assertEqual(col['comparison']['signed_zero'], 'equivalent')
            self.assertEqual(math.copysign(1, col['claim']['lower']), 1)
            self.assertEqual(math.copysign(1, col['claim']['upper']), -1)
            self.assertEqual(result['verdict'], 'CONSISTENT')

    def test_many_batches_and_groups_mixed_columns(self):
        # Writer omits nan_count, so these remain incomplete while decoded
        # counts and non-NaN extrema are checked across every batch and group.
        values = [1., float('nan'), None, -2.] * 40000
        out = io.BytesIO()
        pq.write_table(pa.table({'i': range(len(values)), 'x': values}), out,
                       row_group_size=100000)
        result = audit_bytes(out.getvalue())
        self.assertEqual(result['row_groups'], 2)
        self.assertEqual(result['checked_columns'], 4)
        self.assertEqual(result['verdict'], 'INCOMPLETE')
        self.assertEqual(result['violation_count'], 0)
        self.assertEqual(result['columns'][1]['observed']['nans'], 25000)
        self.assertEqual(result['columns'][3]['observed']['nans'], 15000)

    def test_no_statistics_cannot_pass(self):
        out = io.BytesIO()
        pq.write_table(pa.table({'x': [1., None]}), out, write_statistics=False)
        report = audit_bytes(out.getvalue())
        self.assertEqual(report['verdict'], 'INCOMPLETE')
        self.assertEqual(set(report['columns'][0]['unchecked']),
                         {'null_count_missing', 'nan_count_missing',
                          'lower_bound_missing_or_nan', 'upper_bound_missing_or_nan'})

    def test_half_storage_stays_unchecked(self):
        out = io.BytesIO()
        pq.write_table(pa.table({'x': pa.array([1., 2.], type=pa.float16())}), out)
        report = audit_bytes(out.getvalue())
        self.assertEqual(report['verdict'], 'INCOMPLETE')
        self.assertEqual(report['unchecked'][0]['reason'], 'unsupported_float_storage')

    def test_cli_verdicts_are_strict_json(self):
        for name, code in (('valid', 0), ('bad_lower', 1), ('writer_missing_nan', 3)):
            with self.subTest(scenario=name), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'input.parquet'
                path.write_bytes(fixture_bytes(f'64-{name}.parquet'))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = main([str(path)])
                self.assertEqual(result, code)
                self.assertIn(json.loads(output.getvalue())['verdict'],
                              ('CONSISTENT', 'CONTRADICTED', 'INCOMPLETE'))

    def test_alignment_guards_refuse_mismatched_evidence(self):
        from rgwitness.footer import float_evidence
        data = fixture_bytes('64-valid.parquet')
        for kind in ('groups', 'columns', 'orders', 'path', 'type', 'rows'):
            info = float_evidence(data)
            if kind == 'groups': info['row_groups'].append([])
            if kind == 'columns': info['row_groups'][0].append({})
            if kind == 'orders': info['orders'].append(1)
            if kind == 'path': info['row_groups'][0][0]['path'] = [b'wrong']
            if kind == 'type': info['row_groups'][0][0]['physical_type'] = 4
            if kind == 'rows': info['row_groups'][0][0]['num_values'] = 100
            with self.subTest(kind=kind), patch('rgwitness.footer.float_evidence', return_value=info):
                with self.assertRaisesRegex(ValueError, 'disagree'):
                    audit_bytes(data)

    def test_malformed_width_and_row_budget(self):
        claim = {'num_values': 2, 'nan_count': 0, 'null_count': 0,
                 'lower': b'bad', 'upper': None}
        with self.assertRaisesRegex(ValueError, 'width'):
            compare_float(claim, [[1., 2.]], width=32, limit=2)
        claim['lower'] = struct.pack('<f', 1.)
        with self.assertRaisesRegex(ValueError, 'budget'):
            compare_float(claim, [[1., 2.]], width=32, limit=1)
        with self.assertRaisesRegex(ValueError, 'width'):
            compare_float(claim, [], width=16, limit=1)

    def test_float_json_is_exact_finite_and_nonfinite(self):
        for value in (-0., 0., float('-inf'), float('inf'), float('nan'), 1.5):
            claim = {'num_values': 1, 'nan_count': int(math.isnan(value)), 'null_count': 0,
                     'lower': None, 'upper': None}
            col = compare_float(claim, [[value]], width=64, limit=1)
            report = {'columns': [col]}
            json.dumps(json_report(report), allow_nan=False)
            self.assertIs(col['observed']['lower'], None if math.isnan(value) else value)
            if not math.isnan(value):
                encoded = json_report(report)['columns'][0]['observed']['lower']
                self.assertEqual(encoded, {'kind': 'float64', 'little_endian_hex': struct.pack('<d', value).hex()})


if __name__ == '__main__':
    unittest.main()
