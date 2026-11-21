"""Turn physical log lines into logical records.

The hard part of log analysis is that one event is often many lines: a Python
traceback or a Java stack trace is 10 to 50 lines, but it is *one* problem.
The assembler glues continuation lines onto the record they belong to and
strips the leading timestamp so it does not pollute clustering.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Iterator, Optional, Union

from .models import LogRecord, RawLine
from .formats import ALL_FORMATS, parse_line
from .levels import detect_level  # noqa: F401  (re-exported)
from .util import parse_timestamp  # noqa: F401  (re-exported)

# A line that starts with a timestamp begins a new record.
TS_PREFIX = re.compile(
    r"^\[?(?P<ts>\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\]?[\s,:|-]*"
)
# Classic syslog: "Nov 18 14:02:11 host app[123]: message" (no year, so no datetime).
SYSLOG_PREFIX = re.compile(r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2})\s+")

# Lines that belong to the previous record.
CONTINUATION = re.compile(
    r"^(?:\s+\S"  # indented: stack frames, code, wrapped text
    r"|Traceback \(most recent call last\)"
    r"|Caused by:"
    r"|\.\.\. \d+ more"
    r"|During handling of the above exception"
    r"|The above exception was the direct cause"
    r"|[A-Za-z_][\w$.]*(?:Exception|Error|Throwable)(?::|$)"  # exception line of a trace
    r")"
)

def _starts_record(text: str) -> bool:
    return bool(TS_PREFIX.match(text) or SYSLOG_PREFIX.match(text))


def _build(lines: list[str], raw_ts: Optional[datetime], source: str) -> LogRecord:
    first = lines[0]
    ts_text = None
    m = TS_PREFIX.match(first)
    if m:
        if raw_ts is None:
            ts_text = m.group("ts")  # parsed lazily, only if the record is kept
        first = first[m.end():]
    else:
        m = SYSLOG_PREFIX.match(first)
        if m:
            first = first[m.end():]
    message = "\n".join([first, *lines[1:]])
    return LogRecord(
        message=message,
        timestamp=raw_ts,
        level=detect_level(message),
        source=source,
        line_count=len(lines),
        ts_text=ts_text,
    )


class Assembler:
    """Incremental version of the assembler, for live streams.

    `feed()` takes one physical line and returns the records it completes: usually none,
    sometimes the previous record, and for a structured line (JSON, nginx, syslog) that
    line's own record too. `flush()` returns whatever is still open.
    """

    def __init__(self, source: str = "", max_lines: int = 200, formats: frozenset = ALL_FORMATS) -> None:
        self.source = source
        self.max_lines = max_lines
        self.formats = formats
        self._cur: list[str] = []
        self._cur_ts: Optional[datetime] = None
        self._in_py_traceback = False
        self._expect_exception_line = False

    @property
    def pending(self) -> bool:
        return bool(self._cur)

    def _emit(self) -> Optional[LogRecord]:
        if not self._cur:
            return None
        record = _build(self._cur, self._cur_ts, self.source)
        self._cur = []
        self._in_py_traceback = self._expect_exception_line = False
        return record

    def _start(self, text: str, ts: Optional[datetime]) -> None:
        self._cur, self._cur_ts = [text], ts
        self._in_py_traceback = self._expect_exception_line = False

    def feed(self, item: Union[RawLine, str]) -> tuple[LogRecord, ...]:
        raw = item if isinstance(item, RawLine) else RawLine(str(item))
        text = raw.text.rstrip("\r\n")
        if not text.strip():
            return ()

        if self.formats:
            parsed = parse_line(text, self.formats)
            if parsed is not None:  # a structured line is a complete record on its own
                done = self._emit()
                ts = parsed.ts or raw.ts
                record = LogRecord(
                    message=parsed.message, timestamp=ts, level=parsed.level, source=self.source,
                    ts_text=None if ts else parsed.ts_text, key=parsed.key, group=parsed.group,
                )
                return (done, record) if done else (record,)

        if _starts_record(text) or not self._cur:
            done = self._emit()
            self._start(text, raw.ts)
            return (done,) if done else ()
        if CONTINUATION.match(text):
            if len(self._cur) < self.max_lines:
                self._cur.append(text)
            if text.startswith("Traceback (most recent call last)"):
                self._in_py_traceback = True
                self._expect_exception_line = False
            elif self._in_py_traceback and text[0].isspace():
                self._expect_exception_line = True
            else:
                self._expect_exception_line = False
            return ()
        if self._in_py_traceback and self._expect_exception_line:
            # e.g. "KeyboardInterrupt" or "psycopg2.errors.UniqueViolation: ..."
            if len(self._cur) < self.max_lines:
                self._cur.append(text)
            self._in_py_traceback = self._expect_exception_line = False
            return ()
        done = self._emit()
        self._start(text, raw.ts)
        return (done,) if done else ()

    def flush(self) -> Optional[LogRecord]:
        return self._emit()


def assemble(
    lines: Iterable[Union[RawLine, str]],
    source: str = "",
    max_lines: int = 200,
    formats: frozenset = ALL_FORMATS,
) -> Iterator[LogRecord]:
    """Group physical lines into logical records.

    `max_lines` caps how many lines one record may keep, so a runaway trace
    cannot blow up memory.
    """
    assembler = Assembler(source, max_lines, formats)
    for item in lines:
        yield from assembler.feed(item)
    record = assembler.flush()
    if record is not None:
        yield record
