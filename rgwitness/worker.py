"""Opt-in subprocess budgets. This is not a security sandbox."""
import json
import math
import subprocess
import sys


CODES = {'CONSISTENT': 0, 'CONTRADICTED': 1, 'INVALID': 2, 'INCOMPLETE': 3}


def audit_in_worker(path, *, timeout=30.0, memory_mib=None):
    """Return a complete worker report or INVALID, never partial evidence."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be finite and positive')
    if memory_mib is not None:
        if isinstance(memory_mib, bool) or not isinstance(memory_mib, int) or memory_mib <= 0:
            raise ValueError('memory budget must be a positive integer MiB count')
        if sys.platform != 'linux':
            raise ValueError('memory budgets are supported only on Linux')
    command = [sys.executable, '-m', 'rgwitness._worker', str(path)]
    if memory_mib is not None:
        command.append(str(memory_mib))
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {'verdict': 'INVALID', 'error': 'audit worker exceeded wall-clock timeout'}
    except OSError:
        return {'verdict': 'INVALID', 'error': 'could not start audit worker'}
    if result.returncode not in CODES.values():
        return {'verdict': 'INVALID', 'error': 'audit worker exited without a complete report'}
    try:
        report = json.loads(result.stdout)
        if (not isinstance(report, dict) or report.get('verdict') not in CODES
                or CODES[report['verdict']] != result.returncode):
            raise ValueError('worker status mismatch')
    except (ValueError, TypeError, UnicodeError):
        return {'verdict': 'INVALID', 'error': 'audit worker returned an invalid report'}
    return report
