"""The `strelizia` command line interface."""

import json
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from . import __version__
from .demo import SCENARIOS, generate
from .explain import ExplainError, Explanation, build_payload, explain as run_explain
from .llm import PROVIDERS, LLMError, build_request, resolve_config
from .formats import FORMAT_CHOICES, formats_for
from .follow import Source, Watcher, follow_demo, follow_docker, follow_file
from .models import LEVELS
from .notify import NotifyError, parse_targets
from .pipeline import analyze_lines
from .report import clusters_to_json, render_dry_run, render_history, render_report, render_watch_header, render_window
from .sources import SourceError
from .sources.docker import inspect_container, read_container
from .sources.file import read_file
from .sources.journald import follow_journal, read_journal
from .store import Store, default_state_path
from .digest import build_digest
from .util import parse_clock, parse_duration, parse_quiet_hours
from .watch import Engine, WatchConfig

app = typer.Typer(
    help="Strelizia: turn thousands of noisy log lines into the few problems that matter.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)

_LEVEL_ALIASES = {"warning": "WARN", "err": "ERROR", "critical": "FATAL"}

# Options shared by `analyze` and `demo`
ExplainOpt = Annotated[bool, typer.Option("--explain", help="Ask a language model to diagnose the top patterns.")]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Show exactly what --explain would send, without sending anything.")]
ProviderOpt = Annotated[str, typer.Option("--provider", help=f"Model provider: {', '.join(PROVIDERS)}. 'openai' works with any OpenAI-compatible server.")]
ModelOpt = Annotated[Optional[str], typer.Option("--model", help="Model name, e.g. llama3.1. Or set STRELIZIA_MODEL.")]
BaseUrlOpt = Annotated[Optional[str], typer.Option("--base-url", help="Override the provider's URL. Or set STRELIZIA_BASE_URL.")]
ExplainTopOpt = Annotated[int, typer.Option("--explain-top", min=1, max=8, help="How many top patterns to send for diagnosis.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print machine-readable JSON instead of a report.")]
FormatOpt = Annotated[str, typer.Option("--format", help=f"Log format: {', '.join(FORMAT_CHOICES)}. 'auto' detects JSON, nginx and syslog per line.")]


def _formats(choice: str) -> frozenset:
    try:
        return formats_for(choice)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)


def _normalise_level(value: str) -> str:
    key = value.strip().lower()
    level = _LEVEL_ALIASES.get(key, key.upper())
    if level not in LEVELS:
        raise typer.BadParameter(f"must be one of: {', '.join(l.lower() for l in LEVELS)}")
    return level


def _finish(
    clusters,
    stats,
    *,
    top: int,
    source: str,
    as_json: bool,
    explain: bool,
    dry_run: bool,
    provider: str,
    model: Optional[str],
    base_url: Optional[str],
    explain_top: int,
    facts: Optional[list[str]] = None,
) -> None:
    """Optionally diagnose with a model, then print the report."""
    explanation: Optional[Explanation] = None
    error: Optional[str] = None
    dry = None

    if (explain or dry_run) and clusters:
        try:
            cfg = resolve_config(provider, model, base_url, require_model=not dry_run)
            payload = build_payload(clusters, stats, source=source, facts=facts, top=explain_top)
            if dry_run:
                dry = (cfg, build_request(cfg, payload.system, payload.user), payload)
            else:
                if not cfg.is_local:
                    err_console.print(
                        f"[dim]Sending {payload.pattern_count} redacted patterns ({len(payload.user):,} "
                        f"characters) to {cfg.host}. Preview exactly what with --dry-run.[/dim]"
                    )
                explanation = run_explain(payload, cfg)
        except (LLMError, ExplainError) as exc:
            error = str(exc)

    if as_json:
        if dry:
            cfg, req, payload = dry
            typer.echo(json.dumps({"dry_run": True, "url": req.url, "redacted": payload.redaction, "body": req.body}, indent=2))
        else:
            typer.echo(json.dumps(clusters_to_json(clusters, stats, top, explanation), indent=2))
    else:
        render_report(console, clusters, stats, top, source, explanation)
        if dry:
            render_dry_run(console, *dry)

    if error:
        err_console.print(f"[red]Could not get a diagnosis:[/red] {error}")
        raise typer.Exit(1)


