"""Private worker entry point. Apply optional Linux limits before decoder imports."""
import os
import sys


def main():
    if len(sys.argv) not in (2, 3):
        return 2
    if len(sys.argv) == 3:
        try:
            if sys.platform != 'linux':
                return 2
            import resource
            limit = int(sys.argv[2]) * 1024 * 1024
            if limit <= 0:
                return 2
            _, hard = resource.getrlimit(resource.RLIMIT_AS)
            if hard != resource.RLIM_INFINITY:
                limit = min(limit, hard)
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except (ImportError, ValueError, OverflowError, OSError):
            return 2
    # Avoid a crash creating an unbounded core dump. No resource claim beyond
    # these process-local limits is implied, and inherited access is unchanged.
    if os.name == 'posix':
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    from .__main__ import entrypoint
    sys.argv = [sys.argv[0], '--', sys.argv[1]]
    return entrypoint()


if __name__ == '__main__':
    sys.exit(main())
