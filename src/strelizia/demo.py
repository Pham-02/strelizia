"""Generate realistic broken logs so you can try Strelizia without a broken server."""

from __future__ import annotations

import json
import random
import threading
from datetime import datetime, timedelta
from typing import Callable, Iterator, Optional

_TS = "%Y-%m-%d %H:%M:%S"


def _db_outage(rng: random.Random, start: datetime) -> list[str]:
    """An app whose database goes down partway through."""
    lines: list[str] = []
    t = start
    outage_at = 250
    for i in range(1500):
        t += timedelta(milliseconds=rng.randint(100, 900))
        ts = t.strftime(_TS)
        if i < outage_at:
            lines.append(f"{ts} INFO request GET /api/users/{rng.randint(1, 9999)} 200 {rng.randint(5, 90)}ms")
            continue
        roll = rng.random()
        if roll < 0.70:
            lines.append(f"{ts} ERROR connect ECONNREFUSED 10.0.0.5:5432")
        elif roll < 0.80:
            lines.append(f"{ts} WARN retrying database connection in {rng.choice([1, 2, 4])}s (attempt {rng.randint(1, 12)})")
        elif roll < 0.90:
            lines.append(f"{ts} ERROR request failed")
            lines.append("Traceback (most recent call last):")
            lines.append('  File "/app/db.py", line 41, in get_conn')
            lines.append("    return psycopg2.connect(DSN)")
            lines.append('  File "/usr/lib/python3/psycopg2/__init__.py", line 122, in connect')
            lines.append("    conn = _connect(dsn, connection_factory=connection_factory, **kwasync)")
            lines.append('psycopg2.OperationalError: connection to server at "10.0.0.5", port 5432 failed: Connection refused')
        elif roll < 0.92:
            lines.append(f"{ts} WARN slow query took {rng.randint(1500, 4000)}ms: SELECT * FROM sessions WHERE user_id = {rng.randint(1, 999)}")
        else:
            lines.append(f"{ts} INFO request GET /health 200 {rng.randint(1, 5)}ms")
    return lines


def _nginx_502(rng: random.Random, start: datetime) -> list[str]:
    """nginx in front of an upstream that keeps dying."""
    lines: list[str] = []
    t = start
    paths = ["/api/orders/{}", "/api/cart/{}", "/api/users/{}", "/checkout/{}"]
    for i in range(1200):
        t += timedelta(milliseconds=rng.randint(50, 600))
        client = f"203.0.113.{rng.randint(2, 250)}"
        path = rng.choice(paths).format(rng.randint(1, 5000))
        access_ts = t.strftime("%d/%b/%Y:%H:%M:%S +0000")
        roll = rng.random()
        if roll < 0.35:
            lines.append(
                f"{t.strftime('%Y/%m/%d %H:%M:%S')} [error] {rng.randint(1000, 9999)}#{rng.randint(1000, 9999)}: "
                f"*{rng.randint(10000, 99999)} connect() failed (111: Connection refused) while connecting to upstream, "
                f'client: {client}, server: example.com, request: "GET {path} HTTP/1.1", '
                f'upstream: "http://127.0.0.1:8080{path}", host: "example.com"'
            )
            lines.append(f'{client} - - [{access_ts}] "GET {path} HTTP/1.1" 502 157 "-" "Mozilla/5.0"')
        elif roll < 0.40:
            lines.append(f'{client} - - [{access_ts}] "GET /static/old-logo.png HTTP/1.1" 404 146 "-" "Mozilla/5.0"')
        else:
            lines.append(f'{client} - - [{access_ts}] "GET {path} HTTP/1.1" 200 {rng.randint(300, 9000)} "-" "Mozilla/5.0"')
    return lines


def _bad_deploy(rng: random.Random, start: datetime) -> list[str]:
    """A Java service that starts throwing NullPointerExceptions right after a deploy."""
    lines: list[str] = []
    t = start
    for i in range(1000):
        t += timedelta(milliseconds=rng.randint(100, 800))
        ts = t.strftime(_TS) + f".{rng.randint(0, 999):03d}"
        if i < 300:
            lines.append(f"{ts} INFO  [http-nio-8080-exec-{rng.randint(1, 9)}] c.a.UserController - GET /users/{rng.randint(1, 9999)} 200")
            continue
        if i == 300:
            lines.append(f"{ts} INFO  [main] c.a.Application - Starting application version 2.4.1")
            continue
        roll = rng.random()
        if roll < 0.55:
            lines.append(f"{ts} ERROR [http-nio-8080-exec-{rng.randint(1, 9)}] c.a.UserController - Unhandled exception")
            lines.append('java.lang.NullPointerException: Cannot invoke "String.length()" because "name" is null')
            lines.append("\tat com.app.UserService.validate(UserService.java:88)")
            lines.append("\tat com.app.UserController.create(UserController.java:41)")
            lines.append("\tat org.springframework.web.servlet.FrameworkServlet.service(FrameworkServlet.java:883)")
        elif roll < 0.62:
            lines.append(f"{ts} ERROR [scheduler-{rng.randint(1, 3)}] c.a.SyncJob - Job failed")
            lines.append("java.lang.IllegalStateException: Cache not initialised")
            lines.append("\tat com.app.Cache.get(Cache.java:27)")
            lines.append("\tat com.app.SyncJob.run(SyncJob.java:60)")
        elif roll < 0.68:
            lines.append(f"{ts} WARN  [http-nio-8080-exec-{rng.randint(1, 9)}] c.a.LegacyFilter - Deprecated API /v1/users called by client {rng.randint(100, 999)}")
        else:
            lines.append(f"{ts} INFO  [http-nio-8080-exec-{rng.randint(1, 9)}] c.a.UserController - GET /users/{rng.randint(1, 9999)} 200")
    return lines


