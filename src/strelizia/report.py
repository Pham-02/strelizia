"""Pretty terminal output (Rich) and JSON output."""

from __future__ import annotations

from typing import Optional

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from .models import Cluster
from .pipeline import Stats

_STYLE = {
    "FATAL": ("🔴", "bold red", "red"),
    "ERROR": ("🔴", "red", "red"),
    "WARN": ("🟡", "yellow", "yellow"),
    "INFO": ("🔵", "cyan", "cyan"),
    "DEBUG": ("⚪", "dim", "dim"),
}


def _fmt_time(cluster: Cluster) -> Optional[str]:
    a, b = cluster.first_seen, cluster.last_seen
    if a is None or b is None:
        return None
    fmt = "%Y-%m-%d %H:%M:%S"
    first = a.strftime(fmt)
    last = b.strftime(fmt)
    if first == last:
        return first
    return f"{first}  →  {last}"


def _clip(sample: str, max_lines: int = 8, max_width: int = 160) -> str:
    lines = sample.splitlines()
    shown = [ln if len(ln) <= max_width else ln[: max_width - 1] + "…" for ln in lines[:max_lines]]
    if len(lines) > max_lines:
        shown.append(f"… ({len(lines) - max_lines} more lines)")
    return "\n".join(shown)


def render_report(console: Console, clusters: list[Cluster], stats: Stats, top: int, source: str) -> None:
    shown = clusters[:top]

    header = Text()
    header.append("Strelizia", style="bold magenta")
    header.append(f"  ·  {source}\n", style="dim")
    header.append(
        f"{stats.lines:,} lines → {stats.records:,} records → "
        f"{stats.clusters:,} patterns",
        style="bold",
    )
    if stats.skipped:
        header.append(f"  ({stats.skipped:,} below the level filter were ignored)", style="dim")
    console.print(Panel(header, border_style="magenta"))

    if not shown:
        console.print(Text("No warnings or errors found. Nothing to worry about. 🌺", style="green"))
        return

    for rank, cluster in enumerate(shown, start=1):
        icon, text_style, border = _STYLE.get(cluster.level, _STYLE["INFO"])

        title = Text()
        title.append(f"{icon} #{rank}  ", style="bold")
        title.append(cluster.level, style=text_style)
        title.append(f"  ·  {cluster.count:,}×", style="bold")

        body: list[Text] = []
        pattern = Text()
        pattern.append("Pattern  ", style="bold")
        pattern.append(cluster.template)
        body.append(pattern)

        when = _fmt_time(cluster)
        if when:
            seen = Text()
            seen.append("Seen     ", style="bold")
            seen.append(when, style="dim")
            body.append(seen)

        for i, sample in enumerate(cluster.samples, start=1):
            label = Text(f"\nSample {i}" if len(cluster.samples) > 1 else "\nSample", style="bold")
            body.append(label)
            body.append(Text(_clip(sample), style="dim"))

        console.print(Panel(Group(*body), title=title, title_align="left", border_style=border))

    hidden = len(clusters) - len(shown)
    if hidden > 0:
        console.print(Text(f"… and {hidden:,} more patterns. Use --top to show more.", style="dim"))


def clusters_to_json(clusters: list[Cluster], stats: Stats, top: int) -> dict:
    return {
        "stats": {
            "lines": stats.lines,
            "records": stats.records,
            "skipped": stats.skipped,
            "clusters": stats.clusters,
        },
        "clusters": [
            {
                "id": c.id,
                "level": c.level,
                "count": c.count,
                "template": c.template,
                "first_seen": c.first_seen.isoformat() if c.first_seen else None,
                "last_seen": c.last_seen.isoformat() if c.last_seen else None,
                "samples": c.samples,
            }
            for c in clusters[:top]
        ],
    }
