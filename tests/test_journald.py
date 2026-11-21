import json
import threading
from datetime import datetime

import pytest
from typer.testing import CliRunner

from strelizia.cli import app
from strelizia.pipeline import analyze_lines
from strelizia.sources import SourceError, journald
from strelizia.sources.journald import build_command, follow_journal, read_journal

runner = CliRunner()


def entry(priority, message, ident="app", ts="1731938531123456"):
    return json.dumps({"PRIORITY": str(priority), "MESSAGE": message, "SYSLOG_IDENTIFIER": ident,
                       "__REALTIME_TIMESTAMP": ts, "_PID": "1"})


class FakePopen:
    """Stands in for subprocess.Popen so no real journalctl is needed."""

    instances: list = []
    lines: list[str] = []
    returncode_to_use = 0
    stderr_text = ""

    def __init__(self, cmd, stdout=None, stderr=None, **kwargs):
        self.cmd = cmd
        self.returncode = None
        self.terminated = False
        if hasattr(stderr, "write") and FakePopen.stderr_text:
            stderr.write(FakePopen.stderr_text)
        self.stdout = iter(FakePopen.lines)
        FakePopen.instances.append(self)

    def wait(self):
        self.returncode = FakePopen.returncode_to_use
        return self.returncode

    def terminate(self):
        self.terminated = True


@pytest.fixture(autouse=True)
def fake_popen(monkeypatch):
    FakePopen.instances, FakePopen.lines, FakePopen.returncode_to_use, FakePopen.stderr_text = [], [], 0, ""
    monkeypatch.setattr(journald.subprocess, "Popen", FakePopen)
    return FakePopen


def test_command_construction():
    assert build_command() == ["journalctl", "--no-pager", "-o", "json"]
    assert build_command("nginx", follow=True) == ["journalctl", "--no-pager", "-o", "json", "-f", "-n", "0", "-u", "nginx"]
    cmd = build_command(None, datetime(2026, 11, 18, 14, 2, 11))
    assert cmd[-2:] == ["--since", "2026-11-18 14:02:11"]


def test_read_journal_yields_lines_and_asks_for_the_right_window(fake_popen):
    fake_popen.lines = [entry(3, "disk failing") + "\n", "\n", entry(6, "fine") + "\n"]
    lines = list(read_journal("nginx", 3600))
    assert len(lines) == 2 and "disk failing" in lines[0].text
    cmd = fake_popen.instances[0].cmd
    assert "-u" in cmd and "nginx" in cmd and "--since" in cmd and "-f" not in cmd


def test_journal_entries_flow_through_the_json_adapter():
    FakePopen.lines = [entry(3, "Out of memory: Killed process 4242 (java)", "kernel") + "\n" for _ in range(30)] + [entry(6, "started") + "\n"]
    clusters, stats = analyze_lines(read_journal(None, 600))
    assert stats.records == 31 and len(clusters) == 1
    assert clusters[0].level == "ERROR" and clusters[0].count == 30
    assert "Killed process" in clusters[0].template


def test_missing_journalctl_is_a_clear_error(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(journald.subprocess, "Popen", boom)
    with pytest.raises(SourceError, match="needs Linux with systemd"):
        list(read_journal(None, 60))


def test_journalctl_failure_reports_its_message(fake_popen):
    fake_popen.returncode_to_use = 1
    fake_popen.stderr_text = "Failed to determine unit: Unit nope.service not found\nmore\n"
    with pytest.raises(SourceError, match="Unit nope.service not found"):
        list(read_journal("nope", 60))


def test_follow_streams_new_entries_and_stops_cleanly(fake_popen):
    fake_popen.lines = [entry(3, "one") + "\n", entry(4, "two") + "\n"]
    got, stop = [], threading.Event()

    def put(raw):
        got.append(raw)
        if len(got) == 2:
            stop.set()

    follow_journal("nginx", put, stop, retry=0.01)
    assert len(got) == 2
    assert "-f" in fake_popen.instances[0].cmd and "-u" in fake_popen.instances[0].cmd


def test_follow_restarts_journalctl_if_it_exits(fake_popen):
    fake_popen.lines = [entry(3, "x") + "\n"]
    stop, reports, count = threading.Event(), [], []

    def put(raw):
        count.append(1)
        if len(count) == 2:  # the second process delivered a line: it was restarted
            stop.set()

    follow_journal(None, put, stop, retry=0.01, report=reports.append)
    assert len(fake_popen.instances) >= 2 and any("retrying" in r for r in reports)


def test_follow_gives_up_when_journalctl_does_not_exist(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(journald.subprocess, "Popen", boom)
    reports = []
    follow_journal(None, lambda r: None, threading.Event(), report=reports.append)
    assert "needs Linux" in reports[0]


def test_cli_analyze_journald(fake_popen):
    fake_popen.lines = [entry(3, "Out of memory: Killed process 4242 (java)", "kernel") + "\n" for _ in range(8)]
    r = runner.invoke(app, ["analyze", "--journald", "--unit", "nginx"], env={"COLUMNS": "200"})
    assert r.exit_code == 0, r.output
    assert "journald: nginx" in " ".join(r.output.split()) and "Killed process" in r.output


def test_cli_validation():
    assert runner.invoke(app, ["analyze", "--unit", "nginx", "x.log"]).exit_code == 2
    assert runner.invoke(app, ["analyze", "--journald", "x.log"]).exit_code == 2
    assert runner.invoke(app, ["analyze"]).exit_code == 2
    assert runner.invoke(app, ["watch", "--unit", "nginx", "--demo", "spike"]).exit_code == 2


def test_cli_without_journalctl_fails_politely(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(journald.subprocess, "Popen", boom)
    r = runner.invoke(app, ["analyze", "--journald"], env={"COLUMNS": "200"})
    assert r.exit_code == 1 and "needs Linux with systemd" in " ".join(r.output.split())
