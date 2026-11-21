"""Strelizia's memory: which patterns exist, how often they occur, and what was alerted.

A small SQLite file. It stores only *templates* (patterns with the variable parts
masked), counts, and alert history. It never stores raw log lines.

Patterns are matched across windows by similarity rather than by exact text, because
Drain may word the same template slightly differently depending on how many lines
a window happened to contain.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .models import level_rank

SIMILARITY_ALLOWED_MISMATCH = 0.2  # templates of 5+ tokens may differ in up to 20% of tokens
_PLACEHOLDER = re.compile(r"<[^<>\s]*>")


def default_state_path() -> Path:
    home = os.environ.get("STRELIZIA_HOME")
    base = Path(home) if home else Path.home() / ".strelizia"
    return base / "state.db"


def _norm(token: str) -> str:
    return _PLACEHOLDER.sub("<>", token)


def _compatible(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    return na == nb or na == "<>" or nb == "<>"


def _merge(a: list[str], b: list[str]) -> list[str]:
    """Generalise two compatible token lists: positions that differ become <*>."""
    return [x if x == y else "<*>" for x, y in zip(a, b)]


def templates_match(a: list[str], b: list[str]) -> bool:
    if len(a) != len(b) or not a:
        return False
    allowed = int(len(a) * SIMILARITY_ALLOWED_MISMATCH) if len(a) >= 5 else 0
    mismatches = sum(1 for x, y in zip(a, b) if not _compatible(x, y))
    return mismatches <= allowed


@dataclass
class Pattern:
    id: int
    source: str
    level: str
    template: str
    first_seen: float
    last_seen: float
    total: int


@dataclass
class AlertRecord:
    id: int
    ts: float
    source: str
    pattern_id: Optional[int]
    kind: str
    level: str
    count: int
    rate: float
    title: str
    body: str
    status: str
    targets: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source TEXT PRIMARY KEY, first_seen REAL NOT NULL, restart_ts REAL
);
CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, level TEXT NOT NULL, template TEXT NOT NULL,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL, total INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_patterns_source ON patterns (source, level);
CREATE TABLE IF NOT EXISTS windows (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_windows_source ON windows (source, start);
CREATE TABLE IF NOT EXISTS counts (
    window_id INTEGER NOT NULL, pattern_id INTEGER NOT NULL, count INTEGER NOT NULL,
    PRIMARY KEY (window_id, pattern_id)
);
CREATE INDEX IF NOT EXISTS idx_counts_pattern ON counts (pattern_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL, pattern_id INTEGER,
    kind TEXT NOT NULL, level TEXT NOT NULL, count INTEGER NOT NULL, rate REAL NOT NULL DEFAULT 0,
    title TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, targets TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_alerts_pattern ON alerts (pattern_id, ts);
"""


