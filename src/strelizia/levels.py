"""Guess a severity level from the text of a log record."""

from __future__ import annotations

import re

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
