"""Private prototype for Parquet TYPE_ORDER floats only.

NaNs are counted separately, signed zeros compare equal, and missing counts
remain coverage gaps. IEEE total order is deliberately not implemented here.
"""
import math
import struct


def compare_float(claim, chunks, *, width, limit):
    if width not in (32, 64):
        raise ValueError('unsupported float storage width')
    code = '<f' if width == 32 else '<d'
    bounds = []
    for key in ('lower', 'upper'):
        raw = claim[key]
        if raw is not None and len(raw) != width // 8:
            raise ValueError('invalid float statistics width')
        bounds.append(None if raw is None else struct.unpack(code, raw)[0])
    rows = nulls = nans = 0
    lower = upper = None
    for chunk in chunks:
        for value in chunk:
            rows += 1
            if rows > limit:
                raise ValueError('decoded float row budget exceeded')
            if value is None:
                nulls += 1
            elif math.isnan(value):
                nans += 1
            elif lower is None:
                lower = upper = value
            else:
                lower = min(lower, value)
                upper = max(upper, value)
    violations, unchecked = [], []
    if rows != claim['num_values']:
        violations.append('row_count')
    for key, actual in (('null_count', nulls), ('nan_count', nans)):
        recorded = claim[key]
        if recorded is None:
            unchecked.append(key + '_missing')
        elif recorded != actual:
            violations.append(key)
    lo, hi = bounds
    valid_lo = lo is not None and not math.isnan(lo)
    valid_hi = hi is not None and not math.isnan(hi)
    if valid_lo and valid_hi and lo > hi:
        violations.append('reversed_bounds')
    if lower is None:
        if valid_lo or valid_hi:
            violations.append('bounds_without_non_nan_values')
    else:
        if valid_lo:
            if lo > lower:
                violations.append('lower_excludes_value')
        else:
            unchecked.append('lower_bound_missing_or_nan')
        if valid_hi:
            if hi < upper:
                violations.append('upper_excludes_value')
        else:
            unchecked.append('upper_bound_missing_or_nan')
    # NaN bounds have no comparison meaning under TYPE_ORDER, even in a group
    # containing only NaNs. Keep their presence as an explicit coverage gap.
    if (lo is not None and math.isnan(lo)) or (hi is not None and math.isnan(hi)):
        unchecked.append('nan_bounds_ignored')
    return {'claim': {'rows': claim['num_values'], 'nulls': claim['null_count'],
                      'nans': claim['nan_count'],
                      'has_bounds': lo is not None and hi is not None,
                      'lower': lo, 'upper': hi,
                      'lower_raw_hex': None if claim['lower'] is None else claim['lower'].hex(),
                      'upper_raw_hex': None if claim['upper'] is None else claim['upper'].hex()},
            'observed': {'rows': rows, 'nulls': nulls, 'nans': nans,
                         'lower': lower, 'upper': upper},
            'violations': violations, 'unchecked': unchecked,
            'comparison': {'order': 'TYPE_ORDER', 'nan_comparison': 'excluded',
                           'signed_zero': 'equivalent', 'physical_width': width}}