def _json_api(rng: random.Random, start: datetime) -> list[str]:
    """A Node-style JSON-logging API whose payment provider keeps timing out."""
    lines: list[str] = []
    t = start
    for i in range(900):
        t += timedelta(milliseconds=rng.randint(50, 700))
        iso = t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"
        roll = rng.random()
        if i < 200 or roll > 0.5:
            rec = {"level": "info", "time": iso, "msg": "request completed", "method": "GET",
                   "path": f"/api/users/{rng.randint(1, 9999)}", "status": 200, "duration_ms": rng.randint(4, 120)}
        elif roll < 0.36:
            rec = {"level": "error", "time": iso, "msg": "payment provider timeout", "service": "billing",
                   "provider": "stripe", "error": f"ETIMEDOUT 10.0.0.9:443 after {rng.randint(2000, 9000)}ms",
                   "order_id": rng.randint(1000, 9999)}
        elif roll < 0.42:
            rec = {"level": "warn", "time": iso, "msg": "retrying request", "attempt": rng.randint(1, 5)}
        elif roll < 0.45:
            rec = {"level": "error", "time": iso, "msg": "unhandled exception",
                   "error": "TypeError: Cannot read properties of undefined (reading 'id')",
                   "stack": "TypeError: Cannot read properties of undefined (reading 'id')\n"
                            "    at handler (/app/src/orders.js:88:21)\n    at Layer.handle (/app/node_modules/express/lib/router/layer.js:95:5)"}
        else:
            rec = {"level": "info", "time": iso, "msg": "cache hit", "key": f"user:{rng.randint(1, 500)}"}
        lines.append(json.dumps(rec))
    return lines


SCENARIOS: dict[str, Callable[[random.Random, datetime], list[str]]] = {
    "json-api": _json_api,
    "db-outage": _db_outage,
    "nginx-502": _nginx_502,
    "bad-deploy": _bad_deploy,
}


def generate(scenario: str, seed: int = 7, now: Optional[datetime] = None) -> list[str]:
    """Return the raw log lines for a scenario. Same seed, same logs."""
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown scenario {scenario!r}. Choose from: {', '.join(SCENARIOS)}")
    rng = random.Random(seed)
    start = (now or datetime.now()).replace(microsecond=0) - timedelta(minutes=15)
    return SCENARIOS[scenario](rng, start)


# ---------------------------------------------------------------------- live stream for `watch --demo`

STREAMS = ("spike",)


def _spike_window(rng: random.Random, burst: bool) -> list[str]:
    """One window of a payment service: quiet normally, then a burst plus a brand-new error."""
    now = datetime.now().strftime(_TS)
    lines = [f"{now} INFO request GET /api/orders/{rng.randint(1, 9999)} 200 {rng.randint(5, 90)}ms" for _ in range(18 if not burst else 20)]
    errors = 90 if burst else 2
    lines += [f"{now} ERROR payment gateway timeout after {rng.randint(2000, 9000)}ms (order {rng.randint(1000, 9999)})" for _ in range(errors)]
    if burst:
        lines += [f"{now} ERROR disk quota exceeded for tenant {rng.randint(1, 40)}" for _ in range(6)]
    rng.shuffle(lines)
    return lines


def stream_lines(scenario: str, interval: float, stop: threading.Event) -> Iterator[str]:
    """Yield log lines paced in real time: 8 quiet windows, 2 burst windows, then quiet again."""
    if scenario not in STREAMS:
        raise ValueError(f"Unknown demo {scenario!r}. Choose from: {', '.join(STREAMS)}")
    rng = random.Random(7)
    for burst in [False] * 8 + [True] * 2 + [False] * 3:
        lines = _spike_window(rng, burst)
        gap = interval / max(len(lines), 1)
        for line in lines:
            if stop.is_set():
                return
            yield line
            stop.wait(gap)
    while not stop.is_set():  # keep the service quietly alive until the user stops the demo
        yield f"{datetime.now().strftime(_TS)} INFO request GET /health 200 2ms"
        stop.wait(interval)
