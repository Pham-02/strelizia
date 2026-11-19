"""Talk to a language model: Ollama (local), OpenAI-compatible APIs, or Anthropic.

Standard library only, so installing Strelizia stays light. API keys are read from
environment variables and are never accepted as command line flags (those end up in
shell history) and never printed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlparse

PROVIDERS = ("ollama", "openai", "anthropic")
DEFAULT_URLS = {
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}
KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
OLLAMA_NUM_CTX = 8192  # Ollama's default window is small and silently drops the start of long prompts


class LLMError(Exception):
    """A model call failed. The message is safe and useful to show to the user."""


@dataclass
class LLMConfig:
    provider: str
    model: str
    base_url: str
    api_key: Optional[str] = None
    timeout: float = 180.0

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or self.base_url

    @property
    def is_local(self) -> bool:
        return self.host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


@dataclass
class Request:
    url: str
    headers: dict[str, str]
    body: dict


def resolve_config(
    provider: str,
    model: Optional[str],
    base_url: Optional[str],
    env: Optional[Mapping[str, str]] = None,
    require_model: bool = True,
) -> LLMConfig:
    env = os.environ if env is None else env
    provider = provider.lower()
    if provider not in PROVIDERS:
        raise LLMError(f"Unknown provider {provider!r}. Choose one of: {', '.join(PROVIDERS)}.")

    model = model or env.get("STRELIZIA_MODEL")
    if not model:
        if require_model:
            examples = {"ollama": "llama3.1", "openai": "<a model name>", "anthropic": "<a model name>"}
            raise LLMError(
                f"No model chosen. Pass --model (for example --model {examples[provider]}) "
                "or set STRELIZIA_MODEL."
            )
        model = "(not set: pass --model)"

    url = base_url or env.get("STRELIZIA_BASE_URL")
    if not url and provider == "ollama":
        host = env.get("OLLAMA_HOST")
        if host:
            url = host if "://" in host else f"http://{host}"
            if url.count(":") < 2:  # no port given
                url += ":11434"
    url = (url or DEFAULT_URLS[provider]).rstrip("/")

    key = env.get("STRELIZIA_API_KEY") or env.get(KEY_ENV.get(provider, ""), None)
    needs_key = provider == "anthropic" or (provider == "openai" and url == DEFAULT_URLS["openai"])
    if needs_key and not key and require_model:
        raise LLMError(f"No API key found. Set the {KEY_ENV[provider]} environment variable.")
    return LLMConfig(provider=provider, model=model, base_url=url, api_key=key)


def build_request(cfg: LLMConfig, system: str, user: str, *, json_mode: bool = True) -> Request:
    """Build the exact HTTP request. Used for real calls and for --dry-run."""
    headers = {"Content-Type": "application/json"}
    if cfg.provider == "ollama":
        body: dict = {
            "model": cfg.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0, "num_ctx": OLLAMA_NUM_CTX},
        }
        if json_mode:
            body["format"] = "json"
        return Request(f"{cfg.base_url}/api/chat", headers, body)

    if cfg.provider == "openai":
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        body = {
            "model": cfg.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return Request(f"{cfg.base_url}/chat/completions", headers, body)

    headers["x-api-key"] = cfg.api_key or ""
    headers["anthropic-version"] = "2023-06-01"
    body = {
        "model": cfg.model,
        "max_tokens": 1500,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    return Request(f"{cfg.base_url}/v1/messages", headers, body)


def _post(cfg: LLMConfig, req: Request) -> dict:
    data = json.dumps(req.body).encode("utf-8")
    http_req = urllib.request.Request(req.url, data=data, headers=req.headers, method="POST")
    # A system-wide HTTP proxy must not swallow calls to a model running on this machine.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if cfg.is_local else urllib.request.build_opener()
    try:
        with opener.open(http_req, timeout=cfg.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _http_error(cfg, exc) from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            raise _timeout(cfg) from None
        hint = " Is Ollama running? Try `ollama serve`." if cfg.provider == "ollama" else ""
        # Windows and Linux word this very differently and at great length; keep it short.
        detail = "connection refused" if isinstance(reason, ConnectionRefusedError) else str(reason)
        raise LLMError(f"Could not connect to {cfg.base_url} ({detail}).{hint}") from None
    except TimeoutError:
        raise _timeout(cfg) from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise LLMError(f"{cfg.base_url} did not return valid JSON. Is the base URL correct?") from None


def _timeout(cfg: LLMConfig) -> LLMError:
    return LLMError(
        f"The model did not answer within {int(cfg.timeout)}s. Local models can be slow the first "
        "time (loading) or on large prompts. Try a smaller model."
    )


def _http_error(cfg: LLMConfig, exc: urllib.error.HTTPError) -> LLMError:
    try:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
    except Exception:  # pragma: no cover
        detail = ""
    if exc.code in (401, 403):
        env_name = KEY_ENV.get(cfg.provider, "STRELIZIA_API_KEY")
        return LLMError(f"The API rejected the key (HTTP {exc.code}). Check {env_name}.")
    if exc.code == 404 and cfg.provider == "ollama":
        return LLMError(
            f"Ollama does not have the model {cfg.model!r}. Install it with `ollama pull {cfg.model}` "
            "or see what you have with `ollama list`."
        )
    if exc.code == 429:
        return LLMError("The API is rate limiting you (HTTP 429). Wait a moment and try again.")
    return LLMError(f"The model API returned HTTP {exc.code}. {detail}".strip())


def _extract_text(cfg: LLMConfig, resp: dict) -> str:
    try:
        if cfg.provider == "ollama":
            return resp["message"]["content"]
        if cfg.provider == "openai":
            return resp["choices"][0]["message"]["content"] or ""
        return "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"Unexpected response shape from {cfg.provider}.") from None


def complete(cfg: LLMConfig, system: str, user: str) -> str:
    """Send one request and return the model's text."""
    req = build_request(cfg, system, user, json_mode=True)
    try:
        resp = _post(cfg, req)
    except LLMError as exc:
        # Some OpenAI-compatible servers do not support response_format: retry once without it.
        if cfg.provider == "openai" and "HTTP 400" in str(exc):
            resp = _post(cfg, build_request(cfg, system, user, json_mode=False))
        else:
            raise
    return _extract_text(cfg, resp)
