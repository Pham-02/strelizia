"""Small helpers."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import Optional

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd])")


def parse_duration(text: str) -> float:
    """Parse '90s', '15m', '1h', '2d', '1h30m' or '0.5s' into seconds (an int when whole)."""
    cleaned = text.strip().lower().replace(" ", "")
    parts = _DURATION_PART.findall(cleaned)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != cleaned:
        raise ValueError(f"Invalid duration {text!r}. Use e.g. 30s, 5m, 1h, 2d or 1h30m.")
    total = sum(float(n) * _UNIT_SECONDS[u] for n, u in parts)
    return int(total) if total == int(total) else total


def parse_clock(text: str) -> time:
    """Parse '09:00' into a local time of day."""
    try:
        return datetime.strptime(text.strip(), "%H:%M").time()
    except ValueError:
        raise ValueError(f"Invalid time {text!r}. Use HH:MM, e.g. 09:00.") from None


def parse_quiet_hours(text: str) -> tuple[time, time]:
    """Parse '23:00-07:00' into a (start, end) pair of local times."""
    try:
        start_s, end_s = text.strip().split("-")
        start = datetime.strptime(start_s.strip(), "%H:%M").time()
        end = datetime.strptime(end_s.strip(), "%H:%M").time()
    except ValueError:
        raise ValueError(f"Invalid quiet hours {text!r}. Use HH:MM-HH:MM, e.g. 23:00-07:00.") from None
    return start, end


def in_quiet_hours(window: tuple[time, time], now: datetime) -> bool:
    """True if `now` falls inside the window (which may wrap past midnight)."""
    start, end = window
    t = now.time()
    if start == end:
        return False
    return start <= t < end if start < end else (t >= start or t < end)


_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_NGINX_TS = re.compile(r"^(\d{2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})$")
_SYSLOG_TS = re.compile(r"^([A-Z][a-z]{2}) +(\d{1,2}) (\d{2}):(\d{2}):(\d{2})$")


def _parse_special(s: str) -> Optional[datetime]:
    """nginx access-log time ("18/Nov/2026:14:02:11 +0000") and syslog time ("Nov 18 14:02:11")."""
    m = _NGINX_TS.match(s)
    if m and m.group(2) in _MONTHS:
        offset = timedelta(hours=int(m.group(8)), minutes=int(m.group(9)))
        tz = timezone(offset if m.group(7) == "+" else -offset)
        return datetime(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)), int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=tz)
    m = _SYSLOG_TS.match(s)
    if m and m.group(1) in _MONTHS:
        now = datetime.now()  # syslog has no year: assume this year, or last year if that would be in the future
        try:
            dt = datetime(now.year, _MONTHS[m.group(1)], int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)))
        except ValueError:
            return None
        return dt.replace(year=now.year - 1) if dt - now > timedelta(days=1) else dt
    return None


def parse_timestamp(text: str) -> Optional[datetime]:
    """Parse the timestamp styles we strip from line prefixes. Returns None on failure."""
    s = text.strip()
    try:  # fast path: plain ISO-style timestamps, the common case
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    s = s.replace(",", ".")
    s = re.sub(r"^(\d{4})/(\d{2})/(\d{2})", r"\1-\2-\3", s)
    s = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return _parse_special(text.strip())
