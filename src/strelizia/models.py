"""Core data types shared across Strelizia."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import NamedTuple, Optional

from .util import parse_timestamp

LEVELS = ["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
LEVEL_RANK = {name: i + 1 for i, name in enumerate(LEVELS)}


def level_rank(level: Optional[str]) -> int:
    """Numeric severity. Unknown levels count as INFO."""
    return LEVEL_RANK.get((level or "INFO").upper(), LEVEL_RANK["INFO"])


def as_utc(dt: datetime) -> datetime:
    """Make datetimes comparable: naive values are assumed to be UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class RawLine(NamedTuple):
    """One physical line from a source, optionally with a source-provided timestamp."""

    text: str
    ts: Optional[datetime] = None


_PY_FRAME = re.compile(r'^\s*File ".*", line \d+, in (\S+)')


@dataclass
class LogRecord:
    """One logical log entry (a stack trace is a single record, not 30 lines)."""

    message: str  # full, possibly multi-line, with the leading timestamp removed
    timestamp: Optional[datetime] = None
    level: str = "INFO"
    source: str = ""
    line_count: int = 1
    ts_text: Optional[str] = field(default=None, repr=False)  # unparsed; see resolve_timestamp

    def resolve_timestamp(self) -> Optional[datetime]:
        """Parse the timestamp on first use. Most records are filtered out, so this saves work."""
        if self.timestamp is None and self.ts_text is not None:
            self.timestamp = parse_timestamp(self.ts_text)
            self.ts_text = None
        return self.timestamp

    @property
    def signature(self) -> str:
        """The text used for clustering.

        For single lines this is the line itself. For multi-line records we keep
        the parts that identify *what* failed and *where*, so that thousands of
        traces from the same bug collapse together while different bugs do not.
        """
        lines = [ln for ln in self.message.splitlines() if ln.strip()]
        if not lines:
            return ""
        if len(lines) == 1:
            return lines[0].strip()

        first = lines[0].strip()
        if "Traceback (most recent call last)" in self.message:
            parts = [] if first.startswith("Traceback") else [first]
            parts.append(lines[-1].strip())
            frame = next(
                (m.group(1) for ln in reversed(lines) if (m := _PY_FRAME.match(ln))),
                None,
            )
            sig = " | ".join(parts)
            return f"{sig} @ {frame}" if frame else sig

        sig = first
        # The exception line (e.g. "java.lang.NullPointerException: ...") says *what* failed.
        exc = next(
            (
                ln.strip()
                for ln in lines[1:]
                if not ln[0].isspace() and not ln.startswith(("Caused by:", "..."))
            ),
            None,
        )
        if exc:
            sig += " | " + exc
        caused = [ln.strip() for ln in lines if ln.lstrip().startswith("Caused by:")]
        if caused:
            sig += " | " + caused[-1]
        top_frame = next((ln.strip() for ln in lines if ln.lstrip().startswith("at ")), None)
        if top_frame:
            sig += " @ " + top_frame
        return sig


@dataclass
class Cluster:
    """A group of log records that share one pattern (a probable root cause)."""

    id: int
    template: str
    count: int = 0
    level: str = "INFO"  # worst level seen in this cluster
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    samples: list[str] = field(default_factory=list)
    context_before: list[str] = field(default_factory=list)  # records just before it first appeared
    _first_key: Optional[datetime] = field(default=None, repr=False, compare=False)
    _last_key: Optional[datetime] = field(default=None, repr=False, compare=False)

    def observe(self, record: LogRecord, max_samples: int) -> None:
        self.count += 1
        if level_rank(record.level) > level_rank(self.level):
            self.level = record.level
        ts = record.resolve_timestamp()
        if ts is not None:
            key = as_utc(ts)  # converted once, compared against cached keys
            if self._first_key is None or key < self._first_key:
                self._first_key, self.first_seen = key, ts
            if self._last_key is None or key > self._last_key:
                self._last_key, self.last_seen = key, ts
        if len(self.samples) < max_samples and record.message not in self.samples:
            self.samples.append(record.message[:1500])
