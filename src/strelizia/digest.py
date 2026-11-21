"""The daily digest: what happened in the last day, in one message.

Doubles as a heartbeat: it is sent even on a quiet day, so you know the watcher is alive.
"""

from __future__ import annotations

from .models import level_rank
from .notify import Message
from .redact import Redactor
from .store import Store

_KIND_WORD = {"NEW": "new", "SPIKE": "spike", "STILL": "still happening", "STARTUP": "startup", "RESTART": "restart", "RESOLVED": "resolved"}


def build_digest(store: Store, now: float, hours: float = 24.0, top: int = 5) -> Message:
    since = now - hours * 3600
    rows = store.digest_rows(since, now)
    alerts = store.alert_counts(since, now)
    redactor = Redactor()
    title = f"Strelizia daily digest · last {hours:g}h"

    by_source: dict[str, list] = {}
    for pattern, n in rows:
        by_source.setdefault(pattern.source, []).append((pattern, n))
    for source, _kind in alerts:
        by_source.setdefault(source, [])

    if not by_source:
        return Message(title, "✅ Quiet period: no warnings or errors, and no alerts.", "INFO")

    lines: list[str] = []
    worst = "INFO"
    for source in sorted(by_source):
        items = by_source[source]
        total = sum(n for _, n in items)
        kinds = {k: n for (s, k), n in alerts.items() if s == source}
        raised = sum(kinds.values())
        head = f"{source}: {total:,} warnings and errors in {len(items)} pattern{'s' if len(items) != 1 else ''}"
        if raised:
            detail = ", ".join(f"{n} {_KIND_WORD.get(k, k.lower())}" for k, n in sorted(kinds.items()))
            head += f" · {raised} alert{'s' if raised != 1 else ''} ({detail})"
        lines.append(head)
        for pattern, n in items[:top]:
            fresh = " (new)" if pattern.first_seen > since else ""
            lines.append(f"  {n:,}×  {pattern.level}  {redactor.redact(pattern.template)}{fresh}")
            if level_rank(pattern.level) > level_rank(worst):
                worst = pattern.level
        if len(items) > top:
            lines.append(f"  … and {len(items) - top} more patterns")
        lines.append("")
    return Message(title, "\n".join(lines).rstrip(), worst)
