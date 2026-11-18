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

_LEVEL_WORD = re.compile(
    r"\b(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|SEVERE|CRITICAL|CRIT|FATAL|PANIC|EMERG|ALERT)\b"
)
_NGINX_LEVEL = re.compile(r"\[(emerg|alert|crit|error|warn|notice|info|debug)\]", re.I)
_HTTP_STATUS = re.compile(r'"\s(\d{3})\s')
_ERROR_HINT = re.compile(
    r"\b(traceback|exception|error|failed|failure|refused|timed? ?out|timeout|panic|"
    r"segfault|fatal|oom|out of memory|killed|denied|unreachable)\b",
    re.I,
)
_WARN_HINT = re.compile(r"\b(warn(?:ing)?|deprecated|retry(?:ing)?|slow)\b", re.I)

_LEVEL_MAP = {
    "TRACE": "DEBUG", "DEBUG": "DEBUG",
    "INFO": "INFO", "NOTICE": "INFO",
    "WARN": "WARN", "WARNING": "WARN",
    "ERROR": "ERROR", "ERR": "ERROR", "SEVERE": "ERROR",
    "CRITICAL": "FATAL", "CRIT": "FATAL", "FATAL": "FATAL",
    "PANIC": "FATAL", "EMERG": "FATAL", "ALERT": "FATAL",
}


def detect_level(message: str) -> str:
    """Best-effort severity. Always returns one of DEBUG/INFO/WARN/ERROR/FATAL."""
    first = message.split("\n", 1)[0]
    head = first[:160]

    m = _LEVEL_WORD.search(head)
    if m:
        return _LEVEL_MAP[m.group(1)]
    m = _NGINX_LEVEL.search(head)
    if m:
        return _LEVEL_MAP[m.group(1).upper()]
    m = _HTTP_STATUS.search(first)
    if m:
        status = int(m.group(1))
        if status >= 500:
            return "ERROR"
        if status >= 400:
            return "WARN"
        return "INFO"
    if "Traceback (most recent call last)" in message:
        return "ERROR"
    if _ERROR_HINT.search(first):
        return "ERROR"
    if _WARN_HINT.search(first):
        return "WARN"
    return "INFO"


def parse_timestamp(text: str) -> Optional[datetime]:
    """Parse the timestamp styles we strip from line prefixes. Returns None on failure."""
    s = text.strip().replace(",", ".")
    s = re.sub(r"^(\d{4})/(\d{2})/(\d{2})", r"\1-\2-\3", s)
    s = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _starts_record(text: str) -> bool:
    return bool(TS_PREFIX.match(text) or SYSLOG_PREFIX.match(text))


def _build(lines: list[str], raw_ts: Optional[datetime], source: str) -> LogRecord:
    first = lines[0]
    ts = raw_ts
    m = TS_PREFIX.match(first)
    if m:
        ts = ts or parse_timestamp(m.group("ts"))
        first = first[m.end():]
    else:
        m = SYSLOG_PREFIX.match(first)
        if m:
            first = first[m.end():]
    message = "\n".join([first, *lines[1:]])
    return LogRecord(
        message=message,
        timestamp=ts,
        level=detect_level(message),
        source=source,
        line_count=len(lines),
    )


def assemble(
    lines: Iterable[Union[RawLine, str]],
    source: str = "",
    max_lines: int = 200,
) -> Iterator[LogRecord]:
    """Group physical lines into logical records.

    `max_lines` caps how many lines one record may keep, so a runaway trace
    cannot blow up memory.
    """
    cur: list[str] = []
    cur_ts: Optional[datetime] = None
    in_py_traceback = False
    expect_exception_line = False

    for item in lines:
        raw = item if isinstance(item, RawLine) else RawLine(str(item))
        text = raw.text.rstrip("\r\n")
        if not text.strip():
            continue

        if _starts_record(text) or not cur:
            if cur:
                yield _build(cur, cur_ts, source)
            cur, cur_ts = [text], raw.ts
            in_py_traceback = expect_exception_line = False
        elif CONTINUATION.match(text):
            if len(cur) < max_lines:
                cur.append(text)
            if text.startswith("Traceback (most recent call last)"):
                in_py_traceback = True
                expect_exception_line = False
            elif in_py_traceback and text[0].isspace():
                expect_exception_line = True
            else:
                expect_exception_line = False
        elif in_py_traceback and expect_exception_line:
            # e.g. "KeyboardInterrupt" or "psycopg2.errors.UniqueViolation: ..."
            if len(cur) < max_lines:
                cur.append(text)
            in_py_traceback = expect_exception_line = False
        else:
            yield _build(cur, cur_ts, source)
            cur, cur_ts = [text], raw.ts
            in_py_traceback = expect_exception_line = False

    if cur:
        yield _build(cur, cur_ts, source)
