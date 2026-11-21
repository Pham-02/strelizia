import json
from datetime import datetime, time

from typer.testing import CliRunner

from strelizia.cli import app
from strelizia.digest import build_digest
from strelizia.models import LogRecord
from strelizia.notify import Message, Notifier
from strelizia.store import Store
from strelizia.watch import Engine, WatchConfig

runner = CliRunner()
BASE = datetime(2026, 11, 18, 12, 0, 0).timestamp()
GATEWAY = "ERROR payment gateway timeout after {n}ms"
DISK = "ERROR disk quota exceeded for tenant {n}"


class Capture(Notifier):
    label = "capture"

    def __init__(self):
        self.messages: list[Message] = []

    def send(self, message):
        self.messages.append(message)


def recs(template, n, level="ERROR"):
    return [LogRecord(message=template.format(n=i), level=level) for i in range(n)]


def make(**kw):
    cfg = WatchConfig(interval=60, min_history=300, **kw)
    cap = Capture()
    return Engine(Store(), [cap], cfg), cap


def win(engine, i, records, base=BASE, source="api"):
    return engine.process_window(source, records, base + i * 60, base + (i + 1) * 60)


def kinds(r):
    return [a.kind for a in r.alerts]


# ------------------------------------------------------------------ resolved: pattern disappears


def test_a_sustained_pattern_that_goes_quiet_is_announced_once():
    engine, cap = make(resolve_after=300)
    win(engine, 0, recs(GATEWAY, 2))
    assert kinds(win(engine, 1, recs(GATEWAY, 2) + recs(DISK, 6))) == ["NEW"]
    for i in range(2, 6):
        assert kinds(win(engine, i, recs(GATEWAY, 2))) == [], f"resolved too early at {i}"
    r = win(engine, 6, recs(GATEWAY, 2))  # five minutes after the last occurrence
    assert kinds(r) == ["RESOLVED"] and "disk quota" in r.alerts[0].template
    assert "quiet for" in r.alerts[0].note
    assert cap.messages[-1].level == "INFO" and cap.messages[-1].title == "api: 1 resolved pattern"
    assert "✅ Resolved" in cap.messages[-1].text
    assert kinds(win(engine, 7, recs(GATEWAY, 2))) == []  # only once


def test_a_one_off_error_is_closed_silently():
    engine, cap = make(resolve_after=300)
    win(engine, 0, recs(GATEWAY, 2))
    assert kinds(win(engine, 1, recs(DISK, 1))) == ["NEW"]  # a single occurrence
    for i in range(2, 9):
        assert kinds(win(engine, i, recs(GATEWAY, 2))) == []
    assert len(cap.messages) == 2  # startup + new, and no "resolved" for something that barely happened
    assert all(a.status != "silent" for a in engine.store.recent_alerts(10))
    assert not [p for p, _ in engine.store.open_alerts("api") if "disk" in p.template]


def test_resolve_after_zero_disables_it():
    engine, cap = make(resolve_after=0)
    win(engine, 0, recs(GATEWAY, 2))
    win(engine, 1, recs(DISK, 6))
    for i in range(2, 30):
        assert kinds(win(engine, i, recs(GATEWAY, 2))) == []


def test_resolved_notices_follow_quiet_hours():
    engine, cap = make(resolve_after=120, quiet_hours=(time(23, 0), time(7, 0)))
    night = datetime(2026, 11, 18, 23, 10).timestamp()
    win(engine, 0, recs(GATEWAY, 2), base=night)
    win(engine, 1, recs(DISK, 6), base=night)
    for i in range(2, 6):
        win(engine, i, recs(GATEWAY, 2), base=night)
    assert cap.messages == []  # everything was held
    held_kinds = {a.kind for a in engine.store.held_alerts()}
    assert "RESOLVED" in held_kinds


# ------------------------------------------------------------------ resolved: a spike calms down


def steady(engine, n=12):
    for i in range(n):
        win(engine, i, recs(GATEWAY, 2))


def test_a_spike_that_calms_down_is_announced():
    engine, cap = make(resolve_after=120)
    steady(engine)
    assert kinds(win(engine, 12, recs(GATEWAY, 100))) == ["SPIKE"]
    assert kinds(win(engine, 13, recs(GATEWAY, 2))) == []  # calm for one minute: not yet
    r = win(engine, 14, recs(GATEWAY, 2))
    assert kinds(r) == ["RESOLVED"] and "back down to" in r.alerts[0].note and "was 100/min" in r.alerts[0].note


def test_a_flapping_spike_restarts_the_calm_timer():
    engine, _ = make(resolve_after=120)
    steady(engine)
    win(engine, 12, recs(GATEWAY, 100))
    win(engine, 13, recs(GATEWAY, 2))  # calm...
    assert kinds(win(engine, 14, recs(GATEWAY, 100))) == []  # ...flares up again (same incident, no new spike alert)
    assert kinds(win(engine, 15, recs(GATEWAY, 2))) == []  # the timer started over
    assert kinds(win(engine, 16, recs(GATEWAY, 2))) == ["RESOLVED"]


def test_a_new_spike_after_resolution_alerts_again():
    engine, _ = make(resolve_after=120)
    steady(engine)
    win(engine, 12, recs(GATEWAY, 100))
    win(engine, 13, recs(GATEWAY, 2))
    assert kinds(win(engine, 14, recs(GATEWAY, 2))) == ["RESOLVED"]
    assert kinds(win(engine, 15, recs(GATEWAY, 120))) == ["SPIKE"]


