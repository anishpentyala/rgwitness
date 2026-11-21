"""Exact interpreted flat-schema contracts, excluding key/value metadata."""


def describe_schema(schema):
    """Describe admitted Arrow scalar types without string-rendering equality."""
    import pyarrow as pa

    def describe_type(kind):
        if pa.types.is_integer(kind):
            return {'kind': 'integer', 'bits': kind.bit_width,
                    'signed': pa.types.is_signed_integer(kind)}
        if pa.types.is_floating(kind):
            return {'kind': 'float', 'bits': kind.bit_width}
        if pa.types.is_decimal(kind):
            return {'kind': 'decimal', 'bits': kind.bit_width,
                    'precision': kind.precision, 'scale': kind.scale}
        if pa.types.is_timestamp(kind):
            return {'kind': 'timestamp', 'unit': kind.unit, 'timezone': kind.tz}
        if pa.types.is_time(kind):
            return {'kind': 'time', 'bits': kind.bit_width, 'unit': kind.unit}
        for name, predicate in (
                ('boolean', pa.types.is_boolean), ('string', pa.types.is_string),
                ('large_string', pa.types.is_large_string),
                ('binary', pa.types.is_binary), ('large_binary', pa.types.is_large_binary),
                ('date32', pa.types.is_date32), ('date64', pa.types.is_date64),
                ('null', pa.types.is_null)):
            if predicate(kind):
                return {'kind': name}
        # Never equate an unrecognized type through a potentially lossy repr.
        raise ValueError('unsupported schema contract type')

    return {'format': 'rgwitness/schema-v1',
            'fields': [{'name': field.name, 'nullable': field.nullable,
                        'type': describe_type(field.type)} for field in schema]}


def compare_schemas(entries):
    """Compare every available schema to the first completed member's schema.

    Positions identify separate reads, not unique paths. Unavailable members
    leave a gap even when all available schemas agree.
    """
    reference = None
    reference_position = None
    comparisons = []
    unavailable = []
    mismatch = False
    for entry in entries:
        schema = entry['report'].get('schema')
        if schema is None:
            unavailable.append(entry['position'])
            continue
        if reference is None:
            reference = schema
            reference_position = entry['position']
            continue
        differences = []
        expected = reference['fields']
        actual = schema['fields']
        for index in range(max(len(expected), len(actual))):
            left = expected[index] if index < len(expected) else None
            right = actual[index] if index < len(actual) else None
            if left is None or right is None:
                differences.append({'field_position': index, 'property': 'field',
                                    'reference': left, 'actual': right})
                continue
            for property_name in ('name', 'type', 'nullable'):
                if left[property_name] != right[property_name]:
                    differences.append({'field_position': index, 'property': property_name,
                                        'reference': left[property_name],
                                        'actual': right[property_name]})
        mismatch = mismatch or bool(differences)
        comparisons.append({'position': entry['position'], 'differences': differences})
    verdict = ('CONTRADICTED' if mismatch else 'INCOMPLETE' if unavailable or reference is None
               else 'CONSISTENT')
    return {'verdict': verdict, 'reference_position': reference_position,
            'comparisons': comparisons, 'unavailable_positions': unavailable,
            'scope': 'exact interpreted field order, names, types and nullability; metadata excluded'}


def valid_schema(schema):
    """Validate the optional JSON descriptor without importing the decoder."""
    if not isinstance(schema, dict) or set(schema) != {'format', 'fields'}:
        return False
    if schema['format'] != 'rgwitness/schema-v1':
        return False
    fields = schema['fields']
    if not isinstance(fields, list) or len(fields) > 128:
        return False
    simple = {'boolean', 'string', 'large_string', 'binary', 'large_binary',
              'date32', 'date64', 'null'}
    for field in fields:
        if (not isinstance(field, dict) or set(field) != {'name', 'nullable', 'type'}
                or not isinstance(field['name'], str) or type(field['nullable']) is not bool):
            return False
        kind = field['type']
        if not isinstance(kind, dict) or not isinstance(kind.get('kind'), str):
            return False
        name = kind['kind']
        keys = set(kind)
        if name in simple:
            if keys != {'kind'}:
                return False
        elif name == 'integer':
            if (keys != {'kind', 'bits', 'signed'} or type(kind['bits']) is not int
                    or kind['bits'] not in (8, 16, 32, 64) or type(kind['signed']) is not bool):
                return False
        elif name == 'float':
            if (keys != {'kind', 'bits'} or type(kind['bits']) is not int
                    or kind['bits'] not in (16, 32, 64)):
                return False
        elif name == 'decimal':
            if (keys != {'kind', 'bits', 'precision', 'scale'}
                    or any(type(kind[key]) is not int for key in ('bits', 'precision', 'scale'))
                    or kind['bits'] not in (32, 64, 128, 256)
                    or not 1 <= kind['precision'] <= {32: 9, 64: 18, 128: 38, 256: 76}[kind['bits']]):
                return False
        elif name == 'timestamp':
            if (keys != {'kind', 'unit', 'timezone'}
                    or kind['unit'] not in ('s', 'ms', 'us', 'ns')
                    or (kind['timezone'] is not None and not isinstance(kind['timezone'], str))):
                return False
        elif name == 'time':
            if (keys != {'kind', 'bits', 'unit'} or type(kind['bits']) is not int
                    or (kind['bits'], kind['unit']) not in ((32, 's'), (32, 'ms'),
                                                          (64, 'us'), (64, 'ns'))):
                return False
        else:
            return False
    return True
