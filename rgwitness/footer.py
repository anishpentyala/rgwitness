"""Private prototype: bounded Compact Thrift footer evidence, not a decoder.

Only float order and statistics are consumed. Unknown orders are returned as
unknown, never treated as a supported comparison. Admission limits are new.
"""
import struct

MAX_FOOTER_BYTES = 1024 * 1024
MAX_ITEMS = 100_000
MAX_DEPTH = 32


class _Reader:
    def __init__(self, data):
        self.data = memoryview(data)
        self.pos = 0
        self.items = 0

    def take(self, count):
        if count < 0 or count > len(self.data) - self.pos:
            raise ValueError('truncated compact footer')
        begin = self.pos
        self.pos += count
        return self.data[begin:self.pos]

    def byte(self):
        return self.take(1)[0]

    def varint(self, bits=64):
        number = 0
        for shift in range(0, bits, 7):
            byte = self.byte()
            number |= (byte & 127) << shift
            if number >= 1 << bits:
                raise ValueError('compact integer overflow')
            if byte < 128:
                return number
        raise ValueError('unterminated compact integer')

    def integer(self, bits):
        value = self.varint(bits)
        return (value >> 1) ^ -(value & 1)

    def charge(self, count=1):
        self.items += count
        if self.items > MAX_ITEMS:
            raise ValueError('compact footer item budget exceeded')

    def value(self, kind, depth, *, field=False):
        self.charge()
        if depth > MAX_DEPTH:
            raise ValueError('compact footer nesting budget exceeded')
        if kind in (1, 2):
            if field:
                return kind == 1
            byte = self.byte()
            if byte not in (1, 2):
                raise ValueError('invalid compact boolean')
            return byte == 1
        if kind == 3:
            byte = self.byte()
            return byte if byte < 128 else byte - 256
        if kind in (4, 5, 6):
            return self.integer({4: 16, 5: 32, 6: 64}[kind])
        if kind == 7:
            return bytes(self.take(8))
        if kind == 8:
            return bytes(self.take(self.varint(32)))
        if kind in (9, 10):
            header = self.byte()
            count, element = header >> 4, header & 15
            if count == 15:
                count = self.varint(32)
            if element not in range(1, 14):
                raise ValueError('invalid compact list type')
            if count > MAX_ITEMS - self.items:
                raise ValueError('compact footer list budget exceeded')
            return (element, [self.value(element, depth + 1) for _ in range(count)])
        if kind == 11:
            count = self.varint(32)
            if count > (MAX_ITEMS - self.items) // 2:
                raise ValueError('compact footer map budget exceeded')
            if not count:
                return (0, 0, [])
            header = self.byte()
            key, element = header >> 4, header & 15
            if key not in range(1, 14) or element not in range(1, 14):
                raise ValueError('invalid compact map type')
            return (key, element, [(self.value(key, depth + 1),
                                   self.value(element, depth + 1)) for _ in range(count)])
        if kind == 13:
            return bytes(self.take(16))
        if kind == 12:
            result = {}
            previous = 0
            while True:
                header = self.byte()
                if header == 0:
                    return result
                delta, element = header >> 4, header & 15
                number = previous + delta if delta else self.integer(16)
                if number <= 0 or number > 32767 or number in result:
                    raise ValueError('invalid or duplicate compact field')
                result[number] = (element, self.value(element, depth + 1, field=True))
                previous = number
        raise ValueError('invalid compact value type')


def read_footer(data):
    """Read one ordinary, unencrypted Parquet footer under explicit budgets."""
    if not isinstance(data, bytes) or len(data) < 12 or data[:4] != b'PAR1' or data[-4:] != b'PAR1':
        raise ValueError('expected unencrypted Parquet framing')
    length = struct.unpack('<I', data[-8:-4])[0]
    if length > MAX_FOOTER_BYTES or length > len(data) - 12:
        raise ValueError('footer size budget or framing invalid')
    reader = _Reader(data[-8-length:-8])
    result = reader.value(12, 0)
    if reader.pos != length:
        raise ValueError('trailing compact footer data')
    return result


def _field(obj, number, kind, default=None):
    item = obj.get(number)
    if item is None:
        return default
    if item[0] != kind:
        raise ValueError('unexpected footer field type')
    return item[1]


def _struct_list(obj, number, default=None):
    item = _field(obj, number, 9)
    if item is None:
        return default
    kind, values = item
    if kind != 12:
        raise ValueError('expected footer struct list')
    return values


def float_evidence(data):
    """Return order identifiers and raw per-column float evidence.

    Missing and unknown fields stay distinct from an explicit zero or known
    order. This does not establish schema alignment with a decoder by itself.
    """
    footer = read_footer(data)
    orders = _struct_list(footer, 7)
    if orders is not None:
        orders = [next(iter(order)) if len(order) == 1
                  and next(iter(order.values())) == (12, {}) else None
                  for order in orders]
    groups = _struct_list(footer, 4)
    if groups is None:
        raise ValueError('missing row-group metadata')
    evidence = []
    for group in groups:
        columns = _struct_list(group, 1)
        if columns is None:
            raise ValueError('missing column metadata')
        row = []
        for column in columns:
            meta = _field(column, 3, 12)
            if meta is None:
                raise ValueError('encrypted or missing column metadata')
            stats = _field(meta, 12, 12, {})
            path = _field(meta, 3, 9)
            if path is not None and path[0] != 8:
                raise ValueError('unexpected column path element type')
            row.append({'physical_type': _field(meta, 1, 5),
                        'path': None if path is None else path[1],
                        'num_values': _field(meta, 5, 6),
                        'nan_count': _field(stats, 9, 6),
                        'null_count': _field(stats, 3, 6),
                        'lower': _field(stats, 6, 8),
                        'upper': _field(stats, 5, 8)})
        evidence.append(row)
    return {'orders': orders, 'row_groups': evidence}
