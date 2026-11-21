import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "benchmarks" / "bench.py"


def load():
    spec = importlib.util.spec_from_file_location("bench", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_benchmark_script_still_runs(capsys):
    """A tiny run, so the benchmark cannot silently rot."""
    bench = load()
    assert bench.main(["--lines", "2000", "--repeat", "1", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["results"]) == len(bench.CASES)
    for row in data["results"]:
        assert row["lines"] >= 2000 and row["lines_per_sec"] > 0 and row["patterns"] >= 1


def test_adapters_off_rows_exist_for_an_honest_comparison():
    names = [c["name"] for c in load().CASES]
    assert "nginx, adapters off" in names and "JSON lines, adapters off" in names
