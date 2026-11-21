import json
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from strelizia.assembler import Assembler, assemble
from strelizia.cli import app
from strelizia.demo import generate
from strelizia.formats import ALL_FORMATS, formats_for, parse_line
from strelizia.models import LogRecord, RawLine
from strelizia.pipeline import analyze_lines

runner = CliRunner()

ACCESS = '203.0.113.7 - - [18/Nov/2026:14:02:11 +0000] "GET /api/orders/42?token=abc HTTP/1.1" {status} 157 "-" "Mozilla/5.0 (X11)"'
NGINX_ERR = ('2026/11/18 14:02:11 [{lvl}] 1234#1234: *5678 connect() failed (111: Connection refused) while connecting '
             'to upstream, client: 1.2.3.4, server: example.com, request: "GET /api HTTP/1.1", '
             'upstream: "http://127.0.0.1:8080/api", host: "example.com"')


# ------------------------------------------------------------------ nginx


@pytest.mark.parametrize("status,level", [(200, "INFO"), (301, "INFO"), (401, "WARN"), (404, "WARN"), (499, "WARN"), (502, "ERROR"), (504, "ERROR")])
def test_nginx_access_levels_follow_the_status(status, level):
    p = parse_line(ACCESS.format(status=status))
    assert p.level == level
    assert p.key == f"nginx GET /api/orders/42 returned HTTP {status}"


def test_nginx_access_drops_noise_but_keeps_the_original_line():
    p = parse_line(ACCESS.format(status=502))
    assert "203.0.113.7" not in p.key and "Mozilla" not in p.key and "token" not in p.key
    assert "203.0.113.7" in p.message and "Mozilla" in p.message  # still there for samples (redacted when sent)


def test_nginx_access_timestamp_is_parsed_lazily_with_its_timezone():
    rec = next(assemble([ACCESS.format(status=502).replace("+0000", "+0800")]))
    assert rec.timestamp is None and rec.ts_text is not None
    assert rec.resolve_timestamp() == datetime(2026, 11, 18, 14, 2, 11, tzinfo=timezone(timedelta(hours=8)))


def test_nginx_access_handles_junk_requests():
    p = parse_line('1.2.3.4 - - [18/Nov/2026:14:02:11 +0000] "\\x16\\x03\\x01" 400 150 "-" "-"')
    assert p.level == "WARN" and p.key.endswith("returned HTTP 400")
    assert parse_line("not an access line at all") is None


@pytest.mark.parametrize("lvl,expected", [("emerg", "FATAL"), ("crit", "FATAL"), ("error", "ERROR"), ("warn", "WARN"), ("notice", "INFO")])
def test_nginx_error_levels(lvl, expected):
    assert parse_line(NGINX_ERR.format(lvl=lvl)).level == expected


def test_nginx_error_key_keeps_the_cause_and_the_failing_upstream_only():
    p = parse_line(NGINX_ERR.format(lvl="error"))
    assert p.key == "nginx connect() failed (111: Connection refused) while connecting to upstream upstream=127.0.0.1:8080"
    assert "example.com" not in p.key and "client" not in p.key
    assert p.message.startswith("[error] 1234#1234:")  # timestamp removed, like plain lines
    assert p.ts_text == "2026/11/18 14:02:11"


# ------------------------------------------------------------------ JSON


def test_json_pino_style_numeric_level_and_millisecond_time():
    p = parse_line(json.dumps({"level": 50, "time": 1731938531123, "msg": "db down", "pid": 1, "hostname": "h"}))
    assert p.level == "ERROR" and p.key == "ERROR db down"
    assert p.ts == datetime(2024, 11, 18, 14, 2, 11, 123000, tzinfo=timezone.utc)
    assert "pid" not in p.message and "hostname" not in p.message


@pytest.mark.parametrize("level,expected", [(10, "DEBUG"), (30, "INFO"), (40, "WARN"), (50, "ERROR"), (60, "FATAL"),
                                            ("warning", "WARN"), ("CRITICAL", "FATAL"), ("err", "ERROR"), ("3", "ERROR")])
def test_json_level_variants(level, expected):
    assert parse_line(json.dumps({"level": level, "msg": "x"})).level == expected


def test_json_zap_style_float_seconds_and_error_field():
    p = parse_line(json.dumps({"level": "error", "ts": 1731938531.5, "msg": "request failed", "error": "dial tcp 10.0.0.9:443: i/o timeout"}))
    assert p.key == "ERROR request failed | dial tcp 10.0.0.9:443: i/o timeout"
    assert p.ts.year == 2024 and p.ts.microsecond == 500000


