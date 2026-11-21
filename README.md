<p align="center">
  <img src="docs/logo.png" alt="Strelizia logo" width="200">
</p>

<h1 align="center">Strelizia</h1>

<p align="center">
  <b>Turn 40,000 repetitive log lines into the 3 problems that actually matter.</b>
</p>

<p align="center">
  A lightweight, local-first log analyzer that groups noisy server logs into root causes,<br>
  so you can see what is really going wrong in seconds.
</p>

<p align="center">
  <a href="https://github.com/pham-02/strelizia/actions/workflows/ci.yml"><img src="https://github.com/pham-02/strelizia/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/status-v0.4%20early%20development-F97316" alt="Status: v0.4 early development">
  <img src="https://img.shields.io/badge/python-3.10%2B-5B3A9E" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-MIT-F59E0B" alt="License: MIT">
</p>

<p align="center">
  <a href="#see-it-work">See it work</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#features">Features</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#roadmap">Roadmap</a>
</p>

<!-- Add your demo GIF here once you have recorded one:
<p align="center">
  <img src="docs/demo.gif" alt="Strelizia demo" width="800">
</p>
-->

---

## What is Strelizia?

Anyone who runs a server knows the feeling: something is broken, and the answer is buried somewhere in 40,000 lines of `docker logs` or `/var/log/nginx/error.log`, most of which are the same error repeated again and again.

Big observability platforms (Datadog, Elastic, Grafana Loki) solve this, but they are heavy, expensive, or complicated to set up for a solo developer, a small team, or a homelab.

**Strelizia is the lightweight alternative.** Point it at a Docker container or a log file and it:

1. **Assembles** multi-line entries, so a 30-line stack trace counts as one event
2. **Groups** thousands of similar lines into a handful of distinct patterns
3. **Ranks** them by severity and frequency, so the real problem is at the top
4. **Explains** the top patterns in plain English, if you ask it to (`--explain`), using a local model or a cloud one, with secrets masked first
5. **Watches** live logs, learns what is normal, and **alerts** you on Discord, Slack, Telegram, ntfy, or any webhook only when something is **new**, **spiking**, or **restarting**, instead of pinging you for every error

It understands **JSON logs, nginx, syslog, and the systemd journal** out of the box and detects the format automatically, so problems group by what actually went wrong (`GET /api/orders returned HTTP 502`) instead of by who happened to hit them.

