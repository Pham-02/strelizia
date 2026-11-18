<div align="center">

# 🌺 Strelizia

**Turn 40,000 repetitive log lines into the 3 problems that actually matter.**

A lightweight, local-first log analyzer that watches your server logs, groups noisy errors into root causes, and tells you what went wrong in plain English.

![Status](https://img.shields.io/badge/status-early%20development-orange)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

<!-- Replace with your demo GIF: -->
<!-- ![Strelizia demo](docs/demo.gif) -->

</div>

---

## What is Strelizia?

Anyone who runs a server knows the feeling: something is broken, and the answer is buried somewhere in 40,000 lines of `docker logs` or `/var/log/nginx/error.log`, most of which are the same error repeated again and again.

Big observability platforms (Datadog, Elastic, Grafana Loki) solve this, but they are heavy, expensive, or complicated to set up for a solo developer, a small team, or a homelab.

**Strelizia** is the lightweight alternative. It runs next to your services, reads their logs, and:

1. **Groups** thousands of similar lines into a handful of distinct error patterns
2. **Detects what changed**: new errors, sudden spikes, problems that started after a deploy
3. **Explains** the top issues in plain English, with evidence and next steps
4. **Alerts** you on Telegram, Discord, Slack, or ntfy, only when it matters

> Grouping is done locally and deterministically. An LLM is only asked to explain the few top clusters, never to read your whole log. That keeps it fast, cheap, private, and more trustworthy.

---

## Example

Instead of this:

```
2026-11-18 14:02:11 ERROR connect ECONNREFUSED 10.0.0.5:5432
2026-11-18 14:02:11 ERROR connect ECONNREFUSED 10.0.0.5:5432
2026-11-18 14:02:12 ERROR connect ECONNREFUSED 10.0.0.5:5432
... (38,412 more lines)
```

You get this *(illustrative output)*:

```
╭─ Strelizia report · last 1h · container: api ───────────────────────╮
│                                                                     │
│ 🔴 NEW  ·  38,415 occurrences  ·  first seen 14:02 (after restart)  │
│ Pattern: connect ECONNREFUSED <IP_1>:<PORT_1>                       │
│                                                                     │
│ Diagnosis: The app cannot reach its database. Errors began right    │
│ after the container restarted, and the DB container shows 0         │
│ uptime, so it is likely down or crash-looping.                      │
│ Confidence: high                                                    │
│                                                                     │
│ Check next:                                                         │
│   docker ps -a | grep postgres                                      │
│   docker logs --tail 50 postgres                                    │
│                                                                     │
│ 🟡 SPIKE  ·  1,204 occurrences  ·  12x above baseline               │
│ Pattern: 502 Bad Gateway upstream=<IP_1>                            │
│ ...                                                                 │
╰─────────────────────────────────────────────────────────────────────╯
```

---

## Features

### 🧩 Smart log grouping
Uses log template mining (the Drain algorithm via [`drain3`](https://github.com/logpai/Drain3)) to collapse thousands of near-identical lines into a few patterns, with variable parts like IPs, IDs, and timestamps abstracted away. No LLM needed for this step.

### 🔍 "What changed" detection
Strelizia keeps a baseline in a local SQLite database. It flags clusters that are:
- **New**: never seen before, or first seen after the last container restart or deploy marker
- **Spiking**: far above their normal rate
- **Persistent**: still happening after an earlier alert

Alerts read like "New error since the 14:02 deploy" instead of a generic summary.

### 🧠 Plain-English diagnosis
Top clusters are sent to an LLM along with useful context: the pattern, counts, first/last seen times, a few sample lines, the lines leading up to the error, and system facts such as disk usage, memory, and container restart count. The model must reply in a structured format:

| Field | Meaning |
|---|---|
| `summary` | What is happening, in one or two sentences |
| `likely_cause` | The most probable explanation (a hypothesis, not a fact) |
| `confidence` | low / medium / high |
| `evidence` | The log lines the conclusion is based on |
| `things_to_check` | Read-only commands to verify it |

Strelizia **never executes anything automatically.**

### 🔒 Built-in redaction
Before anything leaves your machine, a redaction layer masks IP addresses, emails, JWTs, API keys, bearer tokens, and other high-entropy secrets. Placeholders are stable (`<IP_1>`, `<TOKEN_2>`), so the model can still reason about patterns. Only the cluster template and a few redacted samples are ever sent, never raw logs.

Use `--dry-run` to see exactly what would be sent.

### 🏠 Local-first, bring your own model
Run fully offline with [Ollama](https://ollama.com), or use OpenAI, Anthropic, or Gemini by changing one line in the config. Strelizia also works without any LLM at all, in clustering-only mode.

### 🔔 Alerts where you already are
Telegram, Discord, Slack, ntfy, email, and more through [Apprise](https://github.com/caronc/apprise). Includes:
- Per-cluster **cooldowns** so you are not pinged repeatedly for the same problem
- **Severity scoring** (level, frequency, novelty, spike size) with a configurable threshold
- A **daily digest** for everything below the alert threshold
- **Quiet hours**

### 🪶 Lightweight by design
Streaming processing with bounded memory, persistent clusterer state across restarts, automatic reconnection after log rotation or container restarts, and rate-limited LLM calls.

### 🧪 Demo log generator
No broken server handy? Generate realistic failures (database outages, OOM kills, 502 storms, a bad deploy) and watch Strelizia diagnose them in seconds.

---

## How it works

```
 Log source (Docker / nginx / syslog)
        │
        ▼
 Multi-line assembler     joins stack traces into single records
        │
        ▼
 Redactor                 masks IPs, emails, tokens, secrets
        │
        ▼
 Clusterer (Drain3)       groups lines into patterns, locally
        │
        ▼
 Scorer                   novelty · spike · severity vs. baseline (SQLite)
        │
        ▼
 LLM explainer            top N clusters only, with context
        │
        ▼
 Notifier + report        Telegram · Discord · Slack · ntfy · CLI
```

---

## Supported log sources

| Source | Status |
|---|---|
| Docker container logs | Planned for v0.1 |
| nginx (access + error) | Planned for v0.4 |
| Linux syslog / journald | Planned for v0.4 |
| Generic JSON logs | Planned for v0.4 |

---

## Quick start

> ⚠️ Strelizia is in early development. The commands below describe the intended interface and will land as the roadmap progresses.

### Install from source

```bash
git clone https://github.com/pham-02/strelizia.git
cd strelizia
pip install -e .
```

### Analyze a container's recent logs

```bash
strelizia analyze --docker my-api --since 1h
```

### Watch continuously and send alerts

```bash
strelizia watch --docker my-api --config strelizia.yml
```

### See exactly what would be sent to an LLM

```bash
strelizia analyze --docker my-api --dry-run
```

### Try the demo

```bash
strelizia demo --scenario db-outage
```

### Run with Docker Compose

```yaml
services:
  strelizia:
    image: strelizia:latest
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./strelizia.yml:/app/strelizia.yml:ro
      - strelizia-data:/data
volumes:
  strelizia-data:
```

> 🔐 **Security note:** mounting the Docker socket gives a container broad access to your Docker daemon, even with `:ro`. For stricter setups, put a read-only proxy such as [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) in front of it.

---

## Configuration

`strelizia.yml`:

```yaml
sources:
  - type: docker
    containers: ["api", "worker", "nginx"]

llm:
  provider: ollama          # ollama | openai | anthropic | gemini | none
  model: llama3.1
  max_clusters_per_report: 5

redaction:
  enabled: true             # keep this on for cloud providers

alerts:
  channels:
    - "tgram://BOT_TOKEN/CHAT_ID"
    - "discord://WEBHOOK_ID/WEBHOOK_TOKEN"
  min_severity: medium
  cooldown: 2h
  quiet_hours: "23:00-07:00"
  daily_digest: "09:00"

detection:
  spike_factor: 5           # alert when rate is 5x baseline
```

---

## Privacy

Server logs are sensitive, so Strelizia is built to be conservative:

- Clustering runs **entirely on your machine**
- With Ollama, **nothing leaves your machine at all**
- With a cloud LLM, only **redacted cluster templates and a few redacted sample lines** are sent
- `--dry-run` shows the exact payload before you trust it
- No telemetry, no accounts

Redaction is pattern-based and cannot catch every possible secret. Review the dry-run output when you first set it up, especially for custom log formats.

---

## Limitations

Strelizia is honest about what it is:

- Diagnoses are **hypotheses**. The real cause of a failure often lives outside the logs (config, a deploy, a full disk), so always check the evidence it shows.
- It does not store or index your logs. It is an analyzer, not a log management system.
- Accuracy depends on log quality and on the model you choose.

### Non-goals

- A web dashboard
- Long-term log storage or full-text search
- Automatic remediation (it will never run fixes for you)
- Replacing Datadog or Elastic at scale

---

## Roadmap

- [ ] **v0.1**: Docker log adapter, multi-line assembler, Drain3 clustering, `analyze` command (no LLM)
- [ ] **v0.2**: Redaction, LLM explainer with structured output, Ollama support, `--dry-run`
- [ ] **v0.3**: Watch mode, SQLite baseline, new/spike detection, cooldowns, Apprise alerts
- [ ] **v0.4**: nginx and syslog adapters, daily digest, Docker Compose setup, demo generator, benchmarks
- [ ] **Later**: custom rules file, feedback (👍/👎) to improve diagnoses, Kubernetes support

---

## Tech stack

Python · [Drain3](https://github.com/logpai/Drain3) · Docker SDK · SQLite · [Typer](https://typer.tiangolo.com) + [Rich](https://github.com/Textualize/rich) · [Apprise](https://github.com/caronc/apprise) · [LiteLLM](https://github.com/BerriAI/litellm) · Pydantic

---

## Contributing

Contributions are welcome, especially log samples and parsers for formats you run in the real world. Good first issues are labeled `good first issue`. Please open an issue to discuss larger changes first.

```bash
git clone https://github.com/pham-02/strelizia.git
cd strelizia
pip install -e ".[dev]"
pytest
```

---

## License

MIT. See [LICENSE](LICENSE).

---

<div align="center">
<sub>Named after the flower that thrives where it shouldn't. Built to find the signal in the noise.</sub>
</div>
