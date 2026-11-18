"""Small helpers."""

from __future__ import annotations

import re

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_PART = re.compile(r"(\d+)\s*([smhd])")


def parse_duration(text: str) -> int:
    """Parse '90s', '15m', '1h', '2d' or '1h30m' into seconds."""
    cleaned = text.strip().lower()
    parts = _DURATION_PART.findall(cleaned)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != cleaned.replace(" ", ""):
        raise ValueError(f"Invalid duration {text!r}. Use e.g. 30m, 1h, 2d or 1h30m.")
    return sum(int(n) * _UNIT_SECONDS[u] for n, u in parts)
