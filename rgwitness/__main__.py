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
    parser.add_argument('file', help='one local regular Parquet file, at most 64 MiB')
    parser.add_argument('--isolate', action='store_true',
                        help='audit in a child process (not a security sandbox)')
    parser.add_argument('--timeout', type=float, default=None,
                        help='worker wall-clock seconds; default 30 with --isolate')
    parser.add_argument('--memory-mib', type=int, default=None,
                        help='optional Linux worker address-space limit, in MiB')
    args = parser.parse_args(argv)
    if (args.timeout is not None or args.memory_mib is not None) and not args.isolate:
        parser.error('--timeout and --memory-mib require --isolate')
    if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0):
        parser.error('--timeout must be finite and positive')
    if args.memory_mib is not None and (args.memory_mib <= 0 or sys.platform != 'linux'):
        parser.error('--memory-mib requires Linux and a positive MiB count')
    try:
        if args.isolate:
            from .worker import audit_in_worker
            report = audit_in_worker(args.file, timeout=args.timeout or 30.0,
                                     memory_mib=args.memory_mib)
        else:
            data = read_input(args.file)
            report = json_report(audit_bytes(data))
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
