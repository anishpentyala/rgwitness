"""Test-only compact-protocol walker for modifying a numeric footer field.

It returns offsets, never rewrites unrelated footer bytes. It is not shipped
as a Parquet parser and is only used on tiny fixtures emitted by PyArrow.
"""
import struct


def integer_offsets(data):
    size = struct.unpack('<I', data[-8:-4])[0]
    start = len(data)-8-size
    position = start
    found = []

    def varint():
        nonlocal position
        value = shift = 0
        begin = position
        while True:
            byte = data[position]
            position += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value, begin, position
            shift += 7
            if shift > 70:
                raise ValueError('invalid fixture integer')

    def value(kind, path):
        nonlocal position
        if kind in (1, 2): return
        if kind == 3: position += 1
        elif kind in (4, 5, 6):
            number, begin, end = varint()
            number = (number >> 1) ^ -(number & 1)
            found.append((path, number, begin, end))
        elif kind == 7: position += 8
        elif kind == 8:
            n, _, _ = varint()
            position += n
        elif kind in (9, 10):
            header = data[position]; position += 1
            n = header >> 4
            if n == 15:
                n, _, _ = varint()
            for index in range(n): value(header & 15, path + (index,))
        elif kind == 11:
            n, _, _ = varint()
            if n:
                header=data[position];position+=1
                for index in range(n):
                    value(header >> 4, path+(index, 'key'))
                    value(header & 15, path+(index, 'value'))
        elif kind == 12: structure(path)
        else: raise ValueError('invalid fixture kind')

    def structure(path):
        nonlocal position
        previous = 0
        while True:
            header=data[position];position+=1
            if header == 0: return
            delta=header>>4
            field = previous+delta if delta else (lambda n: (n >> 1) ^ -(n & 1))(varint()[0])
            previous=field
            value(header&15,path+(field,))

    structure(())
    assert position == len(data)-8
    return found


def wrong_null_count(data, replacement):
    # FileMetaData.row_groups[0].columns[0].meta_data.statistics.null_count
    path = (4, 0, 1, 0, 3, 12, 3)
    matches = [entry for entry in integer_offsets(data) if entry[0] == path]
    assert len(matches) == 1, matches
    _, old, begin, end = matches[0]
    encoded = replacement << 1
    assert encoded < 128 and end-begin == 1
    return data[:begin]+bytes([encoded])+data[end:]