class Store:
    def __init__(self, path: str | os.PathLike = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._tokens: dict[tuple[str, str], list[tuple[int, list[str]]]] = {}

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------------ sources

    def register_source(self, source: str, now: float) -> bool:
        """Remember a source. Returns True the first time it is ever seen."""
        cur = self._db.execute("INSERT OR IGNORE INTO sources (source, first_seen) VALUES (?, ?)", (source, now))
        self._db.commit()
        return cur.rowcount == 1

    def get_restart(self, source: str) -> Optional[float]:
        row = self._db.execute("SELECT restart_ts FROM sources WHERE source = ?", (source,)).fetchone()
        return row["restart_ts"] if row else None

    def set_restart(self, source: str, ts: float) -> None:
        self._db.execute("UPDATE sources SET restart_ts = ? WHERE source = ?", (ts, source))
        self._db.commit()

    # ------------------------------------------------------------------ patterns

    def _load_tokens(self, source: str, level: str) -> list[tuple[int, list[str]]]:
        key = (source, level)
        if key not in self._tokens:
            rows = self._db.execute("SELECT id, template FROM patterns WHERE source = ? AND level = ?", key).fetchall()
            self._tokens[key] = [(r["id"], r["template"].split()) for r in rows]
        return self._tokens[key]

    def match_or_create(self, source: str, level: str, template: str, now: float) -> tuple[Pattern, bool]:
        """Find the stored pattern this template belongs to, or create one. Returns (pattern, created)."""
        tokens = template.split()
        known = self._load_tokens(source, level)
        best: Optional[int] = None
        for index, (pid, stored) in enumerate(known):
            if stored == tokens:
                best = index
                break
            if best is None and templates_match(stored, tokens):
                best = index
        if best is not None:
            pid, stored = known[best]
            merged = _merge(stored, tokens)
            if merged != stored:  # the pattern got more general: remember that
                known[best] = (pid, merged)
                self._db.execute("UPDATE patterns SET template = ? WHERE id = ?", (" ".join(merged), pid))
                self._db.commit()
            return self.get_pattern(pid), False

        cur = self._db.execute(
            "INSERT INTO patterns (source, level, template, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
            (source, level, template, now, now),
        )
        self._db.commit()
        known.append((cur.lastrowid, tokens))
        return self.get_pattern(cur.lastrowid), True

    def get_pattern(self, pattern_id: int) -> Pattern:
        r = self._db.execute("SELECT * FROM patterns WHERE id = ?", (pattern_id,)).fetchone()
        return Pattern(r["id"], r["source"], r["level"], r["template"], r["first_seen"], r["last_seen"], r["total"])

    def patterns(self, source: Optional[str] = None) -> list[Pattern]:
        q, args = "SELECT * FROM patterns", ()
        if source:
            q, args = q + " WHERE source = ?", (source,)
        rows = self._db.execute(q + " ORDER BY total DESC", args).fetchall()
        return [Pattern(r["id"], r["source"], r["level"], r["template"], r["first_seen"], r["last_seen"], r["total"]) for r in rows]

    # ------------------------------------------------------------------ baseline

    def baseline(self, source: str, pattern_id: int, before: float, lookback: float) -> tuple[float, float]:
        """(occurrences, seconds observed) in the lookback period, ending before `before`."""
        since = before - lookback
        observed = self._db.execute(
            "SELECT COALESCE(SUM(end - start), 0) FROM windows WHERE source = ? AND start >= ? AND end <= ?",
            (source, since, before),
        ).fetchone()[0]
        total = self._db.execute(
            "SELECT COALESCE(SUM(c.count), 0) FROM counts c JOIN windows w ON w.id = c.window_id "
            "WHERE c.pattern_id = ? AND w.source = ? AND w.start >= ? AND w.end <= ?",
            (pattern_id, source, since, before),
        ).fetchone()[0]
        return float(total), float(observed)

    def observed_seconds(self, source: str, before: float, lookback: float) -> float:
        return float(
            self._db.execute(
                "SELECT COALESCE(SUM(end - start), 0) FROM windows WHERE source = ? AND start >= ? AND end <= ?",
                (source, before - lookback, before),
            ).fetchone()[0]
        )

    def record_window(self, source: str, start: float, end: float, counts: dict[int, int]) -> None:
        cur = self._db.execute("INSERT INTO windows (source, start, end) VALUES (?, ?, ?)", (source, start, end))
        wid = cur.lastrowid
        for pid, n in counts.items():
            self._db.execute("INSERT INTO counts (window_id, pattern_id, count) VALUES (?, ?, ?)", (wid, pid, n))
            self._db.execute("UPDATE patterns SET total = total + ?, last_seen = ? WHERE id = ?", (n, end, pid))
        self._db.commit()

    # ------------------------------------------------------------------ alerts

    def add_alert(self, *, ts: float, source: str, pattern_id: Optional[int], kind: str, level: str,
                  count: int, rate: float, title: str, body: str, status: str, targets: str = "") -> int:
        cur = self._db.execute(
            "INSERT INTO alerts (ts, source, pattern_id, kind, level, count, rate, title, body, status, targets) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, source, pattern_id, kind, level, count, rate, title, body, status, targets),
        )
        self._db.commit()
        return cur.lastrowid

    def last_alert(self, pattern_id: int, include_held: bool = False) -> Optional[AlertRecord]:
        """The most recent alert raised for a pattern. Held alerts were never delivered, so by
        default they do not count (they must not suppress a later alert); a held alert does
        still mean an incident was opened, which is what `include_held` is for."""
        skip = "('dropped')" if include_held else "('held', 'dropped')"
        r = self._db.execute(
            f"SELECT * FROM alerts WHERE pattern_id = ? AND status NOT IN {skip} ORDER BY ts DESC, id DESC LIMIT 1", (pattern_id,)
        ).fetchone()
        return _alert(r) if r else None

    def last_source_alert(self, source: str, kind: str) -> Optional[AlertRecord]:
        r = self._db.execute(
            "SELECT * FROM alerts WHERE source = ? AND kind = ? AND status NOT IN ('held', 'dropped') ORDER BY ts DESC LIMIT 1",
            (source, kind),
        ).fetchone()
        return _alert(r) if r else None

    def held_alerts(self) -> list[AlertRecord]:
        return [_alert(r) for r in self._db.execute("SELECT * FROM alerts WHERE status = 'held' ORDER BY ts").fetchall()]

    def set_status(self, alert_ids: list[int], status: str, targets: str = "") -> None:
        for aid in alert_ids:
            self._db.execute("UPDATE alerts SET status = ?, targets = ? WHERE id = ?", (status, targets, aid))
        self._db.commit()

    def recent_alerts(self, limit: int = 20) -> list[AlertRecord]:
        rows = self._db.execute("SELECT * FROM alerts WHERE status != 'silent' ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [_alert(r) for r in rows]

    def open_alerts(self, source: str) -> list[tuple[Pattern, AlertRecord]]:
        """Patterns whose latest alert (even one that is still held back) has not been resolved yet."""
        ids = self._db.execute(
            "SELECT p.id FROM patterns p WHERE p.source = ? AND ("
            " SELECT kind FROM alerts WHERE pattern_id = p.id AND status != 'dropped'"
            " ORDER BY ts DESC, id DESC LIMIT 1) IN ('NEW', 'SPIKE', 'STILL', 'STARTUP')",
            (source,),
        ).fetchall()
        return [(self.get_pattern(r["id"]), self.last_alert(r["id"], include_held=True)) for r in ids]

    def count_since(self, pattern_id: int, since: float) -> int:
        """Occurrences recorded in windows that ended after `since`."""
        return int(self._db.execute(
            "SELECT COALESCE(SUM(c.count), 0) FROM counts c JOIN windows w ON w.id = c.window_id "
            "WHERE c.pattern_id = ? AND w.end > ?", (pattern_id, since)).fetchone()[0])

    # ------------------------------------------------------------------ digest and meta

    def get_meta(self, key: str) -> Optional[str]:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self._db.commit()

    def total_observed(self, since: float, until: float) -> float:
        return float(self._db.execute("SELECT COALESCE(SUM(end - start), 0) FROM windows WHERE end > ? AND start < ?", (since, until)).fetchone()[0])

    def digest_rows(self, since: float, until: float) -> list[tuple[Pattern, int]]:
        """Warning/error patterns active in the period with their occurrence counts, busiest first."""
        rows = self._db.execute(
            "SELECT p.id, SUM(c.count) AS n FROM counts c JOIN windows w ON w.id = c.window_id "
            "JOIN patterns p ON p.id = c.pattern_id WHERE w.end > ? AND w.start < ? "
            "AND p.level IN ('WARN', 'ERROR', 'FATAL') GROUP BY p.id ORDER BY n DESC", (since, until)).fetchall()
        return [(self.get_pattern(r["id"]), int(r["n"])) for r in rows]

    def alert_counts(self, since: float, until: float) -> dict[tuple[str, str], int]:
        """Alerts actually raised in the period, by (source, kind)."""
        rows = self._db.execute(
            "SELECT source, kind, COUNT(*) AS n FROM alerts WHERE ts > ? AND ts <= ? "
            "AND status IN ('sent', 'console', 'preview') AND kind NOT IN ('DIGEST') GROUP BY source, kind", (since, until)).fetchall()
        return {(r["source"], r["kind"]): int(r["n"]) for r in rows}

    # ------------------------------------------------------------------ housekeeping

    def prune(self, before: float) -> None:
        """Forget baseline windows older than `before` (alerts and patterns are kept)."""
        self._db.execute("DELETE FROM counts WHERE window_id IN (SELECT id FROM windows WHERE end < ?)", (before,))
        self._db.execute("DELETE FROM windows WHERE end < ?", (before,))
        self._db.commit()


def _alert(r: sqlite3.Row) -> AlertRecord:
    return AlertRecord(r["id"], r["ts"], r["source"], r["pattern_id"], r["kind"], r["level"], r["count"],
                       r["rate"], r["title"], r["body"], r["status"], r["targets"])


__all__ = ["Store", "Pattern", "AlertRecord", "default_state_path", "templates_match", "level_rank"]
