"""Generate fixed real-file controls using independent Apache Thrift 0.22.0.
Run only for fixture maintenance, not a runtime or test dependency.
"""
import base64
import io
import json
import math
from pathlib import Path
import struct
import sys

import pyarrow as pa
import pyarrow.parquet as pq
from float_fixture_oracle import load, dump

CASES = Path(__file__).parent / 'float_cases.py'
encoded = {}


def field(obj, number):
    return next(value for index, kind, value in obj if index == number)


def setfield(obj, number, kind, value):
    obj[:] = [item for item in obj if item[0] != number]
    if value is not None:
        obj.append((number, kind, value))
    obj.sort()


def metadata(tree, group=0, col=0):
    return field(field(field(tree, 4)[1][group], 1)[1][col], 3)


cases = []
for width in (32, 64):
    code = '<f' if width == 32 else '<d'
    kind = pa.float32() if width == 32 else pa.float64()
    specifications = {
        'writer_missing_nan': ([10., 20., None], None, 'INCOMPLETE'),
        'valid': ([10., 20., None], 0, 'CONSISTENT'),
        'bad_lower': ([10., 20., None], 0, 'CONTRADICTED'),
        'bad_upper': ([10., 20., None], 0, 'CONTRADICTED'),
        'broad': ([10., 20., None], 0, 'CONSISTENT'),
        'nan': ([10., float('nan'), None], 1, 'CONSISTENT'),
        'wrong_nan': ([10., float('nan'), None], 0, 'CONTRADICTED'),
        'wrong_null': ([10., 20., None], 0, 'CONTRADICTED'),
        'all_nan': ([float('nan'), float('nan'), None], 2, 'CONSISTENT'),
        'all_null': ([None, None, None], 0, 'CONSISTENT'),
        'signed_zero': ([-0., 0., None], 0, 'CONSISTENT'),
        'infinities': ([float('-inf'), float('inf'), None], 0, 'CONSISTENT'),
        'missing_null': ([10., 20., None], 0, 'INCOMPLETE'),
        'missing_bounds': ([10., 20., None], 0, 'INCOMPLETE'),
        'nan_lower': ([10., 20., None], 0, 'INCOMPLETE'),
        'partial_lower': ([10., 20., None], 0, 'INCOMPLETE'),
        'partial_bad_upper': ([10., 20., None], 0, 'CONTRADICTED'),
        'all_nan_bounds': ([float('nan'), None, None], 1, 'CONTRADICTED'),
        'reversed': ([10., 20., None], 0, 'CONTRADICTED'),
        'missing_order': ([10., 20., None], 0, 'INCOMPLETE'),
        'total_order': ([10., 20., None], 0, 'INCOMPLETE'),
        'unknown_order': ([10., 20., None], 0, 'INCOMPLETE'),
        'negative_nan': ([10., 20., None], -1, 'CONTRADICTED'),
    }
    for name, (values, nans, verdict) in specifications.items():
        buf = io.BytesIO()
        pq.write_table(pa.table({'x': pa.array(values, type=kind)}), buf,
                       compression='NONE', use_dictionary=False)
        data = buf.getvalue()
        tree = load(data)
        meta = metadata(tree)
        stats = field(meta, 12)
        setfield(stats, 9, 10, nans)
        if name in ('bad_lower', 'broad'):
            setfield(stats, 6, 11, struct.pack(code, 15. if name == 'bad_lower' else -10.))
        if name == 'bad_upper': setfield(stats, 5, 11, struct.pack(code, 15.))
        if name == 'wrong_null': setfield(stats, 3, 10, 0)
        if name == 'missing_null': setfield(stats, 3, 10, None)
        if name == 'missing_bounds':
            setfield(stats, 5, 11, None); setfield(stats, 6, 11, None)
        if name == 'nan_lower': setfield(stats, 6, 11, struct.pack(code, float('nan')))
        if name in ('partial_lower', 'partial_bad_upper'):
            setfield(stats, 6, 11, None)
            if name == 'partial_bad_upper': setfield(stats, 5, 11, struct.pack(code, 15.))
        if name == 'all_nan_bounds':
            setfield(stats, 6, 11, struct.pack(code, 1.)); setfield(stats, 5, 11, struct.pack(code, 2.))
        if name == 'reversed':
            setfield(stats, 6, 11, struct.pack(code, 30.)); setfield(stats, 5, 11, struct.pack(code, 0.))
        if name == 'signed_zero':
            # Both directions are compatible with TYPE_ORDER zero equality.
            setfield(stats, 6, 11, struct.pack(code, 0.)); setfield(stats, 5, 11, struct.pack(code, -0.))
        if name == 'missing_order': setfield(tree, 7, 15, None)
        if name in ('total_order', 'unknown_order'):
            setfield(tree, 7, 15, (12, [[(2 if name == 'total_order' else 99, 12, [])]]))
        changed = dump(data, tree)
        # Decode every rewritten file before recording its audit expectation.
        decoded = pq.ParquetFile(io.BytesIO(changed)).read().column(0).to_pylist()
        assert len(decoded) == len(values)
        for expected, actual in zip(values, decoded):
            if expected is None: assert actual is None
            elif math.isnan(expected): assert math.isnan(actual)
            else: assert expected == actual
        filename = f'{width}-{name}.parquet'
        encoded[filename] = base64.b64encode(changed).decode('ascii')
        cases.append({'file': filename, 'width': width, 'scenario': name, 'verdict': verdict,
                      'decoded': [None if v is None else struct.pack(code, v).hex() for v in values]})
manifest = {'generator': 'PyArrow 25.0.1, Apache Thrift 0.22.0 generic Compact reader/writer',
    'note': 'Only footer metadata changed. Raw decoded float bits below are checked independently by tests.', 'cases': cases}
text = ('"""Exact reviewed fixture bytes, generated independently with Apache Thrift.\n\n'
        'Text packaging keeps ordinary tests dependency-free beyond PyArrow. Decoding\n'
        'these constants does not use the production Compact reader.\n"""\n'
        'import base64\n\nMANIFEST = ' + repr(manifest) + '\n\nENCODED = {\n')
for name, value in sorted(encoded.items()):
    text += '    ' + repr(name) + ': (\n'
    for offset in range(0, len(value), 88):
        text += '        ' + repr(value[offset:offset + 88]) + '\n'
    text += '    ),\n'
text += '}\n\n\ndef fixture_bytes(name):\n    return base64.b64decode(ENCODED[name], validate=True)\n'
CASES.write_text(text)
print('Generated', len(cases), 'text-packaged fixtures,', len(text), 'bytes')
