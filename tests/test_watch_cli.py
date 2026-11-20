import json

from typer.testing import CliRunner

from strelizia.cli import app
from strelizia.sources import SourceError

runner = CliRunner()
WIDE = {"COLUMNS": "200"}

ERRORS = "\n".join(
    f"2026-11-18 10:00:{i:02d} ERROR payment gateway timeout after {1000 + i}ms (order {i})" for i in range(6)
) + "\n"


def flat(result) -> str:
    return " ".join(result.output.split())


def alerts_in(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------ argument handling


def test_nothing_to_watch_is_an_error():
    r = runner.invoke(app, ["watch"], env=WIDE)
    assert r.exit_code == 2 and "Nothing to watch" in flat(r)


def test_bad_values_are_clear_errors():
    base = ["watch", "--demo", "spike", "--duration", "1s"]
    assert runner.invoke(app, base + ["--interval", "soon"], env=WIDE).exit_code == 2
    assert runner.invoke(app, base + ["--interval", "0s"], env=WIDE).exit_code == 2
    assert runner.invoke(app, base + ["--cooldown", "x"], env=WIDE).exit_code == 2
    assert runner.invoke(app, base + ["--quiet-hours", "night"], env=WIDE).exit_code == 2
    assert runner.invoke(app, base + ["--alert-level", "loud"], env=WIDE).exit_code == 2
    assert runner.invoke(app, ["watch", "--demo", "nope"], env=WIDE).exit_code == 2


def test_a_bad_notify_url_is_rejected_without_echoing_secrets():
    r = runner.invoke(app, ["watch", "--demo", "spike", "--notify", "ftp://SECRETTOKEN@host/x"], env=WIDE)
    assert r.exit_code == 2 and "SECRETTOKEN" not in r.output


def test_missing_docker_container_fails_fast(monkeypatch):
    def missing(name):
        raise SourceError(f"No container named or with id {name!r}. Check `docker ps -a`.")

    monkeypatch.setattr("strelizia.cli.inspect_container", missing)
    r = runner.invoke(app, ["watch", "--docker", "nope"], env=WIDE)
    assert r.exit_code == 1 and "docker ps -a" in flat(r)


def test_explain_needs_a_model():
    r = runner.invoke(app, ["watch", "--demo", "spike", "--explain"], env=WIDE)
    assert r.exit_code == 2 and "No model chosen" in flat(r)


# ------------------------------------------------------------------ the live demo, end to end


def test_demo_stream_raises_startup_spike_and_new_alerts(tmp_path):
    out = tmp_path / "alerts.jsonl"
    r = runner.invoke(app, ["watch", "--demo", "spike", "--interval", "0.4s", "--duration", "6s",
                            "--notify", f"file://{out}"], env=WIDE)
    assert r.exit_code == 0, r.output
    text = flat(r)
    assert "Already active" in text and "Spike" in text and "New" in text
    assert "sent to file" in text
    kinds = [a["kind"] for line in alerts_in(out) for a in line["alerts"]]
    assert kinds.count("STARTUP") == 1 and "SPIKE" in kinds and "NEW" in kinds
    assert kinds.count("SPIKE") == 1, "one incident must produce one spike alert, not one per window"


def test_dry_run_shows_alerts_but_sends_nothing(tmp_path):
    out = tmp_path / "alerts.jsonl"
    r = runner.invoke(app, ["watch", "--demo", "spike", "--interval", "0.4s", "--duration", "3s",
                            "--dry-run", "--notify", f"file://{out}"], env=WIDE)
    assert r.exit_code == 0, r.output
    assert "dry run" in flat(r).lower() and not out.exists()


def test_quiet_flag_hides_idle_windows(tmp_path):
    noisy = runner.invoke(app, ["watch", "--demo", "spike", "--interval", "0.4s", "--duration", "3s"], env=WIDE)
    quiet = runner.invoke(app, ["watch", "--demo", "spike", "--interval", "0.4s", "--duration", "3s", "--quiet"], env=WIDE)
    assert noisy.output.count("records") > quiet.output.count("records")
    assert "Already active" in quiet.output  # alerts still show


def test_notify_destinations_can_come_from_the_environment(tmp_path):
    out = tmp_path / "env.jsonl"
    env = {**WIDE, "STRELIZIA_NOTIFY": f"file://{out}"}
    r = runner.invoke(app, ["watch", "--demo", "spike", "--interval", "0.4s", "--duration", "2s"], env=env)
    assert r.exit_code == 0 and out.exists()


def test_quiet_hours_hold_alerts(tmp_path):
    out = tmp_path / "alerts.jsonl"
    r = runner.invoke(app, ["watch", "--demo", "spike", "--interval", "0.4s", "--duration", "2s",
                            "--quiet-hours", "00:00-23:59", "--notify", f"file://{out}"], env=WIDE)
    assert r.exit_code == 0
    # (only fails if the test happens to run in the final minute of the day)
    assert "held until quiet hours end" in flat(r) and not out.exists()


# ------------------------------------------------------------------ files and memory


def test_file_watch_remembers_what_it_has_seen(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(ERRORS)
    state = tmp_path / "state.db"
    out = tmp_path / "alerts.jsonl"
    args = ["watch", str(log), "--from-start", "--interval", "0.3s", "--duration", "1.2s",
            "--state", str(state), "--notify", f"file://{out}"]
    first = runner.invoke(app, args, env=WIDE)
    assert first.exit_code == 0, first.output
    assert [a["kind"] for line in alerts_in(out) for a in line["alerts"]] == ["STARTUP"]

    second = runner.invoke(app, args, env=WIDE)  # same log, same memory: nothing is news any more
    assert second.exit_code == 0
    assert len(alerts_in(out)) == 1

    log.write_text(ERRORS + "2026-11-18 10:01:00 ERROR cannot connect to redis at host\n")
    third = runner.invoke(app, args, env=WIDE)
    assert third.exit_code == 0
    kinds = [a["kind"] for line in alerts_in(out) for a in line["alerts"]]
    assert kinds == ["STARTUP", "NEW"]


def test_alert_level_filters_what_can_alert(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(ERRORS)
    out = tmp_path / "alerts.jsonl"
    r = runner.invoke(app, ["watch", str(log), "--from-start", "--interval", "0.3s", "--duration", "1s",
                            "--alert-level", "fatal", "--state", str(tmp_path / "s.db"), "--notify", f"file://{out}"], env=WIDE)
    assert r.exit_code == 0 and not out.exists()


def test_a_file_that_does_not_exist_yet_is_waited_for(tmp_path):
    r = runner.invoke(app, ["watch", str(tmp_path / "later.log"), "--interval", "0.3s", "--duration", "0.8s",
                            "--state", str(tmp_path / "s.db")], env=WIDE)
    assert r.exit_code == 0 and "Waiting for it to appear" in flat(r)


def test_alerts_carry_a_diagnosis_when_asked(tmp_path, fake_llm):
    log = tmp_path / "app.log"
    log.write_text(ERRORS)
    out = tmp_path / "alerts.jsonl"
    env = {**WIDE, "OLLAMA_HOST": fake_llm.host_port}
    r = runner.invoke(app, ["watch", str(log), "--from-start", "--interval", "0.3s", "--duration", "1.2s",
                            "--explain", "--model", "m", "--state", str(tmp_path / "s.db"),
                            "--notify", f"file://{out}"], env=env)
    assert r.exit_code == 0, r.output
    assert "🧠" in alerts_in(out)[0]["text"] and "unreachable" in alerts_in(out)[0]["text"]
    assert len(fake_llm.requests) == 1


def test_a_down_model_does_not_stop_alerts(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(ERRORS)
    out = tmp_path / "alerts.jsonl"
    env = {**WIDE, "OLLAMA_HOST": "127.0.0.1:9"}
    r = runner.invoke(app, ["watch", str(log), "--from-start", "--interval", "0.3s", "--duration", "1.2s",
                            "--explain", "--model", "m", "--state", str(tmp_path / "s.db"),
                            "--notify", f"file://{out}"], env=env)
    assert r.exit_code == 0, r.output
    assert len(alerts_in(out)) == 1 and "diagnosis unavailable" in flat(r)


# ------------------------------------------------------------------ history


def test_history_without_a_state_file(tmp_path):
    r = runner.invoke(app, ["history", "--state", str(tmp_path / "none.db")], env=WIDE)
    assert r.exit_code == 0 and "No alert history yet" in flat(r)


def test_history_lists_alerts(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(ERRORS)
    state = tmp_path / "state.db"
    runner.invoke(app, ["watch", str(log), "--from-start", "--interval", "0.3s", "--duration", "1s", "--state", str(state)], env=WIDE)
    r = runner.invoke(app, ["history", "--state", str(state)], env=WIDE)
    assert r.exit_code == 0
    text = flat(r)
    assert "STARTUP" in text and "ERROR" in text and "console" in text
