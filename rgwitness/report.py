"""Stable, lossless scalar representations for JSON reports."""
import datetime
import decimal


def scalar(value):
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return {'kind': 'integer', 'value': str(value)}
    if isinstance(value, decimal.Decimal):
        return {'kind': 'decimal', 'value': str(value)}
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return {'kind': type(value).__name__, 'value': value.isoformat()}
    if isinstance(value, bytes):
        return {'kind': 'bytes', 'hex': value.hex()}
    raise TypeError('unsupported report scalar')


def json_report(report):
    # Copy instead of modifying the numeric evidence used by comparisons.
    result = dict(report)
    result['columns'] = []
    for column in report['columns']:
        item = dict(column)
        for side in ('claim', 'observed'):
            item[side] = dict(item[side])
            for bound in ('lower', 'upper'):
                item[side][bound] = scalar(item[side][bound])
        result['columns'].append(item)
    return result
