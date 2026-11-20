from datetime import datetime, time

from strelizia.models import LogRecord
from strelizia.notify import Message, Notifier, NotifyError
from strelizia.sources.docker import ContainerInfo
from strelizia.store import Store
from strelizia.watch import Engine, WatchConfig

BASE = datetime(2026, 11, 18, 12, 0, 0).timestamp()  # noon, local time: never inside quiet hours
GATEWAY = "ERROR payment gateway timeout after {n}ms"
DISK = "ERROR disk quota exceeded for tenant {n}"
REDIS = "ERROR cannot connect to redis at host"


class Capture(Notifier):
    label = "capture"

    def __init__(self):
        self.messages: list[Message] = []

    def send(self, message):
        self.messages.append(message)


class Flaky(Notifier):
    label = "flaky"

    def __init__(self):
        self.up = False
        self.messages = []

    def send(self, message):
        if not self.up:
            raise NotifyError("flaky: could not connect (ConnectionRefusedError)")
        self.messages.append(message)


def recs(template, n, level="ERROR"):
    return [LogRecord(message=template.format(n=i), level=level) for i in range(n)]


def make(cfg=None, notifiers=None, **kw):
    cfg = cfg or WatchConfig(interval=60, min_history=300, **kw)
    cap = Capture()
    engine = Engine(Store(), [cap] if notifiers is None else notifiers, cfg)
    return engine, cap


def win(engine, i, records, source="api", container=None, base=BASE):
    return engine.process_window(source, records, base + i * 60, base + (i + 1) * 60, container)


def kinds(result):
    return [a.kind for a in result.alerts]


# ------------------------------------------------------------------ startup and NEW


def test_first_window_gives_one_startup_summary_then_silence():
    engine, cap = make()
    r0 = win(engine, 0, recs(GATEWAY, 3))
    assert kinds(r0) == ["STARTUP"] and r0.status == "sent"
    assert len(cap.messages) == 1 and "already active" in cap.messages[0].title
    assert kinds(win(engine, 1, recs(GATEWAY, 3))) == []
    assert len(cap.messages) == 1


def test_new_pattern_alerts_once():
    engine, cap = make()
    win(engine, 0, recs(GATEWAY, 2))
    r1 = win(engine, 1, recs(GATEWAY, 2) + recs(DISK, 3))
    assert kinds(r1) == ["NEW"] and "disk quota" in r1.alerts[0].template
    assert kinds(win(engine, 2, recs(DISK, 3))) == []  # no longer new
    assert len(cap.messages) == 2


def test_same_pattern_worded_slightly_differently_is_not_new_again():
    engine, _ = make()
    win(engine, 0, [LogRecord(message="ERROR could not send mail to user bob at relay", level="ERROR")])
    r = win(engine, 1, [LogRecord(message="ERROR could not send mail to user amy at relay", level="ERROR")])
    assert kinds(r) == []


def test_patterns_below_the_alert_level_never_alert_but_are_remembered():
    engine, cap = make()
    win(engine, 0, recs(GATEWAY, 2))
    r = win(engine, 1, recs("WARN slow query took {n}ms on replica", 50, "WARN"))
    assert kinds(r) == [] and len(cap.messages) == 1
    assert any(p.level == "WARN" for p in engine.store.patterns())


def test_info_lines_are_not_patterns():
    engine, _ = make()
    r = win(engine, 0, recs("INFO request served in {n}ms", 100, "INFO"))
    assert r.patterns == 0 and r.records == 100 and kinds(r) == []


def test_one_message_per_window_even_with_several_alerts():
    engine, cap = make()
    win(engine, 0, recs(GATEWAY, 2))
    r = win(engine, 1, recs(DISK, 2) + recs(REDIS, 2))
    assert kinds(r) == ["NEW", "NEW"] and len(cap.messages) == 2  # startup + this one message
    assert cap.messages[1].title == "api: 2 new patterns"


def test_listing_is_capped():
    cfg = WatchConfig(interval=60, min_history=300, max_listed=1)
    engine, cap = make(cfg)
    win(engine, 0, recs(GATEWAY, 2))
    win(engine, 1, recs(DISK, 2) + recs(REDIS, 2))
    assert "and 1 more" in cap.messages[1].text


# ------------------------------------------------------------------ spikes


