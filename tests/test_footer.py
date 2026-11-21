"""Small hand-encoded Compact controls independent of the production reader."""
import struct
import unittest
from unittest.mock import patch

from rgwitness.footer import _Reader, float_evidence, read_footer


def framed(raw):
    return b'PAR1' + raw + struct.pack('<I', len(raw)) + b'PAR1'


class FooterTests(unittest.TestCase):
    def test_hand_encoded_scalars_and_unknown_fields(self):
        # Fields 1..7: i32(-2), i64(1), binary('abc'), bool(false),
        # signed byte(-5), DOUBLE(1.5), UUID(raw16). STOP.
        raw = b'\x15\x03\x16\x02\x18\x03abc\x12\x13\xfb\x17' + struct.pack('<d', 1.5)
        raw += b'\x1d' + bytes(range(16)) + b'\x00'
        self.assertEqual(read_footer(framed(raw)),
                         {1: (5, -2), 2: (6, 1), 3: (8, b'abc'), 4: (2, False),
                          5: (3, -5), 6: (7, struct.pack('<d', 1.5)),
                          7: (13, bytes(range(16)))})

    def test_hand_encoded_containers(self):
        # LIST<bool>[true,false], SET<i16>[-1,2],
        # MAP<binary,i32>{'x':-3}, STRUCT{1:bool(true)}.
        raw = b'\x19\x21\x01\x02\x1a\x24\x01\x04\x1b\x01\x85\x01x\x05\x1c\x11\x00\x00'
        self.assertEqual(read_footer(framed(raw)),
                         {1: (9, (1, [True, False])), 2: (10, (4, [-1, 2])),
                          3: (11, (8, 5, [(b'x', -3)])), 4: (12, {1: (1, True)})})

    def test_long_field_delta_and_signed_values(self):
        self.assertEqual(read_footer(framed(b'\x05\xc8\x01\x01\x05\x02\x00\x00')),
                         {100: (5, -1), 1: (5, 0)})

    def test_every_truncation_rejected(self):
        raw = b'\x18\x04abcd\x19\x26\x0a\x11\x00'
        self.assertIn(2, read_footer(framed(raw)))
        for end in range(len(raw)):
            with self.subTest(end=end), self.assertRaises(ValueError):
                read_footer(framed(raw[:end]))

    def test_malformed_values(self):
        for raw in (b'\x00\x00', b'\x15\x00\x05\x02\x00\x00',
                    b'\x10', b'\x1e', b'\x19\x20', b'\x19\x21\x03\x01\x00',
                    b'\x1b\x01\x0f', b'\x05\x00\x00', b'\x15'+b'\xff'*6):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                read_footer(framed(raw))

    def test_budgets(self):
        with patch('rgwitness.footer.MAX_FOOTER_BYTES', 1):
            with self.assertRaises(ValueError): read_footer(framed(b'\x11\x00'))
        with patch('rgwitness.footer.MAX_ITEMS', 2):
            with self.assertRaises(ValueError): read_footer(framed(b'\x19\x25\x00\x00\x00'))
        with patch('rgwitness.footer.MAX_DEPTH', 1):
            with self.assertRaises(ValueError): read_footer(framed(b'\x1c\x11\x00\x00'))
        with self.assertRaises(ValueError): _Reader(b'\xff'*10).varint(64)
        with self.assertRaises(ValueError): _Reader(b'\xff'*5).integer(32)
        with self.assertRaises(ValueError): _Reader(b'\xff'*3).integer(16)

    def test_framing_and_missing_groups(self):
        for data in (b'bad', b'PAR1\x00\xff\xff\xff\xffPAR1', framed(b'\x00')[:-1]+b'2'):
            with self.assertRaises(ValueError): read_footer(data)
        with self.assertRaises(ValueError): float_evidence(framed(b'\x00'))

    def test_unknown_order_and_missing_distinct(self):
        # FileMetaData 4:LIST<STRUCT>[] and 7:LIST<STRUCT>[{99:STRUCT{}}].
        missing = b'\x49\x0c\x00'
        self.assertIsNone(float_evidence(framed(missing))['orders'])
        self.assertEqual(float_evidence(framed(b'\x49\x0c\x39\x1c\x0c\xc6\x01\x00\x00\x00'))['orders'], [99])


if __name__ == '__main__':
    unittest.main()
