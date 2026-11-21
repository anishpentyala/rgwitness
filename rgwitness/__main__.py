import argparse
import json
import math
import os
import sys
from .audit import audit_bytes
from .input import read_input
from .report import json_report


def _run(argv=None):
    parser = argparse.ArgumentParser(
        description='Check Parquet row-group bounds and null counts against unfiltered values.')
    parser.add_argument('file', nargs='+', help='local regular Parquet files, at most 64 MiB each')
    parser.add_argument('--batch', action='store_true',
                        help='audit 1 to 32 explicit files and emit a combined manifest')
    parser.add_argument('--require-schema-match', action='store_true',
                        help='require exact interpreted field schema equality across a batch')
    parser.add_argument('--include-schema', action='store_true',
                        help='include interpreted field schema in a single-file report')
    parser.add_argument('--isolate', action='store_true',
                        help='audit in a child process (not a security sandbox)')
    parser.add_argument('--timeout', type=float, default=None,
                        help='worker wall-clock seconds; default 30 with --isolate')
    parser.add_argument('--memory-mib', type=int, default=None,
                        help='optional Linux worker address-space limit, in MiB')
    args = parser.parse_args(argv)
    if args.require_schema_match and not args.batch:
        parser.error('--require-schema-match requires --batch')
    if args.include_schema and args.batch:
        parser.error('--include-schema is for single files; use --require-schema-match with --batch')
    if not args.batch and len(args.file) != 1:
        parser.error('multiple files require --batch')
    if args.batch and len(args.file) > 32:
        parser.error('batch requires 1 to 32 explicit file paths')
    if (args.timeout is not None or args.memory_mib is not None) and not args.isolate:
        parser.error('--timeout and --memory-mib require --isolate')
    if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0):
        parser.error('--timeout must be finite and positive')
    if args.memory_mib is not None and (args.memory_mib <= 0 or sys.platform != 'linux'):
        parser.error('--memory-mib requires Linux and a positive MiB count')
    try:
        if args.batch:
            from .batch import audit_files
            report = audit_files(args.file, isolate=args.isolate, timeout=args.timeout,
                                 memory_mib=args.memory_mib,
                                 require_schema_match=args.require_schema_match)
        elif args.isolate:
            from .worker import audit_in_worker
            report = audit_in_worker(args.file[0], timeout=args.timeout or 30.0,
                                     memory_mib=args.memory_mib, include_schema=args.include_schema)
        else:
            data = read_input(args.file[0])
            options = {'include_schema': True} if args.include_schema else {}
            report = json_report(audit_bytes(data, **options))
    except Exception as exc:
        # Decoder errors are input failures, not a report with passing columns.
        report = {'verdict': 'INVALID', 'error': str(exc)}
    code = {'CONSISTENT': 0, 'CONTRADICTED': 1, 'INVALID': 2,
            'INCOMPLETE': 3}[report['verdict']]
    payload = json.dumps(report, ensure_ascii=True, allow_nan=False, sort_keys=True) + '\n'
    try:
        written = sys.stdout.write(payload)
        if written != len(payload):
            return 2, True
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        return 2, True
    return code, False


def main(argv=None):
    """Return an exit code without replacing a caller's output stream."""
    return _run(argv)[0]


def entrypoint():
    """Prevent a failed buffered stdout from being retried at shutdown."""
    code, failed = _run()
    if failed:
        try:
            fd = sys.stdout.fileno()
            sink = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(sink, fd)
            finally:
                if sink != fd:
                    os.close(sink)
        except (OSError, ValueError, AttributeError):
            sys.stdout = open(os.devnull, 'w')
    return code


if __name__ == '__main__':
    sys.exit(entrypoint())