def test_json_structlog_and_logrus_iso_times_are_lazy():
    p = parse_line(json.dumps({"event": "cache miss", "level": "warning", "timestamp": "2026-11-18T14:02:11.123456Z"}))
    assert p.level == "WARN" and p.key == "WARN cache miss" and p.ts is None and p.ts_text.startswith("2026-11-18")
    q = parse_line(json.dumps({"level": "warning", "msg": "slow", "time": "2026-11-18T14:02:11+08:00"}))
    assert q.level == "WARN"


def test_json_journald_fields():
    p = parse_line(json.dumps({"PRIORITY": "3", "MESSAGE": "Out of memory: Killed process 4242 (java)",
                               "SYSLOG_IDENTIFIER": "kernel", "__REALTIME_TIMESTAMP": "1731938531123456", "_PID": "1"}))
    assert p.level == "ERROR" and "Killed process" in p.key
    assert p.ts == datetime(2024, 11, 18, 14, 2, 11, 123456, tzinfo=timezone.utc)
    assert "_PID" not in p.message
    binary = parse_line(json.dumps({"PRIORITY": "6", "MESSAGE": list(b"hello bytes")}))
    assert binary.key == "INFO hello bytes"


def test_json_error_objects_and_stack_traces():
    rec = {"level": "error", "msg": "unhandled", "err": {"type": "TypeError", "message": "x is undefined",
           "stack": "TypeError: x is undefined\n    at handler (/app/a.js:1:1)\n    at next (/app/b.js:2:2)"}}
    p = parse_line(json.dumps(rec))
    assert "x is undefined" in p.key and p.key.endswith("@ at handler (/app/a.js:1:1)")
    assert "\n" in p.message and "at next (/app/b.js:2:2)" in p.message  # the stack is kept for samples


def test_json_extra_fields_are_shown_but_capped():
    rec = {"level": "info", "msg": "m", **{f"k{i}": i for i in range(20)}}
    p = parse_line(json.dumps(rec))
    assert p.message.count("=") == 6


def test_json_without_a_level_falls_back_to_the_text():
    assert parse_line(json.dumps({"msg": "connection refused by upstream"})).level == "ERROR"
    assert parse_line(json.dumps({"msg": "all good"})).level == "INFO"


@pytest.mark.parametrize("line", ['{"a": 1}', "[1, 2]", '{"broken": ', '{"level": true}', "{}"])
def test_json_that_is_not_a_log_record_is_left_alone(line):
    assert parse_line(line) is None


# ------------------------------------------------------------------ syslog


def test_syslog_3164_without_priority_uses_the_text_for_level():
    p = parse_line("Nov 18 14:02:11 web1 sshd[812]: Failed password for invalid user admin from 1.2.3.4 port 22 ssh2")
    assert p.level == "ERROR" and p.key == "sshd: Failed password for invalid user admin from 1.2.3.4 port 22 ssh2"
    assert "web1" not in p.key and "812" not in p.key
    ok = parse_line("Nov 18 14:02:11 web1 sshd[812]: Accepted publickey for deploy from 1.2.3.4 port 22 ssh2")
    assert ok.level == "INFO"


@pytest.mark.parametrize("pri,level", [(11, "ERROR"), (12, "WARN"), (14, "INFO"), (15, "DEBUG"), (10, "FATAL"), (8, "FATAL")])
def test_syslog_priority_sets_the_level(pri, level):
    assert parse_line(f"<{pri}>Nov 18 14:02:11 web1 app: something happened").level == level


def test_syslog_5424():
    p = parse_line("<27>1 2026-11-18T14:02:11.003Z web1 myapp 4321 ID47 - database connection lost")
    assert p.level == "ERROR" and p.key == "myapp: database connection lost"
    assert p.ts_text == "2026-11-18T14:02:11.003Z"
    structured = parse_line('<27>1 2026-11-18T14:02:11Z web1 myapp - - [exampleSDID@1 iut="3"] lost it')
    assert structured.key == "myapp: lost it"


def test_syslog_time_without_a_year_assumes_this_year_or_last():
    now = datetime.now()
    recent = (now - timedelta(days=3)).strftime("%b %d %H:%M:%S")
    rec = next(assemble([f"{recent} h app: ERROR boom"]))
    assert rec.resolve_timestamp().year == now.year or (now.month == 1 and rec.resolve_timestamp().year == now.year - 1)
    future = (now + timedelta(days=40)).strftime("%b %d %H:%M:%S")  # would be in the future this year
    rec2 = next(assemble([f"{future} h app: ERROR boom"]))
    assert rec2.resolve_timestamp().year == now.year - 1


# ------------------------------------------------------------------ selecting adapters


def test_formats_for_choices():
    assert formats_for("auto") == ALL_FORMATS and formats_for("plain") == frozenset() and formats_for("json") == {"json"}
    with pytest.raises(ValueError, match="Choose from"):
        formats_for("xml")


