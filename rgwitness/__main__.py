import argparse
import json
import sys
from .audit import audit_bytes
from .input import read_input
from .report import json_report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Check Parquet row-group bounds and null counts against unfiltered values.')
    parser.add_argument('file', help='one local regular Parquet file, at most 64 MiB')
    args = parser.parse_args(argv)
    try:
        data = read_input(args.file)
        report = json_report(audit_bytes(data))
    except Exception as exc:
        # Decoder errors are input failures, not a report with passing columns.
        print(json.dumps({'verdict': 'INVALID', 'error': str(exc)}, ensure_ascii=True))
        return 2
    print(json.dumps(report, ensure_ascii=True, allow_nan=False, sort_keys=True))
    return {'CONSISTENT': 0, 'CONTRADICTED': 1, 'INCOMPLETE': 3}[report['verdict']]


if __name__ == '__main__':
    sys.exit(main())
