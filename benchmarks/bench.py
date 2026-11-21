"""Reproducible benchmarks for Strelizia's analysis pipeline.

    python benchmarks/bench.py                      # 300,000 lines per case, best of 3
    python benchmarks/bench.py --lines 1000000 --repeat 1
    python benchmarks/bench.py --json               # machine-readable

Each case runs in its own process, reading a generated log file from disk (so memory is the
analyzer's, not a Python list of lines). Numbers depend entirely on your hardware, and the
logs are synthetic, so treat them as a guide and compare runs on the same machine.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CASES = [
    {"name": "Plain text, an error flood", "scenario": "db-outage", "adapters": True},
    {"name": "Java stack traces", "scenario": "bad-deploy", "adapters": True},
    {"name": "nginx access + error logs", "scenario": "nginx-502", "adapters": True},
    {"name": "nginx, adapters off", "scenario": "nginx-502", "adapters": False},
    {"name": "JSON lines", "scenario": "json-api", "adapters": True},
    {"name": "JSON lines, adapters off", "scenario": "json-api", "adapters": False},
]


def _rss_mb():
    """Current resident memory in MB (Linux only), or None."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def _peak_rss_mb():
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / (1024 * 1024 if sys.platform == "darwin" else 1024)
    except ImportError:  # Windows
        return None


def _make_log(path: Path, scenario: str, target: int) -> int:
    from strelizia.demo import generate

    written, seed = 0, 1
    with path.open("w", encoding="utf-8") as fh:
        while written < target:
            lines = generate(scenario, seed=seed)
            fh.write("\n".join(lines) + "\n")
            written += len(lines)
            seed += 1
    return written


def _worker(index: int, target: int, repeat: int) -> dict:
    """Run one case. Called in a fresh process so memory numbers are clean."""
    from strelizia.formats import ALL_FORMATS
    from strelizia.pipeline import analyze_lines
    from strelizia.sources.file import read_file

    case = CASES[index]
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "bench.log"
        lines = _make_log(log, case["scenario"], target)
        formats = ALL_FORMATS if case["adapters"] else frozenset()
        before, best, patterns = _rss_mb(), float("inf"), 0
        for _ in range(repeat):
            start = time.perf_counter()
            clusters, _stats = analyze_lines(read_file(str(log)), formats=formats)
            best = min(best, time.perf_counter() - start)
            patterns = len(clusters)
        peak = _peak_rss_mb()
    extra = round(peak - before, 1) if peak is not None and before is not None else None
    return {"name": case["name"], "lines": lines, "seconds": round(best, 2),
            "lines_per_sec": round(lines / best), "extra_mb": extra, "patterns": patterns}


def _hardware() -> str:
    cpu = platform.processor() or platform.machine()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return f"{cpu}, {os.cpu_count()} cores, Python {platform.python_version()}, {platform.system()}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lines", type=int, default=300_000, help="log lines per case")
    parser.add_argument("--repeat", type=int, default=3, help="runs per case; the best is reported")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker is not None:
        print(json.dumps(_worker(args.worker, args.lines, args.repeat)))
        return 0

    results = []
    for index in range(len(CASES)):
        proc = subprocess.run(
            [sys.executable, __file__, "--worker", str(index), "--lines", str(args.lines), "--repeat", str(args.repeat)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(proc.stderr, file=sys.stderr)
            return 1
        results.append(json.loads(proc.stdout.strip().splitlines()[-1]))

    if args.json:
        print(json.dumps({"hardware": _hardware(), "results": results}, indent=2))
        return 0
    print(f"Hardware: {_hardware()}\n")
    print("| Case | Lines | Time | Lines per second | Extra memory | Patterns found |")
    print("|---|---:|---:|---:|---:|---:|")
    for r in results:
        mem = f"{r['extra_mb']:.0f} MB" if r["extra_mb"] is not None else "n/a"
        print(f"| {r['name']} | {r['lines']:,} | {r['seconds']:.1f}s | {r['lines_per_sec']:,} | {mem} | {r['patterns']} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