def test_forced_formats_only_try_their_own_adapter():
    j = json.dumps({"level": "error", "msg": "x"})
    assert parse_line(j, frozenset({"nginx"})) is None
    assert parse_line(j, frozenset({"json"})) is not None
    assert parse_line(ACCESS.format(status=502), frozenset({"json", "syslog"})) is None
    assert parse_line(j, frozenset()) is None


def test_plain_lines_are_not_mistaken_for_structured_ones():
    for line in ["2026-11-18 14:02:11 ERROR connect ECONNREFUSED", "ERROR something", "Traceback (most recent call last):",
                 '  File "/app/x.py", line 3, in f', "<html>not a log</html>", "Mon Nov 18 whatever", "{not json"]:
        assert parse_line(line) is None, line


# ------------------------------------------------------------------ assembler integration


def test_structured_lines_flush_the_open_multiline_record_first_and_keep_order():
    a = Assembler("s")
    out = []
    for line in ["2026-11-18 14:02:11 ERROR request failed", "Traceback (most recent call last):", "ValueError: bad",
                 json.dumps({"level": "warn", "msg": "after the trace"}), "2026-11-18 14:02:12 INFO next"]:
        out += a.feed(line)
    last = a.flush()
    out += [last] if last else []
    assert [r.level for r in out] == ["ERROR", "WARN", "INFO"]
    assert out[0].line_count == 3 and out[1].key == "WARN after the trace"


def test_mixed_stream_from_one_container():
    lines = [ACCESS.format(status=502), NGINX_ERR.format(lvl="error"), json.dumps({"level": "error", "msg": "app"}),
             "2026-11-18 14:02:11 ERROR plain", "Nov 18 14:02:11 h sshd[1]: Failed password for root"]
    recs = list(assemble(lines))
    assert [r.signature.split()[0] for r in recs] == ["nginx", "nginx", "ERROR", "ERROR", "sshd:"]


def test_docker_timestamp_is_used_when_the_line_has_none_of_its_own():
    ts = datetime(2026, 11, 18, 14, 2, 11, tzinfo=timezone.utc)
    (rec,) = assemble([RawLine(json.dumps({"level": "error", "msg": "x"}), ts)])
    assert rec.timestamp == ts


def test_adapters_can_be_switched_off():
    (rec,) = assemble([json.dumps({"level": "error", "msg": "x"})], formats=frozenset())
    assert rec.key is None and rec.level == "ERROR"  # plain heuristics still see the word "error"


def test_signature_prefers_the_adapters_key():
    assert LogRecord(message="long noisy line", key="clean key").signature == "clean key"


# ------------------------------------------------------------------ clustering with adapters


def test_nginx_clusters_by_problem_not_by_visitor():
    lines = [f'10.0.{i % 250}.{i % 200} - - [18/Nov/2026:14:02:11 +0000] "GET /api/orders/{i} HTTP/1.1" 502 {i} "-" "UA-{i}"' for i in range(500)]
    clusters, _ = analyze_lines(lines)
    assert len(clusters) == 1 and clusters[0].count == 500
    assert clusters[0].template == "nginx GET /api/orders/<NUM> returned HTTP 502"


def test_502_and_504_stay_separate_and_404_is_a_different_level():
    lines = []
    for status in (502, 504, 404):
        lines += [ACCESS.format(status=status).replace("42", str(i)) for i in range(30)]
    clusters, _ = analyze_lines(lines, min_level="warn")
    assert sorted(c.template.split()[-1] for c in clusters) == ["404", "502", "504"]


def test_json_logs_group_by_message_not_by_ids():
    clusters, stats = analyze_lines(generate("json-api"))
    templates = [c.template for c in clusters]
    assert len(clusters) == 3
    assert any(t.startswith("ERROR payment provider timeout") for t in templates)
    assert any("unhandled exception" in t and "orders.js" in t for t in templates)


def test_adapters_beat_plain_heuristics_on_nginx_logs():
    lines = generate("nginx-502")
    with_adapters, _ = analyze_lines(lines)
    without, _ = analyze_lines(lines, formats=frozenset())
    assert not any("Mozilla" in c.template for c in with_adapters)
    assert any("Mozilla" in c.template for c in without)


# ------------------------------------------------------------------ CLI


def test_cli_format_option(tmp_path):
    log = tmp_path / "access.log"
    log.write_text("\n".join(ACCESS.format(status=502) for _ in range(10)))
    auto = runner.invoke(app, ["analyze", str(log)], env={"COLUMNS": "200"})
    plain = runner.invoke(app, ["analyze", str(log), "--format", "plain"], env={"COLUMNS": "200"})
    assert "returned HTTP 502" in auto.output and "returned HTTP" not in plain.output
    assert runner.invoke(app, ["analyze", str(log), "--format", "xml"]).exit_code == 2
    assert runner.invoke(app, ["watch", str(log), "--format", "xml"]).exit_code == 2
