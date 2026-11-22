"""Maintain fixed-byte footer controls with Apache Thrift, not runtime parsing.

Development only: install pyarrow==25.0.1 and thrift==0.22.0, then run from the
repository root. The ordinary suite uses the embedded bytes without Thrift.
"""
import base64
import copy
import io
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from thrift.Thrift import TType
from float_fixture_oracle import dump, load


# Independent review counterexample, retained byte-for-byte.
INTERVAL_REPRO_HEX = '504152311504153015304c150415001200000100000000000000000000000000000001000000000000001500151215122c15061510150615061c3602280c010000000000000000000000180c00000000010000000000000011110000000200000003030103021504192c35001806736368656d61150200150e15181502180178252a001606191c191c26001c150e1935000610191801781500160616c40116c401265426081c3602280c010000000000000000000000180c000000000100000000000000111100192c15041500150200150015101502003c29061926020400000016c4011606260816c401002820706172717565742d6370702d6172726f772076657273696f6e2032352e302e31191c1c000000ae00000050415231'

def get(fields, number):
    return next(value for field, _, value in fields if field == number)


def replace(fields, number, kind, value):
    fields[:] = [item for item in fields if item[0] != number]
    fields.append((number, kind, value))
    fields.sort(key=lambda item: item[0])


def generate():
    values = [b'\x00\x7f\x80\xff', b'\x7f\xff\x00\x80',
              b'\x80\x00\xff\x7f', b'\xff\x80\x7f\x00', None]
    out = io.BytesIO()
    pq.write_table(pa.table({'x': pa.array(values, type=pa.binary(4))}), out,
                   compression='NONE')
    original = out.getvalue()
    controls = {}
    for name in ('known', 'absent_order', 'unknown_order', 'lower_only', 'upper_only',
                 'bad_lower_only', 'bad_upper_only', 'bad_lower_width', 'bad_upper_width',
                 'wrong_nulls', 'wrong_num_values', 'wrong_path', 'wrong_physical',
                 'null_lower_only', 'null_upper_only'):
        source = original
        if name.startswith('null_'):
            null_output = io.BytesIO()
            pq.write_table(pa.table({'x': pa.array([None, None], type=pa.binary(4))}),
                           null_output, compression='NONE')
            source = null_output.getvalue()
        fields = copy.deepcopy(load(source))
        group = get(fields, 4)[1][0]
        meta = get(get(group, 1)[1][0], 3)
        stats = get(meta, 12)
        if name == 'absent_order':
            fields[:] = [item for item in fields if item[0] != 7]
        elif name == 'unknown_order':
            replace(fields, 7, TType.LIST, (TType.STRUCT, [[(77, TType.STRUCT, [])]]))
        elif name in ('lower_only', 'bad_lower_only'):
            stats[:] = [item for item in stats if item[0] != 5]
            if name.startswith('bad_'):
                replace(stats, 6, TType.STRING, b'\x20\x00\x00\x00')
        elif name in ('upper_only', 'bad_upper_only'):
            stats[:] = [item for item in stats if item[0] != 6]
            if name.startswith('bad_'):
                replace(stats, 5, TType.STRING, b'\xa0\x00\x00\x00')
        elif name == 'bad_lower_width':
            replace(stats, 6, TType.STRING, b'\x00')
        elif name == 'bad_upper_width':
            replace(stats, 5, TType.STRING, b'\xff' * 5)
        elif name == 'wrong_nulls':
            replace(stats, 3, TType.I64, 0)
        elif name == 'wrong_num_values':
            replace(meta, 5, TType.I64, 4)
        elif name == 'wrong_path':
            replace(meta, 3, TType.LIST, (TType.STRING, [b'y']))
        elif name == 'wrong_physical':
            replace(meta, 1, TType.I32, 6)
        elif name == 'null_lower_only':
            replace(stats, 6, TType.STRING, b'aaaa')
        elif name == 'null_upper_only':
            replace(stats, 5, TType.STRING, b'zzzz')
        controls[name] = base64.b64encode(dump(source, fields)).decode('ascii')
    interval = bytes.fromhex(INTERVAL_REPRO_HEX)
    controls['interval_repro'] = base64.b64encode(interval).decode('ascii')
    fields = load(interval)
    leaf = get(fields, 2)[1][1]
    leaf[:] = [item for item in leaf if item[0] != 6]
    controls['interval_plain'] = base64.b64encode(dump(interval, fields)).decode('ascii')
    for name, logical in (('logical_empty', []),
                          ('logical_unknown', [(99, TType.STRUCT, [])])):
        annotated = copy.deepcopy(fields)
        replace(get(annotated, 2)[1][1], 10, TType.STRUCT, logical)
        controls[name] = base64.b64encode(dump(interval, annotated)).decode('ascii')
    destination = Path(__file__).with_name('fixed_binary_cases.py')
    header = ('"""Fixed-byte controls generated with PyArrow25.0.1/ApacheThrift0.22.0.\n'
              'See generate_fixed_binary_fixtures.py. Do not edit encoded bytes by hand.\n"""\n'
              'import base64\n\nENCODED = {\n')
    body = ''.join('    %r: %r,\n' % item for item in controls.items())
    destination.write_text(header + body + '}\n\ndef case(name):\n'
                           '    return base64.b64decode(ENCODED[name], validate=True)\n')


if __name__ == '__main__':
    generate()
