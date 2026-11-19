"""Pretty terminal output (Rich) and JSON output."""

from __future__ import annotations

from typing import Optional

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from .explain import Explanation, Payload
from .llm import LLMConfig, Request
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


def render_report(
    console: Console,
    clusters: list[Cluster],
    stats: Stats,
    top: int,
    source: str,
    explanation: Optional[Explanation] = None,
) -> None:
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

    if explanation and explanation.overall:
        console.print(
            Panel(
                Text(explanation.overall),
                title=Text("🧠 Overall diagnosis", style="bold magenta"),
                title_align="left",
                subtitle=Text(f"{explanation.provider} · {explanation.model} · a hypothesis, not a fact", style="dim"),
                subtitle_align="right",
                border_style="magenta",
            )
        )

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

        diag = explanation.patterns.get(rank) if explanation else None
        if diag:
            body.extend(_diagnosis_lines(diag))

        console.print(Panel(Group(*body), title=title, title_align="left", border_style=border))

    hidden = len(clusters) - len(shown)
    if hidden > 0:
        console.print(Text(f"… and {hidden:,} more patterns. Use --top to show more.", style="dim"))


def _diagnosis_lines(diag) -> list[Text]:
    lines: list[Text] = []
    head = Text("\n")
    head.append("Diagnosis  ", style="bold magenta")
    head.append(diag.summary)
    lines.append(head)
    cause = Text()
    cause.append("Cause      ", style="bold magenta")
    cause.append(diag.likely_cause)
    cause.append(f"  (confidence: {diag.confidence})", style="dim")
    lines.append(cause)
    for i, item in enumerate(diag.evidence):
        ev = Text()
        ev.append("Evidence   " if i == 0 else "           ", style="bold magenta")
        ev.append("• " + item, style="dim")
        lines.append(ev)
    for i, cmd in enumerate(diag.things_to_check):
        chk = Text()
        chk.append("Check      " if i == 0 else "           ", style="bold magenta")
        chk.append("$ " + cmd, style="green")
        lines.append(chk)
    if diag.hidden_commands:
        n = diag.hidden_commands
        lines.append(Text(f"           ({n} suggested command{'s' if n != 1 else ''} hidden: could change things)", style="dim"))
    return lines


def render_dry_run(console: Console, cfg: LLMConfig, req: Request, payload: Payload) -> None:
    """Show exactly what a real --explain call would send. Nothing is sent."""
    info = Text()
    info.append("Dry run: nothing was sent.\n", style="bold green")
    where = "on this machine" if cfg.is_local else "over the internet"
    info.append("Provider   ", style="bold")
    info.append(f"{cfg.provider} ({where})\n")
    info.append("Endpoint   ", style="bold")
    info.append(f"{req.url}\n")
    info.append("Model      ", style="bold")
    info.append(f"{cfg.model}\n")
    info.append("Redacted   ", style="bold")
    info.append(f"{payload.redaction}\n")
    info.append("Size       ", style="bold")
    info.append(f"{len(payload.user):,} characters of log data, {payload.pattern_count} patterns\n")
    if cfg.provider != "ollama" and cfg.api_key is not None:
        info.append("API key    ", style="bold")
        info.append("read from the environment, never shown or stored", style="dim")
    console.print(Panel(info, border_style="green", title="Strelizia --dry-run", title_align="left"))
    console.print(Panel(Text(payload.system), title="System message", title_align="left", border_style="dim"))
    console.print(Panel(Text(payload.user), title="User message (all the log data the model would see)", title_align="left", border_style="dim"))


def clusters_to_json(
    clusters: list[Cluster], stats: Stats, top: int, explanation: Optional[Explanation] = None
) -> dict:
    def diagnosis(rank: int):
        diag = explanation.patterns.get(rank) if explanation else None
        if not diag:
            return None
        return {
            "summary": diag.summary,
            "likely_cause": diag.likely_cause,
            "confidence": diag.confidence,
            "evidence": diag.evidence,
            "things_to_check": diag.things_to_check,
        }

    return {
        "stats": {
            "lines": stats.lines,
            "records": stats.records,
            "skipped": stats.skipped,
            "clusters": stats.clusters,
        },
        "overall_diagnosis": explanation.overall if explanation else None,
        "clusters": [
            {
                "id": c.id,
                "level": c.level,
                "count": c.count,
                "template": c.template,
                "first_seen": c.first_seen.isoformat() if c.first_seen else None,
                "last_seen": c.last_seen.isoformat() if c.last_seen else None,
                "samples": c.samples,
                "diagnosis": diagnosis(rank),
            }
            for rank, c in enumerate(clusters[:top], start=1)
        ],
    }
