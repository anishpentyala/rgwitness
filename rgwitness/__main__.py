import argparse
import json
import os
import sys
from .audit import audit_bytes
from .input import read_input
from .report import json_report


def _run(argv=None):
    parser = argparse.ArgumentParser(
        description='Check Parquet row-group bounds and null counts against unfiltered values.')
    parser.add_argument('file', help='one local regular Parquet file, at most 64 MiB')
    args = parser.parse_args(argv)
    try:
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
