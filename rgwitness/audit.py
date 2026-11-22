"""Experimental flat-scalar Parquet row-group claim audit."""
import hashlib
import io
from .core import Claim, compare, observe

MAX_BYTES = 64 * 1024 * 1024
MAX_ROWS = 2_000_000


def audit_bytes(data: bytes, *, include_schema=False) -> dict:
    if not isinstance(include_schema, bool):
        raise ValueError('include_schema must be a boolean')
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise ValueError('expected bytes, at most 64 MiB')
    import pyarrow as pa
    import pyarrow.parquet as pq
    source = pq.ParquetFile(io.BytesIO(data))
    metadata = source.metadata
    if metadata.num_rows > MAX_ROWS:
        raise ValueError('row budget exceeds 2,000,000')
    schema = source.schema_arrow
    names = schema.names
    if len(schema) > 128 or metadata.num_row_groups * len(schema) > 4096:
        raise ValueError('column or row-group budget exceeded')
    if metadata.num_rows * len(schema) > 4_000_000:
        raise ValueError('cell budget exceeds 4,000,000')
    if len(set(names)) != len(names) or any('.' in n for n in names):
        raise ValueError('duplicate or dotted field names are not supported')
    results = []
    unchecked = []
    supported = lambda t: (pa.types.is_integer(t) or pa.types.is_boolean(t)
                            or pa.types.is_string(t) or pa.types.is_binary(t)
                            or pa.types.is_large_string(t)
                            or pa.types.is_large_binary(t)
                            or pa.types.is_decimal(t) or pa.types.is_date(t)
                            or pa.types.is_time(t) or pa.types.is_timestamp(t)
                            or (pa.types.is_dictionary(t)
                                and (pa.types.is_string(t.value_type)
                                     or pa.types.is_binary(t.value_type))))
    flat = all(supported(field.type) or pa.types.is_floating(field.type)
               or pa.types.is_null(field.type) for field in schema)
    # A nested column changes the mapping from Arrow fields to Parquet leaves.
    # Refuse the complete file rather than silently checking the wrong index.
    if not flat or metadata.num_columns != len(schema):
        raise ValueError('nested or unsupported physical layout')
    if sum(metadata.row_group(i).num_rows for i in range(metadata.num_row_groups)) != metadata.num_rows:
        raise ValueError('file and row-group row counts disagree')
    float_info = None
    if any(pa.types.is_floating(field.type) for field in schema):
        from .footer import float_evidence
        float_info = float_evidence(data)
        if len(float_info['row_groups']) != metadata.num_row_groups:
            raise ValueError('footer row-group evidence disagrees with decoder')
        orders = float_info['orders']
        if orders is not None and len(orders) != len(schema):
            raise ValueError('footer column-order evidence disagrees with schema')
    for group_index in range(metadata.num_row_groups):
        group = metadata.row_group(group_index)
        if any(group.column(i).file_path for i in range(group.num_columns)):
            raise ValueError('external column chunks are not supported')
        if float_info is not None and len(float_info['row_groups'][group_index]) != group.num_columns:
            raise ValueError('footer column evidence disagrees with decoder')
        for column_index, field in enumerate(schema):
            if pa.types.is_floating(field.type):
                order = None if orders is None else orders[column_index]
                if field.type.bit_width not in (32, 64):
                    unchecked.append({'row_group': group_index, 'column': field.name,
                                      'reason': 'unsupported_float_storage', 'type': str(field.type)})
                    continue
                if order != 1:
                    unchecked.append({'row_group': group_index, 'column': field.name,
                                      'reason': 'unsupported_float_order', 'type': str(field.type)})
                    continue
                from .floats import compare_float
                evidence = float_info['row_groups'][group_index][column_index]
                width = field.type.bit_width
                expected_physical = 4 if width == 32 else 5
                if (width not in (32, 64) or evidence['physical_type'] != expected_physical
                        or evidence['num_values'] != group.num_rows
                        or evidence['path'] != [field.name.encode('utf-8')]):
                    raise ValueError('float evidence and decoded schema disagree')
                batches = source.iter_batches(batch_size=65536, row_groups=[group_index],
                                              columns=[field.name], use_threads=False)
                result = compare_float(evidence, (batch.column(0).to_pylist() for batch in batches),
                                       width=width, limit=MAX_ROWS)
                result.update(row_group=group_index, column=field.name, type=str(field.type))
                results.append(result)
                continue
            if not supported(field.type):
                unchecked.append({'row_group': group_index, 'column': field.name,
                                  'reason': 'unsupported_type', 'type': str(field.type)})
                continue
            temporal = (pa.types.is_date(field.type) or pa.types.is_time(field.type)
                        or pa.types.is_timestamp(field.type))
            stats = group.column(column_index).statistics
            # Python datetime.time has only microsecond resolution. Compare
            # temporal values as integer ticks, never through to_pylist's
            # potentially lossy datetime conversion (notably TIME_NANOS).
            if temporal:
                physical = group.column(column_index).physical_type
                expected = ('INT32' if pa.types.is_date32(field.type)
                            or pa.types.is_time32(field.type) else 'INT64')
                if physical != expected:
                    unchecked.append({'row_group': group_index, 'column': field.name,
                                      'reason': 'unsupported_temporal_storage',
                                      'type': str(field.type)})
                    continue
                lower = stats.min_raw if stats and stats.has_min_max else None
                upper = stats.max_raw if stats and stats.has_min_max else None
                tick_type = pa.int32() if expected == 'INT32' else pa.int64()
                unit = 'days' if pa.types.is_date32(field.type) else field.type.unit
            else:
                lower = stats.min if stats and stats.has_min_max else None
                upper = stats.max if stats and stats.has_min_max else None
            claim = Claim(group.num_rows,
                          stats.null_count if stats and stats.has_null_count else None,
                          bool(stats and stats.has_min_max), lower, upper)
            batches = source.iter_batches(batch_size=65536, row_groups=[group_index],
                                          columns=[field.name], use_threads=False)
            chunks = ((batch.column(0).cast(tick_type).to_pylist() if temporal
                       else batch.column(0).to_pylist()) for batch in batches)
            actual = observe(chunks, limit=MAX_ROWS)
            result = compare(claim, actual)
            result.update(row_group=group_index, column=field.name, type=str(field.type))
            if temporal:
                result['comparison'] = {'representation': 'integer_ticks', 'unit': unit}
            results.append(result)
    violations = sum(len(r['violations']) for r in results)
    gaps = len(unchecked) + sum(len(r['unchecked']) for r in results)
    if not results:
        gaps += 1
    report = {'format': 'rgwitness/experimental-v1',
            'input_snapshot': {'byte_length': len(data),
                               'sha256': hashlib.sha256(data).hexdigest()},
            'verdict': 'CONTRADICTED' if violations else 'INCOMPLETE' if gaps else 'CONSISTENT',
            'rows': metadata.num_rows, 'row_groups': metadata.num_row_groups,
            'checked_columns': len(results), 'violation_count': violations,
            'unchecked_count': gaps, 'columns': results, 'unchecked': unchecked,
            'scope': ('row-group bounds, null counts and supported float NaN counts only; no page/index/bloom audit'
                      if float_info is not None else
                      'row-group bounds and null counts only; no page/index/bloom audit')}
    if include_schema:
        from .schema import describe_schema
        report['schema'] = describe_schema(schema)
    return report
