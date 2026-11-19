"""The `strelizia` command line interface."""

import json
import sys
from typing import Annotated, Optional

import typer
from rich.console import Console

from . import __version__
from .demo import SCENARIOS, generate
from .explain import ExplainError, Explanation, build_payload, explain as run_explain
from .llm import PROVIDERS, LLMError, build_request, resolve_config
from .models import LEVELS
from .pipeline import analyze_lines
from .report import clusters_to_json, render_dry_run, render_report
from .sources import SourceError
from .sources.docker import inspect_container, read_container
from .sources.file import read_file
from .util import parse_duration

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
    since: Annotated[str, typer.Option("--since", "-s", help="Docker only: how far back to read (e.g. 30m, 1h, 2d).")] = "1h",
    top: Annotated[int, typer.Option("--top", "-n", min=1, help="How many patterns to show.")] = 10,
    min_level: Annotated[str, typer.Option("--min-level", "-l", help="Ignore records below this level.")] = "warn",
    samples: Annotated[int, typer.Option("--samples", min=0, max=5, help="Sample log entries to show per pattern.")] = 2,
    as_json: JsonOpt = False,
    explain: ExplainOpt = False,
    dry_run: DryRunOpt = False,
    provider: ProviderOpt = "ollama",
    model: ModelOpt = None,
    base_url: BaseUrlOpt = None,
    explain_top: ExplainTopOpt = 3,
) -> None:
    """Group a log into its distinct problems, and optionally diagnose them."""
    if bool(file) == bool(docker):
        err_console.print("[red]Provide exactly one source:[/red] a log file (or '-' for stdin), or --docker NAME.")
        raise typer.Exit(2)

    level = _normalise_level(min_level)
    facts: Optional[list[str]] = None
    try:
        if docker:
            lines = read_container(docker, since_seconds=parse_duration(since))
            source = f"container: {docker} (last {since})"
        else:
            lines = read_file(file)  # type: ignore[arg-type]
            source = "stdin" if file == "-" else str(file)
        clusters, stats = analyze_lines(lines, source=source, min_level=level, max_samples=samples)
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
def version() -> None:
    """Show the Strelizia version."""
    typer.echo(f"strelizia {__version__}")


if __name__ == "__main__":  # pragma: no cover
    app()
