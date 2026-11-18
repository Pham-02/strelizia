"""The `strelizia` command line interface."""

import json
import sys
from typing import Optional

import typer
from rich.console import Console

from . import __version__
from .demo import SCENARIOS, generate
from .models import LEVELS
from .pipeline import analyze_lines
from .report import clusters_to_json, render_report
from .sources import SourceError
from .sources.docker import read_container
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


def _normalise_level(value: str) -> str:
    key = value.strip().lower()
    level = _LEVEL_ALIASES.get(key, key.upper())
    if level not in LEVELS:
        raise typer.BadParameter(f"must be one of: {', '.join(l.lower() for l in LEVELS)}")
    return level


def _emit(clusters, stats, *, top: int, source: str, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(clusters_to_json(clusters, stats, top), indent=2))
    else:
        render_report(console, clusters, stats, top, source)


@app.command()
def analyze(
    file: Optional[str] = typer.Argument(None, help="Log file to analyze, or '-' to read from stdin."),
    docker: Optional[str] = typer.Option(None, "--docker", "-d", help="Docker container name or id."),
    since: str = typer.Option("1h", "--since", "-s", help="Docker only: how far back to read (e.g. 30m, 1h, 2d)."),
    top: int = typer.Option(10, "--top", "-n", min=1, help="How many patterns to show."),
    min_level: str = typer.Option("warn", "--min-level", "-l", help="Ignore records below this level."),
    samples: int = typer.Option(2, "--samples", min=0, max=5, help="Sample log entries to show per pattern."),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON instead of a report."),
) -> None:
    """Group a log into its distinct problems."""
    if bool(file) == bool(docker):
        err_console.print("[red]Provide exactly one source:[/red] a log file (or '-' for stdin), or --docker NAME.")
        raise typer.Exit(2)

    level = _normalise_level(min_level)
    try:
        if docker:
            lines = read_container(docker, since_seconds=parse_duration(since))
            source = f"container: {docker} (last {since})"
        else:
            lines = read_file(file)  # type: ignore[arg-type]
            source = "stdin" if file == "-" else str(file)
        clusters, stats = analyze_lines(lines, source=source, min_level=level, max_samples=samples)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    except SourceError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    _emit(clusters, stats, top=top, source=source, as_json=as_json)


@app.command()
def demo(
    scenario: str = typer.Option("db-outage", "--scenario", help=f"One of: {', '.join(SCENARIOS)}."),
    raw: bool = typer.Option(False, "--raw", help="Print the generated raw logs instead of analyzing them."),
    seed: int = typer.Option(7, "--seed", help="Random seed (same seed, same logs)."),
    top: int = typer.Option(10, "--top", "-n", min=1),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON instead of a report."),
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

    clusters, stats = analyze_lines(lines, source=f"demo: {scenario}")
    _emit(clusters, stats, top=top, source=f"demo: {scenario}", as_json=as_json)


@app.command()
def version() -> None:
    """Show the Strelizia version."""
    typer.echo(f"strelizia {__version__}")


if __name__ == "__main__":  # pragma: no cover
    app()
