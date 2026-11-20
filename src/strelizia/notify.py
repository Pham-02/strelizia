"""Send alerts to Discord, Slack, Telegram, ntfy, any webhook, or a file.

You pass a URL per destination (`--notify URL`, repeatable, or the STRELIZIA_NOTIFY
environment variable, comma separated):

    https://discord.com/api/webhooks/...      Discord webhook
    https://hooks.slack.com/services/...      Slack incoming webhook
    telegram://BOT_TOKEN/CHAT_ID              Telegram bot
    ntfy://my-topic                           ntfy.sh   (ntfy://host/topic for your own server)
    https://example.com/hook                  any other URL gets a plain JSON POST
    file://alerts.jsonl                       append one JSON line per alert

URLs contain secrets (webhook tokens, bot tokens), so they are never printed, logged
or stored. Errors and the alert history only ever name the *kind* of destination.
Standard library only.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import parse_qs, quote, urlparse

TIMEOUT = 10.0


class NotifyError(Exception):
    """Delivery failed. The message never contains the destination URL."""


@dataclass
class Message:
    title: str
    text: str
    level: str = "ERROR"  # worst level in the message, used for priority
    alerts: list[dict] = field(default_factory=list)  # structured form for generic webhooks


def _is_local(host: Optional[str]) -> bool:
    return host in ("localhost", "127.0.0.1", "::1")


def _post(label: str, url: str, data: bytes, headers: dict[str, str]) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    host = urlparse(url).hostname
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if _is_local(host) else urllib.request.build_opener()
    last: Optional[NotifyError] = None
    for _attempt in (1, 2):
        try:
            with opener.open(req, timeout=TIMEOUT) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as exc:
            last = NotifyError(f"{label}: the service answered HTTP {exc.code}")
            if exc.code < 500 and exc.code != 429:
                break  # a client error will not fix itself on retry
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            last = NotifyError(f"{label}: could not connect ({type(reason).__name__})")
    raise last  # type: ignore[misc]


def _json(payload: dict) -> tuple[bytes, dict[str, str]]:
    return json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json; charset=utf-8"}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Notifier:
    label = "notifier"

    def send(self, message: Message) -> None:  # pragma: no cover
        raise NotImplementedError


class Discord(Notifier):
    label = "discord"

    def __init__(self, url: str) -> None:
        self._url = url

    def send(self, message: Message) -> None:
        body, headers = _json({"content": _clip(f"**{message.title}**\n{message.text}", 1900)})
        _post(self.label, self._url, body, headers)


class Slack(Notifier):
    label = "slack"

    def __init__(self, url: str) -> None:
        self._url = url

    def send(self, message: Message) -> None:
        body, headers = _json({"text": _clip(f"*{message.title}*\n{message.text}", 3500)})
        _post(self.label, self._url, body, headers)


class Telegram(Notifier):
    label = "telegram"

    def __init__(self, token: str, chat_id: str, api: str = "https://api.telegram.org") -> None:
        self._url = f"{api.rstrip('/')}/bot{token}/sendMessage"
        self._chat = chat_id

    def send(self, message: Message) -> None:
        body, headers = _json({"chat_id": self._chat, "text": _clip(f"{message.title}\n\n{message.text}", 4000)})
        _post(self.label, self._url, body, headers)


class Ntfy(Notifier):
    label = "ntfy"

    def __init__(self, base: str, topic: str) -> None:
        self._url = f"{base.rstrip('/')}/{quote(topic)}"

    def send(self, message: Message) -> None:
        priority = {"FATAL": "5", "ERROR": "4"}.get(message.level, "3")
        headers = {
            "Title": message.title.encode("ascii", "ignore").decode() or "Strelizia alert",  # headers must be ASCII
            "Priority": priority,
            "Tags": "rotating_light" if message.level in ("ERROR", "FATAL") else "warning",
            "Content-Type": "text/plain; charset=utf-8",
        }
        _post(self.label, self._url, _clip(message.text, 3500).encode("utf-8"), headers)


class Webhook(Notifier):
    label = "webhook"

    def __init__(self, url: str) -> None:
        self._url = url

    def send(self, message: Message) -> None:
        body, headers = _json({"title": message.title, "text": message.text, "level": message.level, "alerts": message.alerts})
        _post(self.label, self._url, body, headers)


class File(Notifier):
    """Append one JSON line per message. Handy for testing, and for piping alerts elsewhere."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self.label = "file"

    def send(self, message: Message) -> None:
        line = json.dumps({"title": message.title, "text": message.text, "level": message.level, "alerts": message.alerts})
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            raise NotifyError(f"file: could not write ({exc.strerror or type(exc).__name__})") from None


def parse_target(url: str) -> Notifier:
    """Turn one destination URL into a notifier. Raises ValueError for anything unrecognised."""
    url = url.strip()
    if url.startswith("file://"):
        path = url[len("file://"):]
        if not path:
            raise ValueError("file:// needs a path, e.g. file://alerts.jsonl")
        return File(path)

    parsed = urlparse(url)
    scheme, host = parsed.scheme.lower(), (parsed.hostname or "").lower()
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    if scheme == "telegram":
        token, chat = parsed.netloc, parsed.path.strip("/")
        if not token or not chat:
            raise ValueError("Telegram needs telegram://BOT_TOKEN/CHAT_ID")
        return Telegram(token, chat, api=query.get("api", "https://api.telegram.org"))

    if scheme in ("ntfy", "ntfy+http"):
        proto = "http" if scheme == "ntfy+http" else "https"
        parts = [p for p in (parsed.netloc + parsed.path).split("/") if p]
        if len(parts) == 1:
            return Ntfy("https://ntfy.sh", parts[0])
        if len(parts) == 2:
            return Ntfy(f"{proto}://{parts[0]}", parts[1])
        raise ValueError("ntfy needs ntfy://TOPIC or ntfy://HOST/TOPIC")

    if scheme in ("http", "https") and host:
        if host.endswith(("discord.com", "discordapp.com")) and "/webhooks/" in parsed.path:
            return Discord(url)
        if host == "hooks.slack.com":
            return Slack(url)
        if host == "ntfy.sh":
            return Ntfy("https://ntfy.sh", parsed.path.strip("/"))
        return Webhook(url)

    raise ValueError(
        "Unrecognised destination. Use a Discord or Slack webhook URL, telegram://TOKEN/CHAT_ID, "
        "ntfy://TOPIC, any https:// webhook, or file://PATH."
    )


def parse_targets(urls: Iterable[str], env: Optional[dict[str, str]] = None) -> list[Notifier]:
    """Combine `--notify` values with STRELIZIA_NOTIFY (comma separated)."""
    env = os.environ if env is None else env
    all_urls = [u for u in urls if u and u.strip()]
    all_urls += [u for u in env.get("STRELIZIA_NOTIFY", "").split(",") if u.strip()]
    notifiers: list[Notifier] = []
    for u in all_urls:
        try:
            notifiers.append(parse_target(u))
        except ValueError as exc:
            raise ValueError(str(exc)) from None  # drop the chained context: it could hold the URL
    return notifiers