@app.command()
def analyze(
    file: Annotated[Optional[str], typer.Argument(help="Log file to analyze, or '-' to read from stdin.")] = None,
    docker: Annotated[Optional[str], typer.Option("--docker", "-d", help="Docker container name or id.")] = None,
    journald: Annotated[bool, typer.Option("--journald", help="Read the systemd journal (Linux).")] = False,
    unit: Annotated[Optional[str], typer.Option("--unit", "-u", help="With --journald: only this systemd unit, e.g. nginx.")] = None,
    since: Annotated[str, typer.Option("--since", "-s", help="Docker and journald: how far back to read (e.g. 30m, 1h, 2d).")] = "1h",
    top: Annotated[int, typer.Option("--top", "-n", min=1, help="How many patterns to show.")] = 10,
    min_level: Annotated[str, typer.Option("--min-level", "-l", help="Ignore records below this level.")] = "warn",
    samples: Annotated[int, typer.Option("--samples", min=0, max=5, help="Sample log entries to show per pattern.")] = 2,
    as_json: JsonOpt = False,
    log_format: FormatOpt = "auto",
    explain: ExplainOpt = False,
    dry_run: DryRunOpt = False,
    provider: ProviderOpt = "ollama",
    model: ModelOpt = None,
    base_url: BaseUrlOpt = None,
    explain_top: ExplainTopOpt = 3,
) -> None:
    """Group a log into its distinct problems, and optionally diagnose them."""
    if bool(file) + bool(docker) + journald != 1:
        err_console.print("[red]Provide exactly one source:[/red] a log file (or '-' for stdin), --docker NAME, or --journald.")
        raise typer.Exit(2)
    if unit and not journald:
        err_console.print("[red]--unit only works together with --journald.[/red]")
        raise typer.Exit(2)

    level = _normalise_level(min_level)
    formats = _formats(log_format)
    facts: Optional[list[str]] = None
    try:
        if docker:
            lines = read_container(docker, since_seconds=parse_duration(since))
            source = f"container: {docker} (last {since})"
        elif journald:
            lines = read_journal(unit, parse_duration(since))
            source = f"journald{': ' + unit if unit else ''} (last {since})"
        else:
            lines = read_file(file)  # type: ignore[arg-type]
            source = "stdin" if file == "-" else str(file)
        clusters, stats = analyze_lines(lines, source=source, min_level=level, max_samples=samples, formats=formats)
        if docker and (explain or dry_run):
            try:
                facts = inspect_container(docker).facts()
            except SourceError:
                facts = None  # diagnosis still works without these extras
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    except SourceError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    _finish(
        clusters, stats, top=top, source=source, as_json=as_json, explain=explain, dry_run=dry_run,
        provider=provider, model=model, base_url=base_url, explain_top=explain_top, facts=facts,
    )


@app.command()
def demo(
    scenario: Annotated[str, typer.Option("--scenario", help=f"One of: {', '.join(SCENARIOS)}.")] = "db-outage",
    raw: Annotated[bool, typer.Option("--raw", help="Print the generated raw logs instead of analyzing them.")] = False,
    seed: Annotated[int, typer.Option("--seed", help="Random seed (same seed, same logs).")] = 7,
    top: Annotated[int, typer.Option("--top", "-n", min=1)] = 10,
    as_json: JsonOpt = False,
    explain: ExplainOpt = False,
    dry_run: DryRunOpt = False,
    provider: ProviderOpt = "ollama",
    model: ModelOpt = None,
    base_url: BaseUrlOpt = None,
    explain_top: ExplainTopOpt = 3,
) -> None:
    """Analyze generated broken logs, no server needed."""
    try:
        lines = generate(scenario, seed=seed)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)

    if raw:
        sys.stdout.write("\n".join(lines) + "\n")
        return

    source = f"demo: {scenario}"
    clusters, stats = analyze_lines(lines, source=source)
    _finish(
        clusters, stats, top=top, source=source, as_json=as_json, explain=explain, dry_run=dry_run,
        provider=provider, model=model, base_url=base_url, explain_top=explain_top,
    )


