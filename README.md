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
  <img src="https://img.shields.io/badge/status-v0.1%20early%20development-F97316" alt="Status: v0.1 early development">
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

Coming next: **diagnosis** of the top patterns in plain English, **detection of what changed** since your last deploy, and **alerts** on Telegram, Discord, Slack, or ntfy. See the [roadmap](#roadmap).

> Grouping is done locally and deterministically. When LLM diagnosis arrives, the model will only be asked to explain the few top patterns, never to read your whole log. That keeps it fast, cheap, private, and more trustworthy.

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

# Anything piped in
docker logs my-api 2>&1 | strelizia analyze -
```

> `--docker` talks to the same Docker daemon that `docker logs` uses, so it works wherever that command works.

### Usage

**`strelizia analyze [FILE]`**: group a log into its distinct problems

| Option | Meaning |
|---|---|
| `FILE` | Log file to read, or `-` for stdin |
| `-d`, `--docker NAME` | Read from a Docker container instead of a file |
| `-s`, `--since 1h` | Docker only: how far back to read (`30m`, `1h`, `2d`, `1h30m`) |
| `-n`, `--top 10` | How many patterns to show |
| `-l`, `--min-level warn` | Ignore records below this level (`debug`, `info`, `warn`, `error`, `fatal`) |
| `--samples 2` | Sample log entries to show per pattern |
| `--json` | Machine-readable JSON instead of the terminal report |

**`strelizia demo`**: try it with generated logs

| Option | Meaning |
|---|---|
| `--scenario NAME` | `db-outage` (default), `nginx-502`, or `bad-deploy` |
| `--raw` | Print the generated raw logs instead of analyzing them |
| `--seed N` | Same seed, same logs |

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
| Secret redaction and `--dry-run` | 🔜 v0.2 |
| Plain-English diagnosis (Ollama or cloud models) | 🔜 v0.2 |
| "What changed" detection: new errors and spikes | 🔜 v0.3 |
| Watch mode and alerts (Telegram, Discord, Slack, ntfy) | 🔜 v0.3 |
| Dedicated nginx, syslog, and journald adapters | 🔜 v0.4 |

### 🧩 Smart log grouping

Uses log template mining (the Drain algorithm via [`drain3`](https://github.com/logpai/Drain3)) to collapse thousands of near-identical lines into a few patterns. Variable parts like IPs, ids, numbers, and timestamps are masked, while meaningful differences are kept: an HTTP `502` and a `404` are never merged, and each severity level is clustered separately.

### 📚 Multi-line awareness

One failure is often 10 to 50 lines. Strelizia glues stack-trace lines back together and builds each pattern from what identifies the problem: the exception and where it happened. Thousands of traces from one bug become one pattern, while different bugs stay separate.

### 🪶 Lightweight by design

Streaming processing with bounded memory. Current numbers (v0.1, synthetic logs on a small cloud VM, so treat them as a rough guide): about **50,000 lines per second** with **under 40 MB** of memory, collapsing roughly 180,000 records into 4 patterns.

### 🔍 What changed *(planned, v0.3)*

A local baseline will flag patterns that are **new** (first seen after the last restart or deploy), **spiking** (far above their normal rate), or **persistent**, so alerts read "New error since the 14:02 deploy" instead of a generic summary.

### 🧠 Plain-English diagnosis *(planned, v0.2)*

The top patterns will be sent to an LLM with useful context, and it must answer in a fixed structure: a summary, a likely cause (a hypothesis, not a fact), a confidence level, the log lines it based that on, and read-only commands to verify it. Strelizia will **never execute anything automatically.**

*Mock-up of the planned output (not implemented yet):*

```text
Diagnosis: The app cannot reach its database. Errors began right after the
container restarted, and the DB container shows 0 uptime, so it is likely
down or crash-looping.                                      Confidence: high
Check next:  docker ps -a | grep postgres
             docker logs --tail 50 postgres
```

### 🔔 Alerts where you already are *(planned, v0.3)*

Telegram, Discord, Slack, ntfy, and email through [Apprise](https://github.com/caronc/apprise), with per-pattern cooldowns, severity thresholds, quiet hours, and a daily digest, so you are not pinged repeatedly for the same problem.

---

## How it works

```
 Log source (Docker / file / stdin)
        │
        ▼
 Multi-line assembler     joins stack traces into single records           ✅
        │
        ▼
 Clusterer (Drain3)       groups lines into patterns, locally              ✅
        │
        ▼
 Ranker                   severity, then frequency                         ✅
        │
        ▼
 Terminal report / JSON                                                    ✅

 Coming:  Redactor → Baseline and spike detection → LLM explainer → Alerts
```

---

## Supported log sources

| Source | Status |
|---|---|
| Docker container logs (`--docker`) | ✅ Snapshot of recent logs |
| Any log file or stdin | ✅ |
| nginx (access and error) | Works today through file or stdin (auto-detected); dedicated adapter planned for v0.4 |
| Linux syslog / journald | Works today through file or stdin; journald adapter planned for v0.4 |
| JSON logs | Planned for v0.4 |

---

## Privacy

Server logs are sensitive, so Strelizia is built to be conservative:

- **v0.1 runs entirely on your machine.** Nothing is sent anywhere: no telemetry, no accounts, no network calls other than talking to your local Docker daemon.
- When LLM diagnosis arrives (v0.2), a **redaction layer** will mask IPs, emails, tokens, and keys first, only pattern templates and a few redacted samples will be sent, and `--dry-run` will show the exact payload before you trust it.
- With a local model through [Ollama](https://ollama.com), nothing will need to leave your machine at all.

---

## Limitations

Strelizia is honest about what it is:

- Pattern grouping is a heuristic. Unusual log formats may split one problem into a few patterns or merge similar ones.
- Planned diagnoses will be **hypotheses**. The real cause of a failure often lives outside the logs (config, a deploy, a full disk), so always check the evidence it shows.
- It does not store or index your logs. It is an analyzer, not a log management system.

**Non-goals:** a web dashboard, long-term log storage or full-text search, automatic remediation, and replacing Datadog or Elastic at scale.

---

## Roadmap

- [x] **v0.1**: Docker, file and stdin sources, multi-line assembly, Drain3 grouping, `analyze` and `demo` commands
- [ ] **v0.2**: redaction layer, LLM explainer with structured output, Ollama support, `--dry-run`
- [ ] **v0.3**: watch mode, local baseline, new and spike detection, cooldowns, Apprise alerts
- [ ] **v0.4**: nginx, syslog and journald adapters, daily digest, Docker Compose setup, benchmarks
- [ ] **Later**: custom rules file, feedback (👍/👎) to improve diagnoses, Kubernetes support

---

## Tech stack

Python · [Drain3](https://github.com/logpai/Drain3) · Docker SDK · [Typer](https://typer.tiangolo.com) + [Rich](https://github.com/Textualize/rich) · pytest

Planned: SQLite · [Apprise](https://github.com/caronc/apprise) · [LiteLLM](https://github.com/BerriAI/litellm) · Pydantic

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

Project layout: `assembler.py` (multi-line records), `clusterer.py` (Drain3 grouping), `sources/` (Docker and file readers), `report.py` (terminal output), `demo.py` (log generator).

---

## License

MIT. See [LICENSE](LICENSE).

<p align="center">
  <sub>Built to find the signal in the noise.</sub>
</p>
