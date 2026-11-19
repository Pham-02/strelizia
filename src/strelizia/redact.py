"""Redaction: mask secrets and identifying details before any text leaves the machine.

Every value is replaced by a stable placeholder (`<IP_1>`, `<TOKEN_2>`), so the same
IP is always the same placeholder and a model can still reason about patterns
("the same host failed 400 times") without ever seeing the real value.

Redaction is pattern based. It catches the common cases, but it cannot know every
secret, which is why `--dry-run` exists: look at what would be sent first.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: re.Pattern
    group: int = 0  # which regex group holds the sensitive value (0 = whole match)
    validate: Optional[Callable[[str], bool]] = None


def _entropy(value: str) -> float:
    counts = {c: value.count(c) for c in set(value)}
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def _looks_like_token(value: str) -> bool:
    return (
        any(c.isdigit() for c in value)
        and any(c.isalpha() for c in value)
        and _entropy(value) >= 3.5
    )


def _valid_ipv4(value: str) -> bool:
    return all(int(octet) <= 255 for octet in value.split("."))


def _looks_like_secret_value(value: str) -> bool:
    """Skip plain words such as `token: expired` so diagnoses stay readable."""
    if len(value) < 6:
        return False
    return not (value.isalpha() and value.islower() and len(value) <= 12)


_KV_NAMES = r"(?:password|passwd|pwd|secret|client[_-]?secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key)"

RULES: list[Rule] = [
    Rule("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    Rule("TOKEN", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),  # JWT
    Rule("TOKEN", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),  # AWS access key id
    Rule("TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),  # GitHub
    Rule("TOKEN", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),  # Slack
    Rule("TOKEN", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),  # Stripe
    Rule("TOKEN", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),  # Google API key
    Rule("TOKEN", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),  # OpenAI / Anthropic style keys
    Rule("CREDENTIAL", re.compile(r"(?<=://)([^/\s:@]+:[^/\s@]+)(?=@)"), 1),  # scheme://user:pass@host
    Rule("TOKEN", re.compile(r"(?i)\b(?:bearer|basic)\s+([A-Za-z0-9._~+/=-]{8,})"), 1),
    Rule(
        "SECRET",
        re.compile(rf"(?i)\b{_KV_NAMES}[\"']?\s*[:=]\s*[\"']?(?!<)([^\s\"',;&)}}\]]{{3,}})"),
        1,
        _looks_like_secret_value,
    ),
    Rule("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    Rule("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), 0, _valid_ipv4),
    Rule("IP", re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}(?![\w:])")),  # full IPv6
    Rule("IP", re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){0,6}[0-9A-Fa-f]{0,4}::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?(?![\w:])")),  # compressed IPv6
    Rule("USER", re.compile(r"(?:/home/|/Users/|(?i:[A-Z]:\\Users\\))([^/\\\s]+)(?=[/\\])"), 1),
    Rule("TOKEN", re.compile(r"(?<![\w/.<-])[A-Za-z0-9_\-+=]{24,}(?![\w/.>])"), 0, _looks_like_token),
]


class Redactor:
    """Applies the rules and remembers which placeholder belongs to which value."""

    def __init__(self) -> None:
        self._maps: dict[str, dict[str, str]] = {}

    def _placeholder(self, kind: str, value: str) -> str:
        table = self._maps.setdefault(kind, {})
        if value not in table:
            table[value] = f"<{kind}_{len(table) + 1}>"
        return table[value]

    def redact(self, text: str) -> str:
        for rule in RULES:
            text = rule.pattern.sub(lambda m, r=rule: self._replace(r, m), text)
        return text

    def _replace(self, rule: Rule, m: re.Match) -> str:
        value = m.group(rule.group)
        if rule.validate and not rule.validate(value):
            return m.group(0)
        placeholder = self._placeholder(rule.kind, value)
        if rule.group == 0:
            return placeholder
        start = m.start()
        return m.group(0)[: m.start(rule.group) - start] + placeholder + m.group(0)[m.end(rule.group) - start :]

    def counts(self) -> dict[str, int]:
        """How many distinct values were masked, per kind."""
        return {kind: len(table) for kind, table in self._maps.items() if table}

    def summary(self) -> str:
        counts = self.counts()
        if not counts:
            return "nothing needed masking"
        return ", ".join(f"{n} {kind.lower().replace('_', ' ')}{'s' if n != 1 else ''}" for kind, n in sorted(counts.items()))