@app.command()
def watch(
    files: Annotated[Optional[list[str]], typer.Argument(help="Log file(s) to follow.")] = None,
    docker: Annotated[Optional[list[str]], typer.Option("--docker", "-d", help="Docker container to follow (repeat for several).")] = None,
    journald: Annotated[bool, typer.Option("--journald", help="Follow the systemd journal (Linux).")] = False,
    unit: Annotated[Optional[str], typer.Option("--unit", "-u", help="With --journald: only this systemd unit.")] = None,
    demo_stream: Annotated[Optional[str], typer.Option("--demo", help="Watch a simulated live log, no server needed. Try: spike")] = None,
    interval: Annotated[str, typer.Option("--interval", help="Length of each window (e.g. 30s, 1m).")] = "60s",
    notify: Annotated[Optional[list[str]], typer.Option("--notify", help="Where to send alerts (repeatable). Or set STRELIZIA_NOTIFY.")] = None,
    alert_level: Annotated[str, typer.Option("--alert-level", help="Lowest level that can raise an alert.")] = "error",
    cooldown: Annotated[str, typer.Option("--cooldown", help="Minimum time between repeat alerts for one pattern.")] = "2h",
    spike_factor: Annotated[float, typer.Option("--spike-factor", min=1.5, help="A spike is this many times the usual rate.")] = 5.0,
    min_count: Annotated[int, typer.Option("--min-count", min=1, help="Occurrences needed in a window to count as a spike.")] = 5,
    min_history: Annotated[Optional[str], typer.Option("--min-history", help="How long to watch before judging spikes (default 10m).")] = None,
    quiet_hours: Annotated[Optional[str], typer.Option("--quiet-hours", help="Hold alerts during these hours, e.g. 23:00-07:00.")] = None,
    resolve_after: Annotated[Optional[str], typer.Option("--resolve-after", help="Announce 'resolved' after a problem has been gone this long (default 15m, or 'off').")] = None,
    digest_at: Annotated[Optional[str], typer.Option("--digest-at", help="Send a daily digest at this local time, e.g. 09:00.")] = None,
    state: Annotated[Optional[str], typer.Option("--state", help="Where Strelizia keeps its memory (default ~/.strelizia/state.db).")] = None,
    from_start: Annotated[bool, typer.Option("--from-start", help="Files: read existing content too, not just new lines.")] = False,
    alert_samples: Annotated[bool, typer.Option("--alert-samples", help="Include one redacted sample line in alerts.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show alerts here but send them nowhere.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", help="Only print when something happens.")] = False,
    log_format: FormatOpt = "auto",
    duration: Annotated[Optional[str], typer.Option("--duration", help="Stop after this long, e.g. 5m.")] = None,
    explain: Annotated[bool, typer.Option("--explain", help="Add a model's diagnosis to alerts.")] = False,
    provider: ProviderOpt = "ollama",
    model: ModelOpt = None,
    base_url: BaseUrlOpt = None,
    explain_top: ExplainTopOpt = 3,
) -> None:
    """Follow live logs and alert on what is new or unusual."""
    if not (files or docker or journald or demo_stream):
        err_console.print("[red]Nothing to watch.[/red] Give a log file, --docker NAME, --journald, or try --demo spike.")
        raise typer.Exit(2)
    if unit and not journald:
        err_console.print("[red]--unit only works together with --journald.[/red]")
        raise typer.Exit(2)
    formats = _formats(log_format)

    try:
        window = parse_duration(interval)
        cooldown_s = parse_duration(cooldown)
        history_s = parse_duration(min_history) if min_history else (window * 5 if demo_stream else 600)
        stop_after = parse_duration(duration) if duration else None
        quiet_window = parse_quiet_hours(quiet_hours) if quiet_hours else None
        digest_time = parse_clock(digest_at) if digest_at else None
        if resolve_after is None:
            resolve_s = window * 2 if demo_stream else 900
        else:
            resolve_s = 0 if resolve_after.strip().lower() in ("off", "0", "never", "none") else parse_duration(resolve_after)
        notifiers = parse_targets(notify or [])
        if window <= 0:
            raise ValueError("--interval must be greater than zero.")
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)

    cfg = WatchConfig(
        interval=window, alert_level=_normalise_level(alert_level), cooldown=cooldown_s,
        spike_factor=spike_factor, min_count=min_count, min_history=history_s,
        quiet_hours=quiet_window, include_samples=alert_samples, resolve_after=resolve_s, digest_at=digest_time,
    )

    errors_seen: list[str] = []

    def on_error(message: str) -> None:
        errors_seen.append(message)
        err_console.print(f"[yellow]! {message}[/yellow]")

    sources: list[Source] = []
    try:
        for name in docker or []:
            inspect_container(name)  # fail fast on a typo instead of waiting silently
            sources.append(Source(name, lambda put, stop, n=name: follow_docker(n, put, stop, report=on_error), container=name))
    except SourceError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    for path in files or []:
        if not Path(path).exists():
            err_console.print(f"[yellow]{path} does not exist yet. Waiting for it to appear.[/yellow]")
        sources.append(Source(path, lambda put, stop, p=path: follow_file(p, put, stop, from_start=from_start)))
    if journald:
        name = f"journald: {unit}" if unit else "journald"
        sources.append(Source(name, lambda put, stop: follow_journal(unit, put, stop, report=on_error)))
    if demo_stream:
        try:
            from .demo import STREAMS
            if demo_stream not in STREAMS:
                raise ValueError(f"Unknown demo {demo_stream!r}. Choose from: {', '.join(STREAMS)}")
        except ValueError as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)
        sources.append(Source(f"demo: {demo_stream}", lambda put, stop: follow_demo(demo_stream, window, put, stop)))

    explainer = None
    if explain:
        try:
            llm_cfg = resolve_config(provider, model, base_url)
        except LLMError as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)

        def explainer(clusters, stats, source):  # noqa: F811
            payload = build_payload(clusters, stats, source=source, top=explain_top)
            return run_explain(payload, llm_cfg).overall

    store = Store(state or (":memory:" if demo_stream and not state else default_state_path()))
    engine = Engine(store, notifiers, cfg, explain=explainer, send=not dry_run)

    if dry_run:
        where = "nowhere (dry run: alerts are only shown here)"
    elif notifiers:
        where = ", ".join(n.label for n in notifiers)
    else:
        where = "this screen only (add --notify to send them somewhere)"
    extras = []
    if demo_stream and not state:
        extras.append("Demo mode: memory is not saved between runs.")
    if explain:
        extras.append(f"Diagnosis: {provider} / {model or '(no model set)'}")
    if quiet_window:
        extras.append(f"Quiet hours: {quiet_hours}")
    if digest_time:
        extras.append(f"Daily digest at {digest_at}")
    render_watch_header(console, [s.name for s in sources], interval, where, extras)

    watcher = Watcher(engine, sources, interval=window, on_window=lambda r: render_window(console, r, quiet=quiet), on_error=on_error, formats=formats)
    try:
        watcher.run(duration=stop_after)
    except KeyboardInterrupt:
        watcher.stop.set()
        console.print("\n[dim]Stopped.[/dim]")
    finally:
        store.close()


