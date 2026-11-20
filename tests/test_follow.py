import os
import sys
from datetime import datetime, timezone
import threading
import time

import pytest

from strelizia import follow
from strelizia.follow import Source, Watcher, follow_docker, follow_file
from strelizia.models import RawLine
from strelizia.sources.docker import ContainerInfo
from strelizia.watch import WindowResult


def wait_for(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class Runner:
    """Runs a follower in a thread and collects the lines it produces."""

    def __init__(self, target, *args, **kw):
        self.lines: list[RawLine] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=target, args=(*args, self.lines.append, self.stop), kwargs=kw, daemon=True)
        self.thread.start()

    def texts(self):
        return [r.text for r in self.lines]

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=3)
        assert not self.thread.is_alive()


# ---------------------------------------------------------------- files


def test_file_follower_only_reads_new_lines_by_default(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("old line 1\nold line 2\n")
    r = Runner(follow_file, str(log), poll=0.02)
    time.sleep(0.2)
    with log.open("a") as fh:
        fh.write("new line\n")
    assert wait_for(lambda: r.texts() == ["new line"])
    r.finish()


def test_file_follower_can_start_from_the_beginning(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("one\ntwo\n")
    r = Runner(follow_file, str(log), from_start=True, poll=0.02)
    assert wait_for(lambda: r.texts() == ["one", "two"])
    r.finish()


def test_partial_lines_are_completed_before_delivery(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    r = Runner(follow_file, str(log), poll=0.02)
    time.sleep(0.1)
    with log.open("a") as fh:
        fh.write("half a li")
        fh.flush()
        time.sleep(0.2)
        assert r.texts() == []
        fh.write("ne\n")
    assert wait_for(lambda: r.texts() == ["half a line"])
    r.finish()


def test_windows_line_endings_are_stripped(tmp_path):
    log = tmp_path / "app.log"
    log.write_bytes(b"first\r\nsecond\r\n")
    r = Runner(follow_file, str(log), from_start=True, poll=0.02)
    assert wait_for(lambda: r.texts() == ["first", "second"])
    r.finish()


def test_truncation_is_followed(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("a long first line of the old log\n" * 3)
    r = Runner(follow_file, str(log), poll=0.02)
    time.sleep(0.2)
    log.write_text("fresh\n")  # truncated and rewritten, shorter than before
    assert wait_for(lambda: "fresh" in r.texts())
    r.finish()


@pytest.mark.skipif(sys.platform == "win32", reason="an open file cannot be renamed on Windows")
def test_rotation_is_followed(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("")
    r = Runner(follow_file, str(log), poll=0.02)
    time.sleep(0.2)
    with log.open("a") as fh:
        fh.write("before rotation\n")
    assert wait_for(lambda: r.texts() == ["before rotation"])
    os.replace(log, tmp_path / "app.log.1")
    log.write_text("after rotation\n")
    assert wait_for(lambda: "after rotation" in r.texts())
    r.finish()


def test_a_file_that_appears_later_is_read_from_its_start(tmp_path):
    log = tmp_path / "later.log"
    r = Runner(follow_file, str(log), poll=0.02)
    time.sleep(0.3)
    log.write_text("hello\n")
    assert wait_for(lambda: r.texts() == ["hello"], timeout=5)
    r.finish()


# ---------------------------------------------------------------- docker


class FakeContainer:
    def __init__(self, streams):
        self.streams = list(streams)
        self.calls = []

    def logs(self, **kwargs):
        since = kwargs.get("since")
        # older versions of the docker library reject anything but a datetime or a positive int
        if since is not None and not (isinstance(since, int) and since > 0):
            raise ValueError(f"since value should be datetime or positive int, got {type(since)}")
        self.calls.append(kwargs)
        return iter(self.streams.pop(0) if self.streams else [])


def test_docker_follower_handles_split_chunks_and_reconnects_without_duplicates(monkeypatch):
    container = FakeContainer([
        [b"2026-11-18T10:00:00.000000001Z ERR", b"OR one\n2026-11-18T10:00:01.000000001Z two\n"],
        # after the reconnect Docker repeats the last line, then sends a new one
        [b"2026-11-18T10:00:01.000000001Z two\n2026-11-18T10:00:02.000000001Z three\n"],
    ])
    monkeypatch.setattr(follow, "_get_container", lambda name: container)
    r = Runner(follow_docker, "api", retry=0.01)
    assert wait_for(lambda: len(r.lines) >= 3)
    time.sleep(0.1)
    r.finish()
    assert r.texts() == ["ERROR one", "two", "three"]
    assert all(line.ts is not None for line in r.lines)
    assert container.calls[0]["follow"] is True and container.calls[0]["timestamps"] is True
    # resumes from the last line it saw (10:00:01 UTC), not from "now"
    assert container.calls[1]["since"] == int(datetime(2026, 11, 18, 10, 0, 1, tzinfo=timezone.utc).timestamp())


def test_docker_follower_reports_problems_and_keeps_retrying(monkeypatch):
    attempts = []

    def broken(name):
        attempts.append(name)
        raise follow.SourceError("No container named 'api'")

    monkeypatch.setattr(follow, "_get_container", broken)
    reports = []
    r = Runner(follow_docker, "api", retry=0.01, report=reports.append)
    assert wait_for(lambda: len(attempts) >= 3)
    r.finish()
    assert "No container" in reports[0]


# ---------------------------------------------------------------- the watcher loop


class StubEngine:
    def __init__(self):
        self.windows = []

    def process_window(self, source, records, start, end, container=None):
        self.windows.append((source, list(records), start, end, container))
        return WindowResult(source=source, records=len(records))


def scripted(*steps):
    """A reader that emits (delay, line) steps."""
    def reader(put, stop):
        for delay, line in steps:
            if stop.wait(delay):
                return
            put(RawLine(line))
        stop.wait(10)
    return reader


def test_watcher_cuts_the_stream_into_windows_and_joins_multiline_records():
    engine = StubEngine()
    steps = [
        (0.05, "2026-11-18 10:00:00 ERROR request failed"),
        (0.0, "Traceback (most recent call last):"),
        (0.0, '  File "/app/x.py", line 3, in f'),
        (0.0, "ValueError: bad"),
        (0.5, "2026-11-18 10:00:01 ERROR second window"),
    ]
    results = []
    w = Watcher(engine, [Source("api", scripted(*steps))], interval=0.4, idle_flush=0.0, on_window=results.append)
    w.run(duration=1.0)
    assert len(engine.windows) >= 2 and len(results) == len(engine.windows)
    first_records = engine.windows[0][1]
    assert len(first_records) == 1 and first_records[0].line_count == 4  # the traceback stayed together
    all_messages = [r.message for _, recs, *_ in engine.windows for r in recs]
    assert any("second window" in m for m in all_messages)
    for _, _, start, end, _ in engine.windows[:-1]:
        assert abs((end - start) - 0.4) < 0.15


def test_watcher_handles_several_sources_independently():
    engine = StubEngine()
    sources = [
        Source("a", scripted((0.05, "ERROR in a"))),
        Source("b", scripted((0.05, "ERROR in b"), (0.0, "ERROR in b again"))),
    ]
    Watcher(engine, sources, interval=0.3, idle_flush=0.0).run(duration=0.5)
    by_source = {}
    for name, recs, *_ in engine.windows:
        by_source.setdefault(name, []).extend(r.message for r in recs)
    assert by_source["a"] == ["ERROR in a"]
    assert by_source["b"] == ["ERROR in b", "ERROR in b again"]


def test_a_crashed_reader_is_reported_not_silent():
    def crashing(put, stop):
        raise RuntimeError("boom")

    errors = []
    w = Watcher(StubEngine(), [Source("api", crashing)], interval=0.2, on_error=errors.append)
    w.run(duration=0.4)
    assert wait_for(lambda: errors, timeout=2)
    assert "reader stopped" in errors[0] and "boom" in errors[0]


def test_docker_sources_pass_container_info_to_the_engine(monkeypatch):
    info = ContainerInfo("api", "img", "running", 0, False, 0, "2026-11-18T10:00:00Z", "")
    monkeypatch.setattr(follow, "inspect_container", lambda name: info)
    engine = StubEngine()
    Watcher(engine, [Source("api", scripted(), container="api")], interval=0.2).run(duration=0.3)
    assert engine.windows and engine.windows[0][4] is info


def test_docker_inspect_failures_do_not_stop_the_watch(monkeypatch):
    def broken(name):
        raise RuntimeError("daemon hiccup")

    monkeypatch.setattr(follow, "inspect_container", broken)
    engine = StubEngine()
    Watcher(engine, [Source("api", scripted(), container="api")], interval=0.2).run(duration=0.3)
    assert engine.windows and engine.windows[0][4] is None


def test_stop_event_ends_the_loop_promptly():
    engine = StubEngine()
    w = Watcher(engine, [Source("api", scripted())], interval=30)
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    time.sleep(0.3)
    w.stop.set()
    t.join(timeout=3)
    assert not t.is_alive()