def steady(engine, windows, per_window=2):
    for i in range(windows):
        win(engine, i, recs(GATEWAY, per_window))


def test_spike_detected_against_the_usual_rate():
    engine, cap = make()
    steady(engine, 12)
    r = win(engine, 12, recs(GATEWAY, 100))
    assert kinds(r) == ["SPIKE"]
    a = r.alerts[0]
    assert a.count == 100 and 1.5 < a.baseline < 2.5 and a.rate == 100
    assert "usually about" in cap.messages[-1].text


def test_sustained_spike_does_not_repeat_but_a_much_worse_one_does():
    engine, cap = make()
    steady(engine, 12)
    assert kinds(win(engine, 12, recs(GATEWAY, 100))) == ["SPIKE"]
    assert kinds(win(engine, 13, recs(GATEWAY, 100))) == []  # same incident
    assert kinds(win(engine, 14, recs(GATEWAY, 800))) == ["SPIKE"]  # 8x worse than the last alert


def test_no_spike_judgement_without_enough_history():
    engine, _ = make(WatchConfig(interval=60, min_history=600))
    steady(engine, 4)
    assert kinds(win(engine, 4, recs(GATEWAY, 100))) == []


def test_a_handful_of_events_is_never_a_spike():
    engine, _ = make(WatchConfig(interval=60, min_history=300, min_count=5))
    for i in range(12):
        win(engine, i, [])
    win(engine, 12, recs(GATEWAY, 1))  # new pattern, alerts as NEW
    assert kinds(win(engine, 13, recs(GATEWAY, 4))) == []


def test_spike_can_alert_again_after_the_cooldown():
    engine, _ = make(WatchConfig(interval=60, min_history=300, cooldown=300))
    steady(engine, 12)
    assert kinds(win(engine, 12, recs(GATEWAY, 100))) == ["SPIKE"]
    for i in range(13, 19):
        win(engine, i, recs(GATEWAY, 2))
    assert kinds(win(engine, 19, recs(GATEWAY, 100))) == ["SPIKE"]


def test_spike_below_the_factor_is_ignored():
    engine, _ = make()
    steady(engine, 12, per_window=20)
    assert kinds(win(engine, 12, recs(GATEWAY, 60))) == []  # 3x, factor is 5


# ------------------------------------------------------------------ still happening


def test_still_happening_reminder_after_persistence_and_cooldown():
    engine, _ = make(WatchConfig(interval=60, min_history=300, persist_windows=3, cooldown=120))
    assert kinds(win(engine, 0, recs(GATEWAY, 2))) == ["STARTUP"]
    assert kinds(win(engine, 1, recs(GATEWAY, 2))) == []
    r2 = win(engine, 2, recs(GATEWAY, 2))
    assert kinds(r2) == ["STILL"] and "last 3 windows" in r2.alerts[0].note
    assert kinds(win(engine, 3, recs(GATEWAY, 2))) == []  # streak was reset


def test_a_pause_resets_the_streak():
    engine, _ = make(WatchConfig(interval=60, min_history=300, persist_windows=3, cooldown=60))
    win(engine, 0, recs(GATEWAY, 2))
    win(engine, 1, recs(GATEWAY, 2))
    win(engine, 2, [])  # quiet window
    assert kinds(win(engine, 3, recs(GATEWAY, 2))) == []


# ------------------------------------------------------------------ delivery, quiet hours, privacy


def test_quiet_hours_hold_alerts_and_send_one_digest_afterwards():
    cfg = WatchConfig(interval=60, min_history=300, quiet_hours=(time(23, 0), time(7, 0)))
    engine, cap = make(cfg)
    night = datetime(2026, 11, 18, 23, 30).timestamp()
    r = win(engine, 0, recs(GATEWAY, 2), base=night)
    r2 = win(engine, 1, recs(DISK, 2), base=night)
    assert r.status == "held" and r2.status == "held" and cap.messages == []
    assert len(engine.store.held_alerts()) == 2
    morning = datetime(2026, 11, 19, 8, 0).timestamp()
    r3 = win(engine, 0, [], base=morning)
    assert r3.delayed is not None and len(cap.messages) == 1
    assert "2 delayed alerts" in cap.messages[0].title
    assert engine.store.held_alerts() == []


