"""Read logs from a file or from stdin."""

from __future__ import annotations

import sys
from typing import Iterator

from ..models import RawLine
from . import SourceError


def read_file(path: str) -> Iterator[RawLine]:
    """Yield lines from `path`, or from stdin when `path` is '-'."""
    if path == "-":
        for line in sys.stdin:
            yield RawLine(line)
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                yield RawLine(line)
    except FileNotFoundError:
        raise SourceError(f"File not found: {path}") from None
    except IsADirectoryError:
        raise SourceError(f"{path} is a directory, not a log file") from None
    except PermissionError:
        raise SourceError(f"Permission denied reading {path}") from None
