import pytest

from strelizia.store import Store, templates_match


def toks(s):
    return s.split()


def test_same_template_matches_and_different_creates():
    s = Store()
    p1, created = s.match_or_create("api", "ERROR", "ERROR connect ECONNREFUSED <IP>", 100)
    assert created
    p2, created2 = s.match_or_create("api", "ERROR", "ERROR connect ECONNREFUSED <IP>", 200)
    assert not created2 and p2.id == p1.id
    p3, created3 = s.match_or_create("api", "ERROR", "ERROR disk full on <NUM>", 300)
    assert created3 and p3.id != p1.id


def test_wildcards_are_compatible_and_generalise():
    s = Store()
    p, _ = s.match_or_create("api", "ERROR", "ERROR job 42 failed", 1)
    q, created = s.match_or_create("api", "ERROR", "ERROR job <NUM> failed", 2)
    assert not created and q.id == p.id
    assert s.get_pattern(p.id).template == "ERROR job <*> failed"
    r, created = s.match_or_create("api", "ERROR", "ERROR job 7 failed", 3)  # now matches the wildcard
    assert not created and r.id == p.id


def test_one_differing_word_in_a_long_template_is_the_same_pattern():
    a = toks("ERROR could not send mail to user bob at relay")
    b = toks("ERROR could not send mail to user amy at relay")
    assert templates_match(a, b)
    c = toks("ERROR could not send mail to user amy at gateway2 now")
    assert not templates_match(a, c)  # different length
    d = toks("ERROR cannot reach cache for user amy at relay")
    assert not templates_match(a, d)  # many tokens differ


def test_short_templates_must_match_exactly():
    assert not templates_match(toks("ERROR cache miss"), toks("ERROR cache hit"))
    assert templates_match(toks("ERROR cache <*>"), toks("ERROR cache hit"))


def test_levels_and_sources_are_separate():
    s = Store()
    a, _ = s.match_or_create("api", "ERROR", "boom boom boom", 1)
    b, created_b = s.match_or_create("api", "WARN", "boom boom boom", 1)
    c, created_c = s.match_or_create("web", "ERROR", "boom boom boom", 1)
    assert created_b and created_c and len({a.id, b.id, c.id}) == 3


def test_baseline_rate_and_exclusion_of_the_current_window():
    s = Store()
    p, _ = s.match_or_create("api", "ERROR", "x y z", 0)
    for i in range(10):  # ten 60s windows with 6 occurrences each
        s.record_window("api", i * 60, (i + 1) * 60, {p.id: 6})
    total, observed = s.baseline("api", p.id, before=600, lookback=3600)
    assert (total, observed) == (60, 600)
    # a window that ends after `before` is not part of the baseline
    total, observed = s.baseline("api", p.id, before=300, lookback=3600)
    assert (total, observed) == (30, 300)
    # the lookback limits how far back we look
    total, observed = s.baseline("api", p.id, before=600, lookback=120)
    assert (total, observed) == (12, 120)
    assert s.observed_seconds("api", 600, 3600) == 600
    assert s.get_pattern(p.id).total == 60


def test_sources_are_remembered_and_first_run_is_detected():
    s = Store()
    assert s.register_source("api", 1.0) is True
    assert s.register_source("api", 2.0) is False
    assert s.get_restart("api") is None
    s.set_restart("api", 55.0)
    assert s.get_restart("api") == 55.0


def test_alert_history_and_held_alerts():
    s = Store()
    p, _ = s.match_or_create("api", "ERROR", "a b c", 0)
    held = s.add_alert(ts=10, source="api", pattern_id=p.id, kind="NEW", level="ERROR", count=3, rate=3.0,
                       title="t", body="b", status="held")
    assert s.last_alert(p.id) is None  # held alerts were never raised
    assert [a.id for a in s.held_alerts()] == [held]
    s.set_status([held], "sent", "discord")
    last = s.last_alert(p.id)
    assert last.kind == "NEW" and last.status == "sent" and last.targets == "discord"
    assert s.held_alerts() == []
    assert s.recent_alerts(5)[0].id == held


def test_state_survives_reopening(tmp_path):
    path = tmp_path / "nested" / "state.db"
    s = Store(path)
    p, _ = s.match_or_create("api", "ERROR", "a b c", 5)
    s.record_window("api", 0, 60, {p.id: 4})
    s.close()
    s2 = Store(path)
    q, created = s2.match_or_create("api", "ERROR", "a b c", 99)
    assert not created and q.id == p.id and q.total == 4
    assert s2.register_source("api", 1) is True  # sources are only added by register_source


def test_prune_removes_old_windows_but_keeps_patterns_and_alerts():
    s = Store()
    p, _ = s.match_or_create("api", "ERROR", "a b c", 0)
    s.record_window("api", 0, 60, {p.id: 4})
    s.record_window("api", 1000, 1060, {p.id: 4})
    s.add_alert(ts=1, source="api", pattern_id=p.id, kind="NEW", level="ERROR", count=1, rate=1, title="t", body="b", status="sent")
    s.prune(before=500)
    assert s.observed_seconds("api", 5000, 10000) == 60
    assert len(s.patterns()) == 1 and len(s.recent_alerts()) == 1