def test_failed_delivery_is_retried_as_a_delayed_digest():
    flaky = Flaky()
    engine, _ = make(notifiers=[flaky])
    r0 = win(engine, 0, recs(GATEWAY, 2))
    assert r0.status == "failed" and r0.errors and len(engine.store.held_alerts()) == 1
    flaky.up = True
    r1 = win(engine, 1, [])
    assert r1.delayed is not None and len(flaky.messages) == 1
    assert engine.store.held_alerts() == []


def test_failed_delivery_does_not_count_for_cooldown():
    flaky = Flaky()
    engine, _ = make(notifiers=[flaky])
    win(engine, 0, recs(GATEWAY, 2))
    assert engine.store.last_alert(1) is None


def test_old_undelivered_alerts_are_dropped_not_sent_forever():
    flaky = Flaky()
    engine, _ = make(notifiers=[flaky])
    win(engine, 0, recs(GATEWAY, 2))
    for i in range(1, 30):  # 29 more alerts that cannot be delivered
        win(engine, i, [LogRecord(message=f"ERROR unique failure number{i} word{i} more{i} text{i} end{i}", level="ERROR")])
    flaky.up = True
    r = win(engine, 40, [])
    assert r.delayed is not None
    assert len(r.delayed.text.split("\n\n")) <= 20


def test_dry_run_sends_nothing_but_still_records():
    cap = Capture()
    engine = Engine(Store(), [cap], WatchConfig(interval=60, min_history=300), send=False)
    r = win(engine, 0, recs(GATEWAY, 2))
    assert r.status == "preview" and cap.messages == [] and r.message is not None
    assert engine.store.last_alert(1).status == "preview"


def test_console_only_when_no_destinations():
    engine, _ = make(notifiers=[])
    r = win(engine, 0, recs(GATEWAY, 2))
    assert r.status == "console" and r.message is not None


def test_alerts_are_redacted_before_delivery_and_before_storage():
    engine, cap = make()
    win(engine, 0, recs(GATEWAY, 2))
    leak = [LogRecord(message="ERROR login rejected for jane.doe@corp.com token=Zx81QmN0pLkVb2 from home", level="ERROR")]
    win(engine, 1, leak)
    text = cap.messages[1].text + str(cap.messages[1].alerts)
    assert "jane.doe" not in text and "Zx81QmN0pLkVb2" not in text and "<EMAIL_1>" in text
    stored = engine.store.recent_alerts(1)[0].body
    assert "jane.doe" not in stored


def test_samples_are_off_by_default_and_redacted_when_on():
    engine, cap = make()
    win(engine, 0, recs(GATEWAY, 2))
    rec = [LogRecord(message="ERROR upload failed for bob@corp.com after retries exhausted", level="ERROR")]
    win(engine, 1, rec)
    assert "e.g." not in cap.messages[1].text

    cfg = WatchConfig(interval=60, min_history=300, include_samples=True)
    engine2, cap2 = make(cfg)
    win(engine2, 0, recs(GATEWAY, 2))
    win(engine2, 1, rec)
    assert "e.g." in cap2.messages[1].text and "bob@corp.com" not in cap2.messages[1].text


# ------------------------------------------------------------------ container restarts


def info(started, restarts=1, oom=False, exit_code=0):
    return ContainerInfo("api", "corp/api:2", "running", restarts, oom, exit_code, started, "")


def test_restart_detection_and_oom_note():
    engine, cap = make()
    r0 = win(engine, 0, [], container=info("2026-11-18T10:00:00Z"))
    assert kinds(r0) == []  # first observation only remembers it
    r1 = win(engine, 1, [], container=info("2026-11-18T11:00:00Z", restarts=2, oom=True, exit_code=137))
    assert kinds(r1) == ["RESTART"] and r1.alerts[0].level == "FATAL"
    assert "running out of memory" in cap.messages[-1].text and "137" in cap.messages[-1].text


def test_restart_loop_is_reported_once_per_cooldown_with_a_total():
    engine, cap = make(WatchConfig(interval=60, min_history=300, restart_cooldown=300))
    win(engine, 0, [], container=info("2026-11-18T10:00:00Z"))
    assert kinds(win(engine, 1, [], container=info("2026-11-18T10:01:00Z"))) == ["RESTART"]
    assert kinds(win(engine, 2, [], container=info("2026-11-18T10:02:00Z"))) == []
    assert kinds(win(engine, 3, [], container=info("2026-11-18T10:03:00Z"))) == []
    for i in range(4, 6):
        win(engine, i, [], container=info("2026-11-18T10:03:00Z"))
    r = win(engine, 6, [], container=info("2026-11-18T10:03:00Z"))
    assert kinds(r) == ["RESTART"] and "restarted 2 times" in r.alerts[0].template