@app.command()
def digest(
    hours: Annotated[float, typer.Option("--hours", min=0.1, help="How far back to summarise.")] = 24.0,
    state: Annotated[Optional[str], typer.Option("--state", help="Memory file to read (default ~/.strelizia/state.db).")] = None,
    notify: Annotated[Optional[list[str]], typer.Option("--notify", help="Also send it here (repeatable). Or set STRELIZIA_NOTIFY.")] = None,
) -> None:
    """Print a summary of the last day (or --hours), and optionally send it."""
    import time as _time

    path = Path(state) if state else default_state_path()
    if not path.exists():
        console.print("[dim]Nothing to summarise yet. Start with `strelizia watch`.[/dim]")
        return
    try:
        notifiers = parse_targets(notify or [])
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    store = Store(path)
    try:
        message = build_digest(store, _time.time(), hours)
    finally:
        store.close()
    console.print(Panel(Text(message.text), title=Text(message.title, style="bold"), title_align="left", border_style="blue"))
    failed = False
    for notifier in notifiers:
        try:
            notifier.send(message)
            console.print(Text(f"  → sent to {notifier.label}", style="green"))
        except NotifyError as exc:
            err_console.print(f"[red]{exc}[/red]")
            failed = True
    if failed:
        raise typer.Exit(1)


@app.command()
def history(
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="How many recent alerts to show.")] = 20,
    state: Annotated[Optional[str], typer.Option("--state", help="Memory file to read (default ~/.strelizia/state.db).")] = None,
) -> None:
    """Show recent alerts Strelizia has raised."""
    path = Path(state) if state else default_state_path()
    if not path.exists():
        console.print("[dim]No alert history yet. Start one with `strelizia watch`.[/dim]")
        return
    store = Store(path)
    try:
        render_history(console, store.recent_alerts(limit))
    finally:
        store.close()


@app.command()
def version() -> None:
    """Show the Strelizia version."""
    typer.echo(f"strelizia {__version__}")


if __name__ == "__main__":  # pragma: no cover
    app()
