"""Small helpers."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_PART = re.compile(r"(\d+)\s*([smhd])")


def parse_duration(text: str) -> int:
    """Parse '90s', '15m', '1h', '2d' or '1h30m' into seconds."""
    cleaned = text.strip().lower()
    parts = _DURATION_PART.findall(cleaned)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != cleaned.replace(" ", ""):
        raise ValueError(f"Invalid duration {text!r}. Use e.g. 30m, 1h, 2d or 1h30m.")
    return sum(int(n) * _UNIT_SECONDS[u] for n, u in parts)


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