See the [roadmap](#roadmap) for what is next.

> Grouping is local and deterministic. Diagnosis is opt-in: the model only ever sees the few top patterns, redacted, never your whole log. That keeps it fast, cheap, private, and more trustworthy. And `--dry-run` shows you exactly what would be sent before anything is.

---

## See it work

This is real output from `strelizia demo --scenario db-outage`, a generated log of a database going down:

```text
╭──────────────────────────────────────────────────────────────────────────────────────────╮
│ Strelizia  ·  demo: db-outage                                                            │
│ 2,214 lines → 1,500 records → 4 patterns  (350 below the level filter were ignored)      │
╰──────────────────────────────────────────────────────────────────────────────────────────╯
╭─ 🔴 #1  ERROR  ·  882× ──────────────────────────────────────────────────────────────────╮
│ Pattern  ERROR connect ECONNREFUSED <IP>                                                 │
│ Seen     2026-11-18 09:57:33  →  2026-11-18 10:07:58                                     │
│                                                                                          │
│ Sample                                                                                   │
│ ERROR connect ECONNREFUSED 10.0.0.5:5432                                                 │
╰──────────────────────────────────────────────────────────────────────────────────────────╯
╭─ 🔴 #2  ERROR  ·  119× ──────────────────────────────────────────────────────────────────╮
│ Pattern  ERROR request failed | psycopg2.OperationalError: connection to server at       │
│ "<IP>", port <NUM> failed: Connection refused @ connect                                  │
│ Seen     2026-11-18 09:57:55  →  2026-11-18 10:07:43                                     │
│                                                                                          │
│ Sample                                                                                   │
│ ERROR request failed                                                                     │
│ Traceback (most recent call last):                                                       │
│   File "/app/db.py", line 41, in get_conn                                                │
│     return psycopg2.connect(DSN)                                                         │
│   File "/usr/lib/python3/psycopg2/__init__.py", line 122, in connect                     │
│     conn = _connect(dsn, connection_factory=connection_factory, **kwasync)               │
│ psycopg2.OperationalError: connection to server at "10.0.0.5", port 5432 failed:         │
│ Connection refused                                                                       │
╰──────────────────────────────────────────────────────────────────────────────────────────╯
… and 2 more patterns. Use --top to show more.
```

2,214 raw lines became 4 patterns. The 7-line traceback was counted as one event, and the 882 repeated connection errors became a single pattern, with IPs and ports masked so they group together.

---

## Quick start

You need **Python 3.10 or newer**.

```bash
git clone https://github.com/pham-02/strelizia.git
cd strelizia

python -m venv .venv
# Windows:        .venv\Scripts\activate
# Mac and Linux:  source .venv/bin/activate

pip install -e .
strelizia demo
```

That last command analyzes generated broken logs, so you can try Strelizia without a broken server. Then point it at something real:

```bash
# A running Docker container
strelizia analyze --docker my-api --since 1h

# A log file
strelizia analyze /var/log/nginx/error.log

# The systemd journal (Linux), optionally for one unit
strelizia analyze --journald --unit nginx --since 2h

# Anything piped in
docker logs my-api 2>&1 | strelizia analyze -
```

The log format (JSON lines, nginx access and error logs, syslog, journald, or plain text) is detected automatically, line by line. Force one with `--format json|nginx|syslog|plain` if you ever need to.

> `--docker` talks to the same Docker daemon that `docker logs` uses, so it works wherever that command works.

### Add a plain-English diagnosis

```bash
# 1. Preview exactly what would be sent. Nothing is sent, and no model is needed.
strelizia demo --dry-run

# 2. A local model with Ollama: nothing leaves your machine at all
ollama pull llama3.1
strelizia analyze --docker my-api --explain --model llama3.1

# 3. Or a cloud model. The key comes from an environment variable, never a flag.
export ANTHROPIC_API_KEY=...        # PowerShell:  $env:ANTHROPIC_API_KEY = "..."
strelizia analyze --docker my-api --explain --provider anthropic --model <model-name>
```

| Provider | Use it for | Key from |
|---|---|---|
| `ollama` (default) | Models running on your machine | none (`OLLAMA_HOST` if not on localhost) |
| `openai` | OpenAI, and any OpenAI-compatible server (LM Studio, vLLM, OpenRouter, Groq...) via `--base-url` | `OPENAI_API_KEY` |
| `anthropic` | Claude models | `ANTHROPIC_API_KEY` |

You can also set `STRELIZIA_MODEL`, `STRELIZIA_BASE_URL`, and `STRELIZIA_API_KEY`. Bigger models give better diagnoses, and small local models sometimes struggle to return valid JSON (Strelizia retries once, then tells you).

> The three providers are tested against local mock servers that follow each API's request and response format. Reports from real-world use are very welcome.

### Watch live logs and get alerts

```bash
# See watch mode with no server at all: a simulated payment service that is quiet,
# then bursts with errors, then recovers (about 15 seconds)
strelizia watch --demo spike --interval 1s

# Follow containers and send alerts to Discord. Put the URL in an environment variable
# so it never lands in your shell history.
export STRELIZIA_NOTIFY="https://discord.com/api/webhooks/..."    # PowerShell: $env:STRELIZIA_NOTIFY = "..."
strelizia watch --docker my-api --docker my-worker

# Follow a log file, hold alerts overnight, and add a model's diagnosis to each alert
strelizia watch /var/log/nginx/error.log --quiet-hours 23:00-07:00 --explain --model llama3.1

# Preview alerts on screen without sending them anywhere
strelizia watch --docker my-api --notify file://alerts.jsonl --dry-run

# Follow the systemd journal, send a summary every morning at 09:00
strelizia watch --journald --unit nginx --digest-at 09:00

# Review what it has alerted on, or print the digest right now
strelizia history
strelizia digest
```

### Run it in Docker Compose

A ready-made `docker-compose.yml` runs Strelizia next to your containers. It reaches Docker through a read-only proxy (`tecnativa/docker-socket-proxy`) instead of mounting the Docker socket into Strelizia, because a mounted socket is root-equivalent access to the host even when mounted read-only.

```bash
echo 'STRELIZIA_NOTIFY=https://discord.com/api/webhooks/...' > .env     # never commit this file
# edit docker-compose.yml: replace "api" and "worker" with your container names
docker compose up -d --build
```

> The Dockerfile and Compose file are built and validated by this project's CI on every push (see the badge), but they have not been tested against a real Docker host by the author. Reports are welcome.

Where alerts can go (pass `--notify URL`, repeatable, or set `STRELIZIA_NOTIFY` to a comma-separated list):

| Destination | URL |
|---|---|
| Discord | `https://discord.com/api/webhooks/...` |
| Slack | `https://hooks.slack.com/services/...` |
| Telegram | `telegram://BOT_TOKEN/CHAT_ID` |
| ntfy | `ntfy://my-topic` (or `ntfy://host/topic` for your own server) |
| Any webhook | any other `https://...` URL receives a JSON `POST` |
| A file | `file://alerts.jsonl` appends one JSON line per alert |

> Notification URLs contain secrets, so Strelizia never prints, logs, or stores them. Errors and the alert history only name the *kind* of destination. The channels are tested against local mock servers that follow each service's documented request format, so reports from real-world use are very welcome.

### Usage

**`strelizia analyze [FILE]`**: group a log into its distinct problems

| Option | Meaning |
|---|---|
| `FILE` | Log file to read, or `-` for stdin |
| `-d`, `--docker NAME` | Read from a Docker container instead of a file |
| `--journald` | Read the systemd journal instead (Linux; you may need `sudo` or the `systemd-journal` group) |
| `-u`, `--unit NAME` | With `--journald`: only this systemd unit, e.g. `nginx` |
| `-s`, `--since 1h` | Docker and journald: how far back to read (`30m`, `1h`, `2d`, `1h30m`) |
| `--format auto` | `auto`, `plain`, `json`, `nginx`, or `syslog` |
| `-n`, `--top 10` | How many patterns to show |
| `-l`, `--min-level warn` | Ignore records below this level (`debug`, `info`, `warn`, `error`, `fatal`) |
| `--samples 2` | Sample log entries to show per pattern |
| `--json` | Machine-readable JSON instead of the terminal report |
| `--explain` | Ask a model to diagnose the top patterns |
| `--dry-run` | Show exactly what `--explain` would send, without sending anything |
| `--provider`, `--model`, `--base-url` | Which model to use (see above) |
| `--explain-top 3` | How many top patterns to send for diagnosis (1 to 8) |

**`strelizia demo`**: try it with generated logs. Takes the same `--explain`, `--dry-run`, `--provider`, `--model`, `--base-url`, `--explain-top`, `--top` and `--json` options.

| Option | Meaning |
|---|---|
| `--scenario NAME` | `db-outage` (default), `nginx-502`, or `bad-deploy` |
| `--raw` | Print the generated raw logs instead of analyzing them |
| `--seed N` | Same seed, same logs |

**`strelizia watch [FILES...]`**: follow live logs and alert on what is new or unusual

| Option | Meaning |
|---|---|
| `FILES` | Log file(s) to follow (survives rotation and truncation) |
| `-d`, `--docker NAME` | Container to follow, repeatable (reconnects after restarts) |
| `--journald`, `-u NAME` | Follow the systemd journal, optionally for one unit |
| `--format auto` | `auto`, `plain`, `json`, `nginx`, or `syslog` |
| `--demo spike` | A simulated live log, no server needed |
| `--interval 60s` | Length of each window |
| `--notify URL` | Where to send alerts, repeatable (or `STRELIZIA_NOTIFY`) |
| `--alert-level error` | Lowest level that can raise an alert |
| `--cooldown 2h` | Minimum time between repeat alerts for one pattern |
| `--spike-factor 5` | A spike is this many times the usual rate |
| `--min-count 5` | Occurrences needed in a window to count as a spike |
| `--min-history 10m` | How long to watch before judging spikes |
| `--quiet-hours 23:00-07:00` | Hold alerts during these hours, send one digest afterwards |
| `--resolve-after 15m` | Announce "resolved" after a problem has been gone this long (`off` to disable) |
| `--digest-at 09:00` | Send a daily summary at this local time |
| `--from-start` | Files: read existing content too, not just new lines |
| `--alert-samples` | Include one redacted sample line in alerts |
| `--explain` | Add a model's diagnosis to alerts (with `--provider`, `--model`, `--base-url`) |
| `--dry-run` | Show alerts on screen, send them nowhere |
| `--quiet` | Only print when something happens |
| `--duration 5m` | Stop after this long |
| `--state PATH` | Where Strelizia keeps its memory (default `~/.strelizia/state.db`) |

**`strelizia history`**: show recent alerts (`-n` for how many, `--state` to pick the memory file)

**`strelizia digest`**: print a summary of the last day (`--hours` to change it, `--notify URL` to also send it)

---

## Features

| Feature | Status |
|---|---|
| Multi-line assembly (Python and Java stack traces) | ✅ Available |
| Pattern grouping with Drain3 | ✅ Available |
| Docker containers, log files, and stdin | ✅ Available |
| Severity detection and ranking | ✅ Available |
| Terminal report and `--json` output | ✅ Available |
| Demo log generator | ✅ Available |
| Secret redaction and `--dry-run` | ✅ Available |
| Plain-English diagnosis (Ollama, OpenAI-compatible, Anthropic) | ✅ Available |
| Watch mode for Docker containers and files | ✅ Available |
| "What changed" detection: new patterns, spikes, restarts | ✅ Available |
| Alerts to Discord, Slack, Telegram, ntfy, webhooks, files | ✅ Available |
| Format adapters: JSON, nginx, syslog, journald | ✅ Available |
| "Resolved" notices when a problem ends | ✅ Available |
| Daily digest | ✅ Available |
| Docker Compose setup, reproducible benchmarks | ✅ Available |

### 🧩 Smart log grouping

Uses log template mining (the Drain algorithm via [`drain3`](https://github.com/logpai/Drain3)) to collapse thousands of near-identical lines into a few patterns. Variable parts like IPs, ids, numbers, and timestamps are masked, while meaningful differences are kept: different HTTP statuses (a `502` and a `504`, let alone a `404`) are never merged, and each severity level is clustered separately.

### 🧬 Understands your log format

An nginx access line is mostly noise: client IP, browser, byte counts. Format adapters know the fields, so they read the real severity (an HTTP 502 is an error, syslog priority 3 is an error), the real timestamp, and build a clean clustering key. A thousand 502s from a thousand visitors become one pattern, `nginx GET /api/orders/<NUM> returned HTTP 502`. JSON logs group by message and error, with the top stack frame included, and the systemd journal is read through its JSON output. Detection is automatic and per line, so a container that writes both access and error logs just works.

### 📚 Multi-line awareness

One failure is often 10 to 50 lines. Strelizia glues stack-trace lines back together and builds each pattern from what identifies the problem: the exception and where it happened. Thousands of traces from one bug become one pattern, while different bugs stay separate.

### 🔍 Alerts that mean something

Most alerting is noise: every error pings you. Watch mode remembers every pattern it has seen (in a small local SQLite file) and how often it usually happens over the last 24 hours, then raises an alert only for:

| Alert | When |
|---|---|
| 🆕 **New** | A pattern it has never seen before (labelled "first seen 3m after the restart at 14:02" when it follows a container restart) |
| 📈 **Spike** | At least 5× the usual rate and at least 5 occurrences in the window, after 10 minutes of watching. A sustained spike alerts once, and again only if it gets another 5× worse |
| 🔁 **Still happening** | A pattern that has been active for 15 windows in a row, as a reminder once the cooldown has passed |
| ✅ **Resolved** | A pattern that raised an alert has not appeared for 15 minutes, or a spike has been back below a fifth of its alert rate (with the default `--spike-factor 5`) for that long. A problem that only happened a handful of times (fewer than 5) is closed silently instead of sending a pair of messages about nothing |
| ♻️ **Restarted** | A watched container restarted, with the restart count and whether it was killed for running out of memory. A crash loop is summarised once per 10 minutes instead of one ping per restart |
| 👀 **Already active** | One summary when you first start watching a source that is already broken |

All of these respect `--alert-level` and a per-pattern cooldown (2 hours by default). Everything found in one window goes out as **one message**, not one per pattern. Alerts held back by `--quiet-hours` (or by a failed delivery) arrive afterwards as a single message.

With `--digest-at 09:00` you also get a daily summary: what happened in the last 24 hours per source, the busiest patterns, and how many alerts were raised. It is sent even on a quiet day, so it doubles as proof the watcher is alive. (`strelizia digest` prints it on demand.)

Patterns are matched across windows by similarity, not exact text, so a pattern worded slightly differently from one window to the next is not mistaken for a new one.

### 🔒 Redaction built in

Before anything is sent to a model or an alert channel, Strelizia masks values with stable placeholders (`<IP_1>`, `<EMAIL_1>`, `<TOKEN_2>`), so you can still see that "the same host failed 400 times" without exposing the host. It handles IPv4 and IPv6 addresses, emails, JWTs and common API key formats (AWS, GitHub, Slack, Stripe, Google, OpenAI-style), bearer tokens, `password=` style values, credentials inside URLs, private key blocks, usernames in home-directory paths, and long random-looking tokens.

### 🧠 Plain-English diagnosis

With `--explain`, the top patterns go to a model along with useful context: counts, first and last seen times, a few samples, the records just before each pattern first appeared, and for Docker containers facts like the restart count and whether it was killed for running out of memory. The model must answer in a fixed structure: an overall diagnosis (patterns are often symptoms of one cause), and for each pattern a summary, a likely cause (a hypothesis, not a fact), a confidence level, evidence from the logs, and read-only commands to verify it.

In watch mode, the diagnosis is added to alerts (at most once every 5 minutes, and a failing model never blocks an alert). Strelizia **never executes anything**, and it hides suggested commands that could change things (restarts, deletes, installs, redirects).

*Hand-written illustration of the output format (not captured from a real model):*

```text
Overall diagnosis: The database is unreachable, so every request fails.

#1 ERROR  882×   ERROR connect ECONNREFUSED <IP>
Diagnosis  The app cannot connect to its database.
Cause      The database server is down or refusing connections.  (confidence: high)
Evidence   • ECONNREFUSED <IP_1>:5432
Check      $ docker ps -a
           $ docker logs --tail 50 db 2>&1
```

### 🏠 Local-first, bring your own model

Run fully offline with [Ollama](https://ollama.com), or use any OpenAI-compatible server or Anthropic. Strelizia also works without any model at all, in grouping-only mode. Model calls and alert delivery use only Python's standard library, so installing stays light.

### 🪶 Lightweight by design

Streaming processing, bounded memory, and a cache that skips the clustering work for repeated identical lines. On one cloud-VM core that is roughly **35,000 to 270,000 lines per second** depending on the format and how repetitive the logs are, using a few MB of extra memory. See [Benchmarks](#benchmarks) for the table and how to reproduce it.

---

## How it works

```
 Log source (Docker / file / stdin)
        │
        ▼
 Format adapters         JSON · nginx · syslog · journald (auto-detected)  ✅
        │
        ▼
 Multi-line assembler     joins stack traces into single records           ✅
        │
        ▼
 Clusterer (Drain3)       groups lines into patterns, locally              ✅
        │
        ├──► analyze:  ranker ──► terminal report / JSON                   ✅
        │
        └──► watch:    one window at a time (say 60s)
                │
                ▼
        Memory (SQLite)   Is this pattern new? How common is it usually?   ✅
                │
                ▼
        Rules             new · spike · still happening · restart · resolved  ✅
                │         cooldowns, quiet hours, one message per window
                ▼
        Redactor ──► optional LLM diagnosis ──► Discord / Slack / Telegram /
                                                 ntfy / webhook / file     ✅
```

---

## Supported log sources

| Source | Status |
|---|---|
| Docker containers (`--docker`) | ✅ Snapshot with `analyze`, live with `watch` |
| Log files and stdin | ✅ Live with `watch` (follows rotation and truncation) |
| systemd journal (`--journald`) | ✅ Linux with `journalctl` |

| Format | Status |
|---|---|
| JSON lines (pino, zap, structlog, logrus, journald, and similar) | ✅ Level, time, message and error fields are read; stack traces stay attached |
| nginx access and error logs | ✅ Groups by `GET /path returned HTTP 502`; the error log by cause and failing upstream |
| syslog (RFC 3164 and 5424) | ✅ Severity from the priority field when present |
| Python and Java stack traces, plain text | ✅ Multi-line records are assembled by heuristics |

Other formats fall back to the plain-text heuristics, which work reasonably but cluster on the whole line. Formats you want supported are a great first contribution (see `formats.py`).

---

## Benchmarks

Reproduce with `python benchmarks/bench.py`. Each case reads a generated log file from disk in its own process, so memory is the analyzer's alone.

*One core of a cloud VM (Intel Xeon @ 2.10GHz), Python 3.12, 300,000 lines per case, best of 2. The logs are synthetic, so treat the numbers as a rough guide and compare runs on the same machine.*

| Case | Lines per second | Extra memory |
|---|---:|---:|
| Plain text, an error flood | 209,000 | under 1 MB |
| Java stack traces | 267,000 | under 1 MB |
| nginx access + error logs | 85,000 | 5 MB |
| nginx, adapters off | 37,000 | 6 MB |
| JSON lines | 95,000 | 1 MB |
| JSON lines, adapters off | 38,000 | 6 MB |

Two things worth knowing. Format adapters make nginx and JSON logs about **2.3 to 2.5 times faster** (their clustering keys are shorter and repeat more), but add roughly **8 to 10 percent** to plain text, where every line has to be checked first. And repetitive logs (an error flood, which is exactly when you need the tool) are much faster than varied ones, because identical lines skip the clustering work entirely.

---

## Privacy

Server logs are sensitive, so Strelizia is built to be conservative:

- **`analyze` without `--explain` runs entirely on your machine.** Nothing is sent anywhere: no telemetry, no accounts, no network calls other than talking to your local Docker daemon.
- **Alerts leave your machine only if you set a `--notify` destination.** An alert contains the pattern (with variable parts and secrets masked), counts, and optionally one redacted sample (`--alert-samples`). Never your whole log. Use `--dry-run` to see alerts without sending them.
- **With `--explain`, only a small slice is sent to the model:** the top patterns (3 by default), each with its template, at most 2 clipped samples, and at most 2 records from just before it appeared, plus container facts. `strelizia analyze --dry-run` prints the exact payload.
- **It is all redacted first** (see [Redaction](#-redaction-built-in)).
- **With a local model through [Ollama](https://ollama.com), nothing leaves your machine.** With a cloud provider, Strelizia prints a notice naming the host before it sends.
- **Secrets stay secret.** API keys are read from environment variables only. Notification URLs can be passed with `--notify`, but prefer the `STRELIZIA_NOTIFY` environment variable, because command-line flags end up in your shell history. Neither is ever printed, logged, or stored, and notification errors never contain the URL.
- **Logs are treated as untrusted.** Log text can contain attacker-controlled content, so it is fenced off from the model's instructions, the model is told never to follow instructions found in it, the answer is validated strictly, and risky commands are hidden.
- **The memory file stays on your machine.** `~/.strelizia/state.db` holds pattern *templates*, counts, and alert history, never raw log lines. A template can still contain literal text from your logs that was not masked (for example an email that appears in every line of a pattern), so treat that file like your logs.

Redaction is pattern based, so it cannot catch everything. It will not recognise a bare person's name, an internal hostname, or a secret written in free text. Review `--dry-run` output when you first set it up, especially for custom log formats.

---

## Limitations

Strelizia is honest about what it is:

- Pattern grouping is a heuristic. Unusual log formats may split one problem into a few patterns or merge similar ones.
- Spike detection needs history: it only judges spikes after `--min-history` (10 minutes by default) of watching, and "usual" means the last 24 hours of what *it* has watched. A brand-new install has no idea what is normal yet.
- "Resolved" means a pattern stopped appearing, or a spike fell well below its alert rate and stayed there. A pattern that merely dropped to a lower but still elevated level is not announced.
- Format adapters cover the common formats. Anything else falls back to plain-text heuristics, which cluster on the whole line.
- Diagnoses are **hypotheses**. The real cause of a failure often lives outside the logs (config, a deploy, a full disk), so always check the evidence it shows. Smaller models are noticeably less reliable.
- Redaction cannot catch every secret (see [Privacy](#privacy)).
- It does not store or index your logs. It is an analyzer and alerter, not a log management system.

**Non-goals:** a web dashboard, long-term log storage or full-text search, automatic remediation, and replacing Datadog or Elastic at scale.

---

## Roadmap

- [x] **v0.1**: Docker, file and stdin sources, multi-line assembly, Drain3 grouping, `analyze` and `demo` commands
- [x] **v0.2**: redaction layer, `--dry-run`, LLM diagnosis with structured output (Ollama, OpenAI-compatible, Anthropic), Docker container facts
- [x] **v0.3**: watch mode, local memory, new / spike / still-happening / restart detection, cooldowns, quiet hours, alerts to Discord, Slack, Telegram, ntfy, webhooks and files
- [x] **v0.4**: format adapters (JSON, nginx, syslog, journald), "resolved" notices, daily digest, Docker Compose setup, reproducible benchmarks
- [ ] **Later**: publish to PyPI, custom rules file, feedback (👍/👎) to improve diagnoses, optional Apprise support for 100+ more services, Kubernetes support, more formats (Apache, HAProxy, logfmt)

---

## Tech stack

Python · [Drain3](https://github.com/logpai/Drain3) · Docker SDK · [Typer](https://typer.tiangolo.com) + [Rich](https://github.com/Textualize/rich) · SQLite · pytest. Model calls and alert delivery use the standard library only.

---

## Contributing

Contributions are welcome, especially **log samples and parsers for formats you run in the real world**. Please open an issue to discuss larger changes first.

```bash
git clone https://github.com/pham-02/strelizia.git
cd strelizia
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

Project layout: `assembler.py` (multi-line records), `clusterer.py` (Drain3 grouping), `sources/` (Docker, file and journald readers), `formats.py` (JSON, nginx and syslog adapters), `levels.py` (severity guessing), `digest.py` (daily summary), `benchmarks/` (reproducible speed tests), `redact.py` (secret masking), `llm.py` (model providers), `explain.py` (payload, parsing, command safety), `watch.py` (alert rules), `store.py` (memory), `follow.py` (live sources), `notify.py` (alert channels), `report.py` (terminal output), `demo.py` (log generator).

---

## License

MIT. See [LICENSE](LICENSE).

<p align="center">
  <sub>Built to find the signal in the noise.</sub>
</p>
