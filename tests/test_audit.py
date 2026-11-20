import io
import struct
import unittest

import pyarrow as pa
import pyarrow.parquet as pq
from rgwitness.audit import audit_bytes
from rgwitness.core import Claim, compare, observe


def fixture(values, kind=pa.int64(), statistics=True):
    output = io.BytesIO()
    pq.write_table(pa.table({'x': pa.array(values, type=kind)}), output,
                   compression='NONE', write_statistics=statistics,
                   row_group_size=3)
    return output.getvalue()


def alter_footer_integer(data, old, new):
    size = int.from_bytes(data[-8:-4], 'little')
    start = len(data) - 8 - size
    footer = data[start:-8]
    original = struct.pack('<q', old)
    replacement = struct.pack('<q', new)
    assert footer.count(original) == 2, footer.count(original)
    footer = footer.replace(original, replacement)
    return data[:start] + footer + data[-8:]


class CoreTests(unittest.TestCase):
    def test_conservative_bounds(self):
        actual = observe([[2, None, 4]])
        self.assertEqual(compare(Claim(3, 1, True, 0, 10), actual)['violations'], [])

    def test_wrong_lower(self):
        actual = observe([[10, None, 20]])
        self.assertEqual(compare(Claim(3, 1, True, 15, 20), actual)['violations'],
                         ['lower_excludes_value'])

    def test_wrong_upper(self):
        actual = observe([[10, None, 20]])
        self.assertEqual(compare(Claim(3, 1, True, 10, 15), actual)['violations'],
                         ['upper_excludes_value'])

    def test_null_count(self):
        actual = observe([[10, None, 20]])
        self.assertEqual(compare(Claim(3, 0, True, 10, 20), actual)['violations'], ['null_count'])

    def test_missing_is_not_zero(self):
        actual = observe([[10, 20]])
        self.assertEqual(compare(Claim(2, None, False), actual)['unchecked'],
                         ['null_count_missing', 'bounds_missing'])

    def test_all_null(self):
        actual = observe([[None, None]])
        self.assertEqual(compare(Claim(2, 2, False), actual)['unchecked'], [])


class FileTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(audit_bytes(fixture([10, 20, None]))['verdict'], 'CONSISTENT')

    def test_bad_lower(self):
        data = alter_footer_integer(fixture([10, 20, None]), 10, 15)
        # Negative control: data still decodes fully and unchanged.
        self.assertEqual(pq.ParquetFile(io.BytesIO(data)).read().column(0).to_pylist(),
                         [10, 20, None])
        report = audit_bytes(data)
        self.assertEqual(report['verdict'], 'CONTRADICTED')
        self.assertEqual(report['columns'][0]['violations'], ['lower_excludes_value'])

    def test_bad_upper(self):
        data = alter_footer_integer(fixture([10, 20, None]), 20, 15)
        self.assertEqual(audit_bytes(data)['columns'][0]['violations'], ['upper_excludes_value'])

    def test_broad_bounds_not_corrupt(self):
        data = alter_footer_integer(fixture([10, 20, None]), 10, 0)
        self.assertEqual(audit_bytes(data)['verdict'], 'CONSISTENT')

    def test_missing_stats(self):
        report = audit_bytes(fixture([10, 20, None], statistics=False))
        self.assertEqual(report['verdict'], 'INCOMPLETE')
        self.assertEqual(report['violation_count'], 0)

    def test_all_null(self):
        self.assertEqual(audit_bytes(fixture([None, None]))['verdict'], 'CONSISTENT')

    def test_multiple_groups(self):
        report = audit_bytes(fixture([1, 2, None, 4, 5, None, 7]))
        self.assertEqual(report['verdict'], 'CONSISTENT')
        self.assertEqual(report['row_groups'], 3)

    def test_float_not_certified(self):
        report = audit_bytes(fixture([1.0, float('nan')], pa.float64()))
        self.assertEqual(report['verdict'], 'INCOMPLETE')
        self.assertEqual(report['checked_columns'], 0)

    def test_strings(self):
        self.assertEqual(audit_bytes(fixture(['a', 'z', None], pa.string()))['verdict'], 'CONSISTENT')

    def test_unsigned_boundary(self):
        self.assertEqual(audit_bytes(fixture([0, 2**64-1], pa.uint64()))['verdict'], 'CONSISTENT')

    def test_nested_refused(self):
        with self.assertRaisesRegex(ValueError, 'nested'):
            audit_bytes(fixture([[1], [2]], pa.list_(pa.int64())))

    def test_invalid_file(self):
        with self.assertRaises(Exception):
            audit_bytes(b'not parquet')


if __name__ == '__main__':
    unittest.main()
