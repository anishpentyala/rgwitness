"""Bounded local snapshots. The decoder is not a hostile-file sandbox."""
import os
import stat
from .audit import MAX_BYTES


def read_input(path: str) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('input must be a regular file')
        with os.fdopen(fd, 'rb') as handle:
            fd = -1
            data = handle.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError('input exceeds 64 MiB')
        return data
    finally:
        if fd >= 0:
            os.close(fd)
