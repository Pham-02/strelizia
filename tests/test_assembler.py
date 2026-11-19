from datetime import datetime, timezone

from strelizia.assembler import assemble, detect_level, parse_timestamp
from strelizia.models import RawLine


def records(lines, **kw):
    return list(assemble(lines, **kw))


def test_python_traceback_is_one_record():
    recs = records([
        "2026-11-18 14:02:11 ERROR request failed",
        "Traceback (most recent call last):",
        '  File "/app/x.py", line 3, in handler',
        "    boom()",
        "ValueError: bad input",
        "2026-11-18 14:02:12 INFO ok",
    ])
    assert len(recs) == 2
    first = recs[0]
    assert first.line_count == 5
    assert first.level == "ERROR"
    assert first.resolve_timestamp() == datetime(2026, 11, 18, 14, 2, 11)
    assert not first.message.startswith("2026")  # timestamp stripped
    assert "ValueError: bad input" in first.signature
    assert "handler" in first.signature
    assert recs[1].level == "INFO"


def test_java_trace_is_one_record():
    recs = records([
        "2026-11-18 14:02:11.123 ERROR [exec-1] c.a.Ctl - Unhandled exception",
        'java.lang.NullPointerException: Cannot invoke "String.length()"',
        "\tat com.app.Service.validate(Service.java:88)",
        "\tat com.app.Ctl.create(Ctl.java:41)",
        "Caused by: java.io.IOException: boom",
        "\t... 12 more",
        "2026-11-18 14:02:12.000 INFO  [main] c.a.App - started",
    ])
    assert len(recs) == 2
    assert recs[0].line_count == 6
    assert "NullPointerException" in recs[0].signature
    assert "IOException" in recs[0].signature


def test_different_exceptions_have_different_signatures():
    a, b = records([
        "2026-11-18 14:02:11 ERROR job failed",
        "java.lang.NullPointerException: x",
        "\tat a.B.c(B.java:1)",
        "2026-11-18 14:02:12 ERROR job failed",
        "java.lang.IllegalStateException: y",
        "\tat a.B.c(B.java:1)",
    ])
    assert a.signature != b.signature


def test_lines_without_timestamps_are_separate_records():
    recs = records([
        "starting up",
        "listening on port 8080",
        "ERROR could not open config",
    ])
    assert len(recs) == 3
    assert recs[2].level == "ERROR"


def test_docker_prefixed_lines_keep_source_timestamp():
    ts = datetime(2026, 11, 18, 14, 2, 11, tzinfo=timezone.utc)
    recs = records([
        RawLine("ERROR request failed", ts),
        RawLine("Traceback (most recent call last):", ts),
        RawLine('  File "/app/x.py", line 3, in f', ts),
        RawLine("KeyError: 'id'", ts),
        RawLine("INFO next", ts),
    ])
    assert len(recs) == 2
    assert recs[0].timestamp == ts
    assert recs[0].line_count == 4  # unusual exception names are still attached


def test_runaway_record_is_capped():
    lines = ["ERROR boom"] + [f"  frame {i}" for i in range(1000)]
    (rec,) = records(lines, max_lines=50)
    assert rec.line_count == 50


def test_blank_lines_are_ignored():
    assert len(records(["", "  ", "INFO a", "", "INFO b"])) == 2


def test_detect_level_variants():
    assert detect_level("2026/11/18 [error] 12#12: oops") == "ERROR"
    assert detect_level("WARNING disk almost full") == "WARN"
    assert detect_level("CRITICAL db gone") == "FATAL"
    assert detect_level('1.2.3.4 - - [x] "GET / HTTP/1.1" 502 157') == "ERROR"
    assert detect_level('1.2.3.4 - - [x] "GET / HTTP/1.1" 404 157') == "WARN"
    assert detect_level('1.2.3.4 - - [x] "GET / HTTP/1.1" 200 157') == "INFO"
    assert detect_level("connection refused by upstream") == "ERROR"  # no explicit level
    assert detect_level("server started") == "INFO"
    assert detect_level("INFO handled 0 errors") == "INFO"  # explicit level wins


def test_parse_timestamp_variants():
    assert parse_timestamp("2026-11-18T14:02:11.123456789Z") == datetime(
        2026, 11, 18, 14, 2, 11, 123456, tzinfo=timezone.utc
    )
    assert parse_timestamp("2026/11/18 14:02:11") == datetime(2026, 11, 18, 14, 2, 11)
    assert parse_timestamp("2026-11-18 14:02:11,123") == datetime(2026, 11, 18, 14, 2, 11, 123000)
    assert parse_timestamp("2026-11-18T14:02:11+0800").utcoffset().total_seconds() == 8 * 3600
    assert parse_timestamp("garbage") is None
