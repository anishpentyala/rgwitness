"""Sequential, explicit-file manifests. Each member remains a separate audit."""
import math
import os
import sys
from itertools import islice

from .audit import audit_bytes
from .input import read_input
from .report import json_report

MAX_FILES = 32
VERDICTS = ('CONSISTENT', 'CONTRADICTED', 'INVALID', 'INCOMPLETE')


def audit_files(paths, *, isolate=False, timeout=None, memory_mib=None,
                require_schema_match=False):
    """Audit 1..32 explicit paths in order, preserving failures and duplicates.

    Paths are included as supplied, without resolving or expanding them. This
    manifest is not an atomic dataset snapshot. Schema equality is opt-in.
    Worker limits, when selected, apply separately to each input.
    """
    if not isinstance(require_schema_match, bool):
        raise ValueError('require_schema_match must be a boolean')
    if isinstance(paths, (str, bytes, os.PathLike)):
        raise ValueError('expected an iterable of file paths, not one path')
    paths = list(islice(paths, MAX_FILES + 1))
    if not paths or len(paths) > MAX_FILES:
        raise ValueError('batch requires 1 to 32 explicit file paths')
    paths = [os.fspath(path) for path in paths]
    if any(not isinstance(path, str) for path in paths):
        raise ValueError('batch paths must be strings or string-valued path objects')
    if (timeout is not None or memory_mib is not None) and not isolate:
        raise ValueError('timeout and memory budget require isolation')
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('timeout must be finite and positive')
    if memory_mib is not None and (isinstance(memory_mib, bool)
            or not isinstance(memory_mib, int) or memory_mib <= 0 or sys.platform != 'linux'):
        raise ValueError('memory budget requires Linux and a positive integer MiB count')
    entries = []
    counts = dict.fromkeys(VERDICTS, 0)
    for position, path in enumerate(paths, start=1):
        try:
            if isolate:
                from .worker import audit_in_worker
                options = {'include_schema': True} if require_schema_match else {}
                report = audit_in_worker(path, timeout=30.0 if timeout is None else timeout,
                                         memory_mib=memory_mib, **options)
            else:
                options = {'include_schema': True} if require_schema_match else {}
                report = json_report(audit_bytes(read_input(path), **options))
        except Exception as exc:
            report = {'verdict': 'INVALID', 'error': str(exc)}
        counts[report['verdict']] += 1
        entries.append({'position': position, 'path': path, 'report': report})
    verdict = next((v for v in ('INVALID', 'CONTRADICTED', 'INCOMPLETE') if counts[v]),
                   'CONSISTENT')
    result = {'format': 'rgwitness/batch-v1', 'verdict': verdict,
            'file_count': len(entries), 'verdict_counts': counts, 'files': entries,
            'scope': 'separate file audits, not an atomic snapshot or cross-file schema audit'}
    if require_schema_match:
        from .schema import compare_schemas
        result['schema_check'] = compare_schemas(entries)
        schema_verdict = result['schema_check']['verdict']
        priority = {'CONSISTENT': 0, 'INCOMPLETE': 1, 'CONTRADICTED': 2, 'INVALID': 3}
        result['verdict'] = max((verdict, schema_verdict), key=priority.__getitem__)
        result['scope'] = ('separate file audits with an exact interpreted schema contract; '
                           'not an atomic snapshot or schema evolution compatibility check')
    return result