# ------------------------------------------------------------------ digest


def run_day(engine, base, windows=100):
    for i in range(windows):
        records = recs(GATEWAY, 2)
        if i == 40:
            records += recs(DISK, 6)
        win(engine, i, records, base=base)


def test_digest_content():
    engine, cap = make(resolve_after=0)
    for i in range(70):
        records = recs(GATEWAY, 2)
        if i == 40:
            records += recs("ERROR login failed for jane@corp.com from host", 6)
        win(engine, i, records)
    message = build_digest(engine.store, BASE + 70 * 60, hours=24)
    assert message.title == "Strelizia daily digest · last 24h"
    assert "api: " in message.text and "warnings and errors in 2 patterns" in message.text
    assert "alerts" in message.text and "(new)" in message.text
    assert "140×  ERROR  ERROR payment gateway" in message.text
    assert "jane@corp.com" not in message.text and "<EMAIL_1>" in message.text


def test_digest_on_a_quiet_system():
    engine, _ = make()
    assert build_digest(engine.store, BASE, 24).text == "✅ Quiet period: no warnings or errors, and no alerts."


def test_digest_is_sent_once_a_day_at_the_chosen_time():
    cfg = WatchConfig(interval=60, min_history=300, digest_at=time(9, 0), resolve_after=0)
    cap = Capture()
    engine = Engine(Store(), [cap], cfg)
    morning = datetime(2026, 11, 18, 7, 30).timestamp()
    digests = []
    for i in range(130):  # 07:30 to 09:40
        r = win(engine, i, recs(GATEWAY, 2), base=morning)
        if r.digest:
            digests.append(i)
    assert digests == [89]  # the window ending at 09:00
    assert [m.title for m in cap.messages].count("Strelizia daily digest · last 24h") == 1
    last = engine.store.recent_alerts(50)
    assert any(a.kind == "DIGEST" and a.status == "sent" for a in last)
    # the next day at 09:00 it is sent again
    next_morning = datetime(2026, 11, 19, 8, 0).timestamp()
    again = [i for i in range(90) if win(engine, i, recs(GATEWAY, 2), base=next_morning).digest]
    assert again == [59]


def test_digest_waits_for_a_meaningful_amount_of_data():
    cfg = WatchConfig(interval=60, min_history=300, digest_at=time(9, 0))
    engine = Engine(Store(), [Capture()], cfg)
    start = datetime(2026, 11, 18, 8, 55).timestamp()  # only five minutes before the digest time
    assert not any(win(engine, i, recs(GATEWAY, 2), base=start).digest for i in range(30))


def test_digest_in_quiet_hours_is_held_and_delivered_later():
    cfg = WatchConfig(interval=60, min_history=300, digest_at=time(9, 0), resolve_after=0,
                      quiet_hours=(time(8, 30), time(9, 30)))
    cap = Capture()
    engine = Engine(Store(), [cap], cfg)
    morning = datetime(2026, 11, 18, 7, 30).timestamp()
    for i in range(160):  # until 10:10
        win(engine, i, recs(GATEWAY, 2), base=morning)
    titles = [m.title for m in cap.messages]
    assert any("delayed alert" in t for t in titles)
    delayed_text = " ".join(m.text for m in cap.messages)
    assert "Strelizia daily digest" in delayed_text


# ------------------------------------------------------------------ the digest command


def test_digest_command(tmp_path):
    state = tmp_path / "state.db"
    store = Store(state)
    engine = Engine(store, [], WatchConfig(interval=60, min_history=300, resolve_after=0))
    now = datetime.now().timestamp()
    for i in range(70):
        engine.process_window("api", recs(GATEWAY, 2), now - (70 - i) * 60, now - (69 - i) * 60)
    store.close()
    out = tmp_path / "digest.jsonl"
    r = runner.invoke(app, ["digest", "--state", str(state), "--notify", f"file://{out}"], env={"COLUMNS": "200"})
    assert r.exit_code == 0, r.output
    assert "daily digest" in r.output and "api: 140 warnings and errors in 1 pattern" in " ".join(r.output.split())
    assert json.loads(out.read_text())["title"].startswith("Strelizia daily digest")


def test_digest_command_without_history_or_with_bad_input(tmp_path):
    r = runner.invoke(app, ["digest", "--state", str(tmp_path / "none.db")])
    assert r.exit_code == 0 and "Nothing to summarise" in " ".join(r.output.split())
    Store(tmp_path / "s.db").close()
    assert runner.invoke(app, ["digest", "--state", str(tmp_path / "s.db"), "--notify", "ftp://x"]).exit_code == 2


def test_watch_option_validation():
    base = ["watch", "--demo", "spike", "--duration", "1s"]
    assert runner.invoke(app, base + ["--digest-at", "nine"]).exit_code == 2
    assert runner.invoke(app, base + ["--resolve-after", "soon"]).exit_code == 2


def test_demo_watch_shows_the_whole_incident_lifecycle():
    r = runner.invoke(app, ["watch", "--demo", "spike", "--interval", "0.4s", "--duration", "6.5s"], env={"COLUMNS": "200"})
    assert r.exit_code == 0, r.output
    text = " ".join(r.output.split())
    assert "Already active" in text and "Spike" in text and "New" in text and "Resolved" in text
