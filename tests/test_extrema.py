"""Extrema scan equivalence at boundary values and across chunk layouts."""
import datetime
from decimal import Decimal
import math
import random
import struct
import unittest
from rgwitness.core import Observed, observe
from rgwitness.floats import compare_float


class ExtremaTests(unittest.TestCase):
    def test_scalar_chunk_oracle(self):
        rng = random.Random(932110)
        pools = [[None, -(2**63), -1, 0, 1, 2**64 - 1],
                 [None, False, True], [None, '', 'a', 'é', '中'],
                 [None, b'', b'\x00', b'\xff'],
                 [None, Decimal('-1.2'), Decimal('0'), Decimal('2.4')],
                 [None, datetime.date(1900, 1, 1), datetime.date(2030, 1, 1)]]
        for pool in pools:
            for _ in range(100):
                values = [rng.choice(pool) for _ in range(rng.randrange(70))]
                nonnull = [v for v in values if v is not None]
                chunks = [values[i:i+7] for i in range(0, len(values), 7)]
                chunks.insert(0, [])
                expected = Observed(len(values), values.count(None),
                                    min(nonnull) if nonnull else None,
                                    max(nonnull) if nonnull else None)
                self.assertEqual(observe(iter(chunks), limit=len(values)), expected)

    def test_limits_and_iteration(self):
        self.assertEqual(observe([[]], limit=0), Observed(0, 0, None, None))
        with self.assertRaisesRegex(ValueError, 'row budget'):
            observe([[None]], limit=0)
        seen = []
        def items():
            for value in [9, None, 2, 4]:
                seen.append(value)
                yield value
        self.assertEqual(observe([items()]), Observed(4, 1, 2, 9))
        self.assertEqual(seen, [9, None, 2, 4])

    def test_float_signed_zero_and_nan_oracle(self):
        rng = random.Random(930)
        pool = [None, -math.inf, -1.5, -0.0, 0.0, 2.5, math.inf, math.nan]
        for width in (32, 64):
            pack = lambda n: struct.pack('<f' if width == 32 else '<d', n)
            for _ in range(500):
                values = [rng.choice(pool) for _ in range(rng.randrange(70))]
                nonnan = [x for x in values if x is not None and not math.isnan(x)]
                claim = dict(num_values=len(values), null_count=values.count(None),
                             nan_count=sum(x is not None and math.isnan(x) for x in values),
                             lower=pack(min(nonnan)) if nonnan else None,
                             upper=pack(max(nonnan)) if nonnan else None)
                result = compare_float(claim, [values[:11], [], values[11:]], width=width, limit=len(values))
                actual = result['observed']
                self.assertEqual(result['violations'], [])
                self.assertEqual(result['unchecked'], [])
                for key, expected in [('lower', min(nonnan) if nonnan else None),
                                      ('upper', max(nonnan) if nonnan else None)]:
                    if expected is None:
                        self.assertIsNone(actual[key])
                    else:
                        self.assertEqual(struct.pack('<d', actual[key]), struct.pack('<d', expected))
                self.assertEqual(actual['rows'], len(values))
                self.assertEqual(actual['nans'], claim['nan_count'])
                self.assertEqual(actual['nulls'], claim['null_count'])

    def test_float_equal_zero_keeps_first_bits_and_limit(self):
        for width in (32, 64):
            for values in ([-0.0, 0.0], [0.0, -0.0]):
                pack = lambda n: struct.pack('<f' if width == 32 else '<d', n)
                claim = dict(num_values=2, null_count=0, nan_count=0,
                             lower=pack(-0.0), upper=pack(0.0))
                result = compare_float(claim, [[values[0]], [values[1]]], width=width, limit=2)
                for key in ('lower', 'upper'):
                    self.assertEqual(struct.pack('<d', result['observed'][key]),
                                     struct.pack('<d', values[0]))
                self.assertEqual(result['violations'], [])
                with self.assertRaisesRegex(ValueError, 'row budget'):
                    compare_float(claim, [values], width=width, limit=1)

    def test_scalar_equal_decimals_keep_first_representation(self):
        for first, second in ((Decimal('1.0'), Decimal('1.00')),
                              (Decimal('1.00'), Decimal('1.0'))):
            result = observe([[first], [], [second]], limit=2)
            self.assertIs(result.lower, first)
            self.assertIs(result.upper, first)
            self.assertEqual(result.lower.as_tuple(), first.as_tuple())
            self.assertEqual(result.upper.as_tuple(), first.as_tuple())
