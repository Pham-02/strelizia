"""Small helpers."""

from __future__ import annotations

import re
from datetime import datetime, time
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
        return None
