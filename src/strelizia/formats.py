"""Format adapters: understand structured log formats instead of guessing.

A plain-text heuristic sees an nginx access line as one long string full of noise (client
IP, browser, byte counts). An adapter knows its fields, so it can:

  * read the real severity (an HTTP 502 is an error, syslog priority 3 is an error)
  * read the real timestamp
  * build a clean *clustering key*, e.g. `nginx GET /api/orders/42 502`, so problems group
    by what actually went wrong instead of by who happened to hit them

Supported: JSON lines (pino, zap, structlog, logrus, journald `-o json`, and similar),
nginx access and error logs, and syslog (RFC 3164 and 5424). Detection is automatic and
per line, so mixed streams (access and error logs from one container) just work.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from .levels import detect_level

ALL_FORMATS = frozenset({"json", "nginx", "syslog"})
FORMAT_CHOICES = ("auto", "plain", "json", "nginx", "syslog")


def formats_for(choice: str) -> frozenset[str]:
    """Turn a --format value into the set of adapters to try."""
    if choice == "auto":
        return ALL_FORMATS
    if choice == "plain":
        return frozenset()
    if choice in ALL_FORMATS:
        return frozenset({choice})
    raise ValueError(f"Unknown format {choice!r}. Choose from: {', '.join(FORMAT_CHOICES)}.")


@dataclass
class Parsed:
    message: str  # what is shown and sampled (keeps useful detail)
    key: str  # what is clustered on (variable noise removed)
    level: str
    ts: Optional[datetime] = None
    ts_text: Optional[str] = None  # parsed lazily, only if the record is kept
    group: str = ""  # records in different groups are never merged into one pattern


# ---------------------------------------------------------------------- levels

_TEXT_LEVELS = {
    "trace": "DEBUG", "debug": "DEBUG", "dbg": "DEBUG", "verbose": "DEBUG",
    "info": "INFO", "information": "INFO", "notice": "INFO",
    "warn": "WARN", "warning": "WARN",
    "error": "ERROR", "err": "ERROR", "severe": "ERROR",
    "critical": "FATAL", "crit": "FATAL", "fatal": "FATAL", "panic": "FATAL",
    "emerg": "FATAL", "emergency": "FATAL", "alert": "FATAL",
}
_SYSLOG_SEVERITY = ["FATAL", "FATAL", "FATAL", "ERROR", "WARN", "INFO", "INFO", "DEBUG"]  # 0..7


def _level_from(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if v.isdigit():
            return _level_from(int(v))
        return _TEXT_LEVELS.get(v)
    if isinstance(value, (int, float)):
        n = int(value)
        if 0 <= n <= 7:  # syslog / journald PRIORITY scale
            return _SYSLOG_SEVERITY[n]
        # pino and bunyan scale: 10 trace, 20 debug, 30 info, 40 warn, 50 error, 60 fatal
        return "FATAL" if n >= 60 else "ERROR" if n >= 50 else "WARN" if n >= 40 else "INFO" if n >= 30 else "DEBUG"
    return None


# ---------------------------------------------------------------------- JSON

_LEVEL_KEYS = ("level", "lvl", "severity", "loglevel", "log_level", "levelname", "PRIORITY", "log.level")
_MSG_KEYS = ("msg", "message", "MESSAGE", "event", "log", "text", "short_message", "@message")
_TIME_KEYS = ("time", "timestamp", "ts", "@timestamp", "datetime", "asctime", "t", "__REALTIME_TIMESTAMP")
_ERR_KEYS = ("error", "err", "exception", "exc_info", "error.message")
_STACK_KEYS = ("stack", "stack_trace", "stacktrace", "traceback", "error.stack_trace")
_SKIP_EXTRA = set(_LEVEL_KEYS + _MSG_KEYS + _TIME_KEYS + _ERR_KEYS + _STACK_KEYS) | {"pid", "hostname", "v"}
MAX_EXTRA_FIELDS = 6


def _first(obj: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    return None


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and all(isinstance(b, int) and 0 <= b < 256 for b in value):
        return bytes(value).decode("utf-8", errors="replace")  # journald encodes binary messages as byte lists
    if isinstance(value, dict):  # e.g. pino serialises Error objects
        inner = _first(value, ("message", "msg", "type"))
        return _as_text(inner) if inner is not None else json.dumps(value, default=str)[:300]
    return str(value)


def _epoch(value: float) -> Optional[datetime]:
    """Epoch seconds, milliseconds (pino), microseconds (journald) or nanoseconds to a datetime."""
    v = float(value)
    if v > 1e17:  # nanoseconds
        v /= 1e9
    elif v > 1e14:  # microseconds
        v /= 1e6
    elif v > 1e11:  # milliseconds
        v /= 1e3
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _json(text: str) -> Optional[Parsed]:
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    raw_msg = _first(obj, _MSG_KEYS)
    explicit_level = _level_from(_first(obj, _LEVEL_KEYS))
    if raw_msg is None and explicit_level is None:
        return None  # valid JSON, but not a log record
    msg = " ".join(_as_text(raw_msg).split()) if raw_msg is not None else ""
    level = explicit_level or detect_level(msg)

    err = _first(obj, _ERR_KEYS)
    err_text = " ".join(_as_text(err).split())[:300] if err is not None else ""
    stack_raw = _first(obj, _STACK_KEYS)
    if stack_raw is None and isinstance(err, dict):
        stack_raw = _first(err, _STACK_KEYS)
    stack = _as_text(stack_raw) if stack_raw is not None else ""

    head = f"{level} {msg}".strip()
    extras = [
        f"{k}={_as_text(v)[:80]}"
        for k, v in obj.items()
        if k not in _SKIP_EXTRA and not k.startswith("_") and isinstance(v, (str, int, float)) and not isinstance(v, bool)
    ][:MAX_EXTRA_FIELDS]
    message = " ".join([head] + ([f"error={err_text}"] if err_text else []) + extras)
    if stack:
        message += "\n" + "\n".join(stack.splitlines()[:30])

    key = head + (f" | {err_text}" if err_text else "")
    top_frame = next((ln.strip() for ln in stack.splitlines() if ln.strip().startswith("at ")), None)
    if top_frame:
        key += " @ " + top_frame

    parsed = Parsed(message, key, level)
    t = _first(obj, _TIME_KEYS)
    if isinstance(t, str) and t.isdigit():
        t = int(t)
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        parsed.ts = _epoch(t)
    elif isinstance(t, str):
        parsed.ts_text = t
    return parsed


# ---------------------------------------------------------------------- nginx

_NGINX_ACCESS = re.compile(
    r'^\S+ \S+ \S+ \[(?P<time>\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})\] '
    r'"(?P<req>[^"]*)" (?P<status>\d{3}) \S+'
)
_NGINX_ERROR = re.compile(
    r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \[(?P<lvl>[a-z]+)\] \d+#\d+: (?:\*\d+ )?(?P<msg>.*)$"
)
_NGINX_ERR_FIELDS = re.compile(r", (?:client|server|request|upstream|host|referrer|subrequest): ")
_NGINX_UPSTREAM = re.compile(r'upstream: "([^"]*)"')
_NGINX_LEVELS = {"emerg": "FATAL", "alert": "FATAL", "crit": "FATAL", "error": "ERROR",
                 "warn": "WARN", "notice": "INFO", "info": "INFO", "debug": "DEBUG"}


def _nginx_access(text: str) -> Optional[Parsed]:
    m = _NGINX_ACCESS.match(text)
    if not m:
        return None
    status = int(m.group("status"))
    parts = m.group("req").split(" ")
    method, target = (parts[0], parts[1]) if len(parts) >= 2 else ("-", m.group("req") or "-")
    path = target.split("?", 1)[0]  # the query string is noise (and may hold secrets)
    level = "ERROR" if status >= 500 else "WARN" if status >= 400 else "INFO"
    return Parsed(text, f"nginx {method} {path} returned HTTP {status}", level, ts_text=m.group("time"), group=str(status))


def _nginx_error(text: str) -> Optional[Parsed]:
    m = _NGINX_ERROR.match(text)
    if not m:
        return None
    msg = m.group("msg")
    core = _NGINX_ERR_FIELDS.split(msg, 1)[0]
    key = f"nginx {core}"
    up = _NGINX_UPSTREAM.search(msg)
    if up:  # which upstream is failing is part of what went wrong
        host = urlparse(up.group(1)).netloc
        if host:
            key += f" upstream={host}"
    level = _NGINX_LEVELS.get(m.group("lvl"), "ERROR")
    return Parsed(text[len(m.group("ts")) + 1:], key, level, ts_text=m.group("ts"))


# ---------------------------------------------------------------------- syslog

_SYSLOG_3164 = re.compile(
    r"^(?:<(?P<pri>\d{1,3})>)?(?P<ts>[A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2}) \S+ "
    r"(?P<prog>[A-Za-z0-9_.\-/]+)(?:\[\d+\])?: ?(?P<msg>.*)$"
)
_SYSLOG_5424 = re.compile(
    r"^<(?P<pri>\d{1,3})>\d (?P<ts>\S+) \S+ (?P<prog>\S+) \S+ \S+ (?:-|\[[^\]]*\](?:\[[^\]]*\])*) ?(?P<msg>.*)$"
)
_MONTH_PREFIXES = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"}


def _syslog(text: str) -> Optional[Parsed]:
    m = _SYSLOG_5424.match(text) if text[:1] == "<" else None
    m = m or _SYSLOG_3164.match(text)
    if not m:
        return None
    msg = m.group("msg")
    pri = m.group("pri")
    level = _SYSLOG_SEVERITY[int(pri) % 8] if pri else detect_level(msg)
    body = f"{m.group('prog')}: {msg}"
    return Parsed(body, body, level, ts_text=m.group("ts"))


# ---------------------------------------------------------------------- entry point


def parse_line(text: str, allowed: frozenset[str] = ALL_FORMATS) -> Optional[Parsed]:
    """Return a parsed record if the line is in a known structured format, else None.

    Cheap prefilters run first so ordinary text lines cost almost nothing.
    """
    if not allowed:
        return None
    first = text[:1]
    if first == "{":
        return _json(text) if "json" in allowed else None
    if first == "<":
        return _syslog(text) if "syslog" in allowed else None
    if "nginx" in allowed:
        if first.isdigit() and text[4:5] == "/":
            parsed = _nginx_error(text)
            if parsed:
                return parsed
        if '] "' in text:
            parsed = _nginx_access(text)
            if parsed:
                return parsed
    if "syslog" in allowed and text[3:4] == " " and text[:3] in _MONTH_PREFIXES:
        return _syslog(text)
    return None
