import json

import pytest
from typer.testing import CliRunner

from strelizia.cli import app
from strelizia.demo import SCENARIOS, generate
from strelizia.pipeline import analyze_lines
from strelizia.sources.docker import split_docker_prefix
from strelizia.util import parse_duration

runner = CliRunner()
ENV = {"COLUMNS": "200"}


def test_db_outage_demo_finds_the_root_cause():
    clusters, stats = analyze_lines(generate("db-outage"))
    assert stats.lines > stats.records  # tracebacks were assembled
    assert "ECONNREFUSED" in clusters[0].template
    assert clusters[0].count > 500
    assert len(clusters) <= 6


def test_nginx_demo_separates_502_from_404():
    clusters, _ = analyze_lines(generate("nginx-502"))
    by_level = {c.level: c.template for c in clusters if "returned HTTP" in c.template}
    assert by_level["ERROR"].endswith("returned HTTP 502")
    assert by_level["WARN"].endswith("returned HTTP 404")
    assert any("connect() failed" in c.template and "upstream=" in c.template for c in clusters)
    assert not any("Mozilla" in c.template or "<TS>" in c.template for c in clusters)  # access-log noise is gone


def test_bad_deploy_demo_separates_exceptions():
    clusters, _ = analyze_lines(generate("bad-deploy"))
    templates = " ".join(c.template for c in clusters)
    assert "NullPointerException" in templates
    assert "IllegalStateException" in templates
    assert len(clusters) <= 5


def test_min_level_filters_records():
    clusters, stats = analyze_lines(generate("db-outage"), min_level="ERROR")
    assert all(c.level in ("ERROR", "FATAL") for c in clusters)
    assert stats.skipped > 0


def test_demo_is_deterministic_for_a_seed():
    assert [c.template for c in analyze_lines(generate("db-outage", seed=3))[0]] == [
        c.template for c in analyze_lines(generate("db-outage", seed=3))[0]
    ]


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_cli_demo_renders(scenario):
    result = runner.invoke(app, ["demo", "--scenario", scenario], env=ENV)
    assert result.exit_code == 0, result.output
    assert "Strelizia" in result.output
    assert "patterns" in result.output


def test_cli_demo_json_is_valid():
    result = runner.invoke(app, ["demo", "--json", "--top", "2"])
    data = json.loads(result.output)
    assert len(data["clusters"]) == 2
    assert data["stats"]["lines"] > 0


def test_cli_analyze_file_and_stdin(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("\n".join(generate("db-outage")))
    r1 = runner.invoke(app, ["analyze", str(log)], env=ENV)
    assert r1.exit_code == 0 and "ECONNREFUSED" in r1.output
    r2 = runner.invoke(app, ["analyze", "-"], input=log.read_text(), env=ENV)
    assert r2.exit_code == 0 and "ECONNREFUSED" in r2.output


def test_cli_errors_are_friendly():
    assert runner.invoke(app, ["analyze"]).exit_code == 2
    assert runner.invoke(app, ["analyze", "x.log", "--docker", "c"]).exit_code == 2
    missing = runner.invoke(app, ["analyze", "/no/such/file.log"])
    assert missing.exit_code == 1 and "not found" in missing.output.lower()
    assert runner.invoke(app, ["analyze", "x.log", "--min-level", "loud"]).exit_code == 2
    assert runner.invoke(app, ["demo", "--scenario", "nope"]).exit_code == 2


def test_clean_log_reports_nothing_to_worry_about(tmp_path):
    log = tmp_path / "ok.log"
    log.write_text("2026-11-18 14:00:00 INFO all good\n" * 50)
    result = runner.invoke(app, ["analyze", str(log)], env=ENV)
    assert result.exit_code == 0
    assert "Nothing to worry about" in result.output


def test_rich_markup_in_logs_is_not_interpreted(tmp_path):
    log = tmp_path / "markup.log"
    log.write_text("2026/11/18 14:00:00 [error] 1#1: [bold]oops[/bold] failed\n")
    result = runner.invoke(app, ["analyze", str(log)], env=ENV)
    assert result.exit_code == 0
    assert "[bold]oops[/bold]" in result.output


def test_split_docker_prefix():
    raw = split_docker_prefix("2026-11-18T14:02:11.123456789Z ERROR boom")
    assert raw.text == "ERROR boom"
    assert raw.ts is not None and raw.ts.year == 2026
    plain = split_docker_prefix("no prefix here")
    assert plain.text == "no prefix here" and plain.ts is None


def test_parse_duration():
    assert parse_duration("90s") == 90
    assert parse_duration("15m") == 900
    assert parse_duration("1h30m") == 5400
    assert parse_duration("2d") == 172800
    for bad in ("", "abc", "1x", "h"):
        with pytest.raises(ValueError):
            parse_duration(bad)