def test_unchanged_container_does_not_alert():
    engine, _ = make()
    for i in range(5):
        r = win(engine, i, [], container=info("2026-11-18T10:00:00Z"))
        assert kinds(r) == []


def test_new_pattern_right_after_a_restart_says_so():
    engine, cap = make()
    start = datetime.fromtimestamp(BASE).astimezone().isoformat()
    win(engine, 0, recs(GATEWAY, 2), container=info(datetime.fromtimestamp(BASE - 7200).astimezone().isoformat()))
    win(engine, 1, [], container=info(start))  # restart noticed
    r = win(engine, 2, recs(DISK, 3), container=info(start))
    assert "after the restart at" in r.alerts[0].note


# ------------------------------------------------------------------ diagnosis


def test_diagnosis_is_attached_and_rate_limited():
    calls = []

    def explain(clusters, stats, source):
        calls.append((len(clusters), source))
        return "The payment provider is down."

    cap = Capture()
    engine = Engine(Store(), [cap], WatchConfig(interval=60, min_history=300, explain_gap=300), explain=explain)
    win(engine, 0, recs(GATEWAY, 2))
    assert "🧠 The payment provider is down." in cap.messages[0].text
    win(engine, 1, recs(DISK, 2))  # inside the 5 minute gap: alert goes out without a model call
    assert len(calls) == 1 and "🧠" not in cap.messages[1].text
    win(engine, 7, recs(REDIS, 2))  # gap has passed
    assert len(calls) == 2


def test_a_failing_model_never_blocks_the_alert():
    def explain(*_):
        raise RuntimeError("model is down")

    cap = Capture()
    engine = Engine(Store(), [cap], WatchConfig(interval=60, min_history=300), explain=explain)
    r = win(engine, 0, recs(GATEWAY, 2))
    assert r.status == "sent" and len(cap.messages) == 1
    assert any("diagnosis unavailable" in n for n in r.notes)


# ------------------------------------------------------------------ persistence


def test_memory_survives_a_restart_of_the_watcher(tmp_path):
    path = tmp_path / "state.db"
    cap1 = Capture()
    e1 = Engine(Store(path), [cap1], WatchConfig(interval=60, min_history=300))
    win(e1, 0, recs(GATEWAY, 2))
    e1.store.close()

    cap2 = Capture()
    e2 = Engine(Store(path), [cap2], WatchConfig(interval=60, min_history=300))
    r = win(e2, 5, recs(GATEWAY, 2))  # same pattern after a restart: not new, not a startup
    assert kinds(r) == [] and cap2.messages == []
    r2 = win(e2, 6, recs(DISK, 2))
    assert kinds(r2) == ["NEW"]


def test_still_happening_respects_the_cooldown():
    # persistence is reached at window 2, but the cooldown (1 hour) has not passed yet
    engine, _ = make(WatchConfig(interval=60, min_history=300, persist_windows=3, cooldown=3600))
    assert kinds(win(engine, 0, recs(GATEWAY, 2))) == ["STARTUP"]
    for i in range(1, 60):
        assert kinds(win(engine, i, recs(GATEWAY, 2))) == [], f"alerted too early at window {i}"
    assert kinds(win(engine, 60, recs(GATEWAY, 2))) == ["STILL"]  # exactly one hour after the first alert


def test_old_baseline_data_is_pruned_automatically():
    cfg = WatchConfig(interval=60, min_history=300, lookback=3600)  # keeps 3 hours of windows
    engine, _ = make(cfg)
    for i in range(3):
        win(engine, i, recs(GATEWAY, 2))
    assert engine.store.observed_seconds("api", BASE + 100000, 200000) == 180
    day_later = BASE + 2 * 86400
    engine.process_window("api", [], day_later, day_later + 60)
    # the three old windows are gone; only the new one remains
    assert engine.store.observed_seconds("api", day_later + 100000, 400000) == 60
    assert len(engine.store.patterns()) == 1  # patterns and alert history are kept
