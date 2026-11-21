"""The watch engine: turns windows of log records into alerts.

Each window (say 60 seconds of one source) goes through the same steps:
  1. group the records into patterns (Drain), keep only warnings and errors
  2. match each pattern against the memory in store.py (is it new? how common is it usually?)
  3. decide whether anything deserves an alert: NEW, SPIKE, STILL happening, RESTART
  4. apply cooldowns and quiet hours, redact, and deliver ONE message for the window

It is deliberately free of threads and sleeping: time is passed in, so every rule can be
tested precisely. See follow.py for the live part.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Callable, Optional

from .clusterer import Clusterer
from .digest import build_digest
from .models import Cluster, LogRecord, level_rank
from .notify import Message, Notifier, NotifyError
from .pipeline import Stats
from .redact import Redactor
from .sources.docker import ContainerInfo
from .store import AlertRecord, Store
from .util import in_quiet_hours, parse_timestamp

SPIKE_FLOOR = 0.5  # per minute: below this a pattern counts as "essentially never"
MAX_DELAYED = 20  # undelivered alerts we will still send as a digest; older ones are dropped

ExplainFn = Callable[[list[Cluster], Stats, str], Optional[str]]


@dataclass
class WatchConfig:
    interval: float = 60.0
    alert_level: str = "ERROR"  # patterns below this level never alert
    cooldown: float = 7200.0  # seconds between repeat alerts for the same pattern
    spike_factor: float = 5.0  # a spike is this many times the usual rate...
    min_count: int = 5  # ...and at least this many occurrences in the window
    min_history: float = 600.0  # seconds of watching needed before spikes can be judged
    lookback: float = 86400.0  # how far back "usual" is measured
    persist_windows: int = 15  # consecutive active windows before a STILL happening reminder
    restart_cooldown: float = 600.0
    quiet_hours: Optional[tuple[time, time]] = None
    startup_summary: bool = True
    include_samples: bool = False
    max_listed: int = 6  # patterns listed in one message
    explain_gap: float = 300.0  # seconds between model calls
    resolve_after: float = 900.0  # quiet (or calmed down) this long before announcing "resolved"; 0 disables
    digest_at: Optional[time] = None  # local time of the daily digest; None disables


@dataclass
class Alert:
    kind: str  # NEW | SPIKE | STILL | STARTUP | RESTART
    source: str
    level: str
    pattern_id: Optional[int]
    count: int
    rate: float  # per minute, in this window
    baseline: float  # per minute, usual
    template: str
    note: str = ""
    sample: str = ""
    cluster: Optional[Cluster] = field(default=None, repr=False)


@dataclass
class WindowResult:
    source: str
    records: int = 0
    patterns: int = 0
    alerts: list[Alert] = field(default_factory=list)
    message: Optional[Message] = None
    status: str = ""  # sent | console | preview | held
    delivered_to: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    delayed: Optional[Message] = None  # earlier undelivered alerts, sent this window
    digest: Optional[Message] = None  # the daily digest, if it was due this window


_LABEL = {
    "NEW": "🆕 New",
    "SPIKE": "📈 Spike",
    "STILL": "🔁 Still happening",
    "STARTUP": "👀 Already active",
    "RESTART": "♻️ Restarted",
    "RESOLVED": "✅ Resolved",
}
_NOUN = {
    "NEW": ("new pattern", "new patterns"),
    "SPIKE": ("spike", "spikes"),
    "STILL": ("ongoing problem", "ongoing problems"),
    "STARTUP": ("pattern already active", "patterns already active"),
    "RESTART": ("restart", "restarts"),
    "RESOLVED": ("resolved pattern", "resolved patterns"),
}


def _fmt_dur(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:g}s" if seconds < 10 else f"{round(seconds)}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _epoch(iso: str) -> Optional[float]:
    dt = parse_timestamp(iso) if iso else None
    return dt.timestamp() if dt else None


class Engine:
    def __init__(
        self,
        store: Store,
        notifiers: list[Notifier],
        cfg: WatchConfig,
        *,
        explain: Optional[ExplainFn] = None,
        send: bool = True,
    ) -> None:
        self.store, self.notifiers, self.cfg = store, notifiers, cfg
        self._explain, self._send = explain, send
        self._streak: dict[int, int] = {}
        self._pending_restarts: dict[str, int] = {}
        self._last_explain: Optional[float] = None
        self._last_prune: Optional[float] = None
        self._calm: dict[int, float] = {}  # pattern id -> when its spike started calming down

    # ------------------------------------------------------------------ main entry

    def process_window(
        self,
        source: str,
        records: list[LogRecord],
        start: float,
        end: float,
        container: Optional[ContainerInfo] = None,
    ) -> WindowResult:
        cfg, now = self.cfg, end
        res = WindowResult(source=source, records=len(records))
        if self._last_prune is None or now - self._last_prune >= 86400:
            self.store.prune(now - 3 * cfg.lookback)  # keep the memory file from growing forever
            self._last_prune = now
        first_run = self.store.register_source(source, now)
        quiet = self._in_quiet(now)
        if not quiet:
            self._flush_delayed(res)
        self._maybe_digest(res, now, quiet)

        alerts: list[Alert] = []
        if container is not None:
            self._check_restart(source, container, now, alerts)

        # 1. group into patterns (warnings and errors only)
        clusterer = Clusterer(max_samples=1)
        for record in records:
            if level_rank(record.level) >= level_rank("WARN"):
                clusterer.add(record)
        clusters = clusterer.clusters()
        res.patterns = len(clusters)

        # 2. fold them onto the patterns we remember
        folded: dict[int, dict] = {}
        for c in clusters:
            pattern, created = self.store.match_or_create(source, c.level, c.template, now)
            slot = folded.setdefault(pattern.id, {"pattern": pattern, "count": 0, "created": False, "cluster": c})
            slot["count"] += c.count
            slot["created"] = slot["created"] or created
            if c.count > slot["cluster"].count:
                slot["cluster"] = c

        # 3. decide what deserves an alert
        duration = max(end - start, 1e-6)
        history_ok = self.store.observed_seconds(source, start, cfg.lookback) >= cfg.min_history
        restart_ts = self.store.get_restart(source)
        for pid, slot in folded.items():
            pattern, count = slot["pattern"], slot["count"]
            self._streak[pid] = self._streak.get(pid, 0) + 1
            if level_rank(pattern.level) < level_rank(cfg.alert_level):
                continue
            rate = count / duration * 60
            total, observed = self.store.baseline(source, pid, start, cfg.lookback)
            usual = total / observed * 60 if observed > 0 else 0.0

            kind, note = None, ""
            if first_run:
                kind = "STARTUP" if cfg.startup_summary else None
            elif slot["created"]:
                kind = "NEW"
                if restart_ts is not None and 0 <= now - restart_ts <= 3600:
                    note = f"first seen {_fmt_dur(now - restart_ts)} after the restart at {_clock(restart_ts)}"
            else:
                last = self.store.last_alert(pid)
                if (
                    history_ok
                    and count >= cfg.min_count
                    and rate >= cfg.spike_factor * max(usual, SPIKE_FLOOR)
                    and self._escalation_ok(last, rate, now)
                ):
                    kind = "SPIKE"
                elif self._streak[pid] >= cfg.persist_windows and last is not None and now - last.ts >= cfg.cooldown:
                    kind = "STILL"
                    note = f"active in each of the last {self._streak[pid]} windows"
                    self._streak[pid] = 0
                elif cfg.resolve_after > 0 and last is not None and last.kind == "SPIKE":
                    calm_note = self._check_calm(pid, last, rate, start, now)
                    if calm_note:
                        kind, note = "RESOLVED", calm_note
            if kind == "RESOLVED":
                alerts.append(Alert(kind, source, pattern.level, pid, count, rate, usual, pattern.template, note))
            elif kind:
                sample = slot["cluster"].samples[0] if cfg.include_samples and slot["cluster"].samples else ""
                alerts.append(Alert(kind, source, pattern.level, pid, count, rate, usual, pattern.template,
                                    note, sample, slot["cluster"]))

        for pid in [p for p in self._streak if p not in folded]:
            del self._streak[pid]
        for pid in [p for p in self._calm if p not in folded]:
            del self._calm[pid]
        self.store.record_window(source, start, end, {pid: s["count"] for pid, s in folded.items()})
        if cfg.resolve_after > 0:
            self._check_gone(source, folded, now, alerts)

        # 4. deliver
        if alerts:
            res.alerts = alerts
            self._finish(res, alerts, now, quiet, len(records))
        return res

    # ------------------------------------------------------------------ rules

    def _escalation_ok(self, last: Optional[AlertRecord], rate: float, now: float) -> bool:
        """A repeat spike alert needs the cooldown to pass, or the rate to grow another spike_factor times."""
        if last is None or now - last.ts >= self.cfg.cooldown:
            return True
        return rate >= self.cfg.spike_factor * max(last.rate, SPIKE_FLOOR)

    def _in_quiet(self, now: float) -> bool:
        q = self.cfg.quiet_hours
        return bool(q) and in_quiet_hours(q, datetime.fromtimestamp(now))

    def _check_restart(self, source: str, c: ContainerInfo, now: float, alerts: list[Alert]) -> None:
        started = _epoch(c.started_at)
        if started is None:
            return
        previous = self.store.get_restart(source)
        if previous is None:
            self.store.set_restart(source, started)
            return
        if started > previous + 1:
            self.store.set_restart(source, started)
            self._pending_restarts[source] = self._pending_restarts.get(source, 0) + 1
        pending = self._pending_restarts.get(source, 0)
        if not pending:
            return
        last = self.store.last_source_alert(source, "RESTART")
        if last is not None and now - last.ts < self.cfg.restart_cooldown:
            return  # a crash loop: we will report the total once the cooldown passes
        self._pending_restarts[source] = 0
        facts = [f"{c.restart_count} restarts in total"]
        if c.oom_killed:
            facts.append("killed for running out of memory (OOM)")
        if c.exit_code not in (None, 0):
            facts.append(f"last exit code {c.exit_code}")
        what = "restarted" if pending == 1 else f"restarted {pending} times"
        alerts.append(Alert("RESTART", source, "FATAL" if c.oom_killed else "ERROR", None, pending, 0.0, 0.0,
                            f"container {what}, latest start at {_clock(started)}", ", ".join(facts)))

    def _check_calm(self, pid: int, last: AlertRecord, rate: float, start: float, now: float) -> Optional[str]:
        """A spike counts as resolved once its rate has stayed well below the alert for resolve_after."""
        if rate > last.rate / self.cfg.spike_factor:
            self._calm.pop(pid, None)
            return None
        began = self._calm.setdefault(pid, start)
        if now - began >= self.cfg.resolve_after:
            del self._calm[pid]
            return f"back down to {rate:.0f}/min (was {last.rate:.0f}/min at the alert)"
        return None

    def _check_gone(self, source: str, folded: dict, now: float, alerts: list[Alert]) -> None:
        """Announce patterns that raised an alert and then stopped appearing."""
        for pattern, last in self.store.open_alerts(source):
            if pattern.id in folded or now - pattern.last_seen < self.cfg.resolve_after:
                continue
            since = self.store.count_since(pattern.id, last.ts)
            sustained = last.kind in ("SPIKE", "STILL") or last.count + since >= self.cfg.min_count
            if sustained:
                note = f"quiet for {_fmt_dur(now - pattern.last_seen)}"
                alerts.append(Alert("RESOLVED", source, pattern.level, pattern.id, last.count + since, 0.0, 0.0, pattern.template, note))
            else:  # a one-off: close it quietly instead of sending a pair of messages about nothing
                self.store.add_alert(ts=now, source=source, pattern_id=pattern.id, kind="RESOLVED", level=pattern.level,
                                     count=0, rate=0.0, title=f"{source}: closed", body="", status="silent")

    def _maybe_digest(self, res: WindowResult, now: float, quiet: bool) -> None:
        at = self.cfg.digest_at
        if at is None:
            return
        day = datetime.fromtimestamp(now).date()
        if now < datetime.combine(day, at).timestamp() or self.store.get_meta("last_digest") == day.isoformat():
            return
        self.store.set_meta("last_digest", day.isoformat())
        if self.store.total_observed(now - 86400, now) < 3600:
            return  # under an hour of data: nothing useful to say yet, try again tomorrow
        message = build_digest(self.store, now, 24.0)
        if quiet:
            self.store.add_alert(ts=now, source="all", pattern_id=None, kind="DIGEST", level="INFO", count=0, rate=0.0,
                                 title=message.title, body=message.text, status="held")
            return
        status, delivered, errors = self._deliver(message)
        res.digest, res.errors = message, res.errors + errors
        self.store.add_alert(ts=now, source="all", pattern_id=None, kind="DIGEST", level="INFO", count=0, rate=0.0,
                             title=message.title, body=message.text,
                             status="held" if status == "failed" else status, targets=",".join(delivered))

    # ------------------------------------------------------------------ delivery

    def _finish(self, res: WindowResult, alerts: list[Alert], now: float, quiet: bool, n_records: int) -> None:
        if quiet:
            res.status = "held"
            for alert in alerts:
                self._store_alert(alert, now, "held", "")
            return
        diagnosis = self._diagnose(res, alerts, n_records, now)
        message = self._compose(res.source, alerts, diagnosis)
        res.message = message
        status, delivered, errors = self._deliver(message)
        res.status, res.delivered_to, res.errors = status, delivered, errors
        stored_status = "held" if status == "failed" else status  # undelivered: retry next window
        for alert in alerts:
            self._store_alert(alert, now, stored_status, ",".join(delivered))

    def _deliver(self, message: Message) -> tuple[str, list[str], list[str]]:
        if not self._send:
            return "preview", [], []
        if not self.notifiers:
            return "console", [], []
        delivered, errors = [], []
        for notifier in self.notifiers:
            try:
                notifier.send(message)
                delivered.append(notifier.label)
            except NotifyError as exc:
                errors.append(str(exc))
        return ("sent" if delivered else "failed"), delivered, errors

    def _flush_delayed(self, res: WindowResult) -> None:
        """Send alerts that were held back (quiet hours, or delivery that failed) as one digest."""
        held = self.store.held_alerts()
        if not held:
            return
        stale, held = held[:-MAX_DELAYED], held[-MAX_DELAYED:]
        self.store.set_status([a.id for a in stale], "dropped")
        text = "\n\n".join(f"{a.title}\n{a.body}" for a in held)
        message = Message(f"{len(held)} delayed alert{'s' if len(held) != 1 else ''}", text,
                          max((a.level for a in held), key=level_rank))
        status, delivered, errors = self._deliver(message)
        if status == "failed":
            res.errors += errors
            return
        self.store.set_status([a.id for a in held], status, ",".join(delivered))
        res.delayed = message

    def _diagnose(self, res: WindowResult, alerts: list[Alert], n_records: int, now: float) -> Optional[str]:
        clusters = [a.cluster for a in alerts if a.cluster is not None]
        if self._explain is None or not clusters:
            return None
        if self._last_explain is not None and now - self._last_explain < self.cfg.explain_gap:
            return None
        self._last_explain = now
        try:
            stats = Stats(lines=n_records, records=n_records, skipped=0, clusters=len(clusters))
            return self._explain(clusters, stats, res.source)
        except Exception as exc:  # a failing model must never stop alerts
            res.notes.append(f"diagnosis unavailable: {exc}")
            return None

    # ------------------------------------------------------------------ formatting

    def _render(self, alert: Alert, redactor: Redactor) -> list[str]:
        window = _fmt_dur(self.cfg.interval)
        head = f"{_LABEL[alert.kind]} · {alert.level}"
        if alert.kind in ("RESTART", "RESOLVED"):
            lines = [head, f"   {redactor.redact(alert.template)}"]
            if alert.note:
                lines.append(f"   {alert.note}")
            return lines
        head += f" · {alert.count:,}× in {window}"
        if alert.kind == "SPIKE":
            head += f" (usually about {alert.baseline:.1f}/min, now {alert.rate:.0f}/min)"
        lines = [head, f"   {redactor.redact(alert.template)}"]
        if alert.note:
            lines.append(f"   {alert.note}")
        if alert.sample:
            sample = redactor.redact("\n".join(alert.sample.splitlines()[:3]))[:300]
            lines += ["   e.g. " + sample.replace("\n", "\n        ")]
        return lines

    def _compose(self, source: str, alerts: list[Alert], diagnosis: Optional[str]) -> Message:
        redactor = Redactor()
        shown = alerts[: self.cfg.max_listed]
        lines: list[str] = []
        for alert in shown:
            lines += self._render(alert, redactor) + [""]
        if len(alerts) > len(shown):
            lines.append(f"… and {len(alerts) - len(shown)} more")
        if diagnosis:
            lines.append("🧠 " + redactor.redact(diagnosis))
        counts: dict[str, int] = {}
        for a in alerts:
            counts[a.kind] = counts.get(a.kind, 0) + 1
        parts = [f"{n} {_NOUN[k][0 if n == 1 else 1]}" for k, n in counts.items()]
        worst = max((a.level for a in alerts if a.kind != "RESOLVED"), key=level_rank, default="INFO")
        structured = [
            {"kind": a.kind, "level": a.level, "count": a.count, "rate_per_min": round(a.rate, 2),
             "usual_per_min": round(a.baseline, 2), "pattern": redactor.redact(a.template), "note": a.note}
            for a in alerts
        ]
        return Message(f"{source}: {', '.join(parts)}", "\n".join(lines).rstrip(), worst, structured)

    def _store_alert(self, alert: Alert, now: float, status: str, targets: str) -> None:
        redactor = Redactor()
        body = "\n".join(self._render(alert, redactor))
        self.store.add_alert(ts=now, source=alert.source, pattern_id=alert.pattern_id, kind=alert.kind,
                             level=alert.level, count=alert.count, rate=alert.rate,
                             title=f"{alert.source}: {_LABEL[alert.kind]}", body=body, status=status, targets=targets)
