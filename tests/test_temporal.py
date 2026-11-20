"""Real temporal footer controls with values finer than datetime.time can hold."""
import io
import json
import unittest

import pyarrow as pa
import pyarrow.parquet as pq
from rgwitness.audit import audit_bytes
from rgwitness.report import json_report
from test_audit import fixture, alter_footer_integer


class TemporalTests(unittest.TestCase):
    def test_legacy_int96_is_unchecked(self):
        output = io.BytesIO()
        pq.write_table(pa.table({'x': pa.array([1001,1999], type=pa.timestamp('ns'))}),
                       output, use_deprecated_int96_timestamps=True)
        result = audit_bytes(output.getvalue())
        self.assertEqual(result['verdict'], 'INCOMPLETE')
        self.assertEqual(result['checked_columns'], 0)
        self.assertEqual(result['unchecked'][0]['reason'], 'unsupported_temporal_storage')

    def test_timestamp_beyond_datetime_range(self):
        # Integer ticks can represent timestamps Python datetime cannot.
        data = fixture([2**63-2, 2**63-1], pa.timestamp('ms'))
        result = audit_bytes(data)
        self.assertEqual(result['verdict'], 'CONSISTENT')
        self.assertEqual(result['columns'][0]['observed']['upper'], 2**63-1)


    def test_time_nanos_wrong_lower(self):
        data = alter_footer_integer(fixture([1001, 1999, None], pa.time64('ns')), 1001, 1500)
        table = pq.ParquetFile(io.BytesIO(data)).read()
        self.assertEqual(table.column(0).cast(pa.int64()).to_pylist(), [1001, 1999, None])
        result = audit_bytes(data)
        self.assertEqual(result['verdict'], 'CONTRADICTED')
        self.assertEqual(result['columns'][0]['violations'], ['lower_excludes_value'])
        self.assertEqual(result['columns'][0]['observed']['lower'], 1001)
        self.assertEqual(result['columns'][0]['claim']['lower'], 1500)
        self.assertEqual(result['columns'][0]['comparison'],
                         {'representation': 'integer_ticks', 'unit': 'ns'})

    def test_time_nanos_wrong_upper(self):
        data = alter_footer_integer(fixture([1001, 1999, None], pa.time64('ns')), 1999, 1500)
        result = audit_bytes(data)
        self.assertEqual(result['verdict'], 'CONTRADICTED')
        self.assertEqual(result['columns'][0]['violations'], ['upper_excludes_value'])

    def test_time_nanos_wider_bounds(self):
        data = alter_footer_integer(fixture([1001, 1999, None], pa.time64('ns')), 1001, 999)
        self.assertEqual(audit_bytes(data)['verdict'], 'CONSISTENT')

    def test_timestamp_ns_negative(self):
        data = alter_footer_integer(fixture([-1999, -1001, None], pa.timestamp('ns')), -1999, -1500)
        result = audit_bytes(data)
        self.assertEqual(result['verdict'], 'CONTRADICTED')
        self.assertEqual(result['columns'][0]['observed']['lower'], -1999)

    def test_temporal_units(self):
        cases = [(pa.date32(), pa.int32(), 'days', [-2, 3, None]),
                 (pa.time32('ms'), pa.int32(), 'ms', [1, 2, None]),
                 (pa.time64('us'), pa.int64(), 'us', [1, 2, None]),
                 (pa.time64('ns'), pa.int64(), 'ns', [1, 2, None]),
                 (pa.timestamp('ms'), pa.int64(), 'ms', [-2, 3, None]),
                 (pa.timestamp('us', 'America/Los_Angeles'), pa.int64(), 'us', [-2, 3, None]),
                 (pa.timestamp('ns', 'UTC'), pa.int64(), 'ns', [-1999, 1001, None])]
        for kind, raw_type, unit, values in cases:
            with self.subTest(kind=kind):
                data = fixture(values, kind)
                table = pq.ParquetFile(io.BytesIO(data)).read()
                self.assertEqual(table.column(0).cast(raw_type).to_pylist(), values)
                result = audit_bytes(data)
                self.assertEqual(result['verdict'], 'CONSISTENT')
                self.assertEqual(result['columns'][0]['comparison']['unit'], unit)
                self.assertEqual(result['columns'][0]['observed']['lower'], min(x for x in values if x is not None))
                json.dumps(json_report(result), allow_nan=False)

    def test_temporal_missing_stats(self):
        result = audit_bytes(fixture([1, 2, None], pa.time64('ns'), statistics=False))
        self.assertEqual(result['verdict'], 'INCOMPLETE')
        self.assertEqual(result['columns'][0]['observed']['lower'], 1)

    def test_temporal_all_null(self):
        result = audit_bytes(fixture([None, None], pa.time64('ns')))
        self.assertEqual(result['verdict'], 'CONSISTENT')
        self.assertIsNone(result['columns'][0]['observed']['lower'])

    def test_ns_timestamp_ignores_python_datetime_conversion(self):
        # Values outside the datetime year range still have exact integer ticks.
        data = fixture([-(2**63)+1, 2**63-1], pa.timestamp('ns'))
        result = audit_bytes(data)
        self.assertEqual(result['verdict'], 'CONSISTENT')
        self.assertEqual(result['columns'][0]['observed']['upper'], 2**63-1)
        encoded = json_report(result)
        self.assertEqual(encoded['columns'][0]['observed']['upper']['value'], str(2**63-1))


if __name__ == '__main__':
    unittest.main()
