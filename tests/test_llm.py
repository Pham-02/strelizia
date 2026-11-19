import pytest

from strelizia import llm
from strelizia.llm import LLMConfig, LLMError, build_request, complete, resolve_config


def test_model_is_required_with_helpful_message():
    with pytest.raises(LLMError, match="--model"):
        resolve_config("ollama", None, None, env={})
    cfg = resolve_config("ollama", None, None, env={}, require_model=False)  # --dry-run
    assert "not set" in cfg.model


def test_model_and_url_from_environment():
    env = {"STRELIZIA_MODEL": "llama3.1", "OLLAMA_HOST": "gpu-box:11500"}
    cfg = resolve_config("ollama", None, None, env=env)
    assert (cfg.model, cfg.base_url) == ("llama3.1", "http://gpu-box:11500")
    assert resolve_config("ollama", "m", None, env={"OLLAMA_HOST": "gpu-box"}).base_url == "http://gpu-box:11434"
    assert resolve_config("ollama", "m", "http://x:1/", env={"OLLAMA_HOST": "ignored"}).base_url == "http://x:1"


def test_api_keys_from_environment_only():
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        resolve_config("openai", "m", None, env={})
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        resolve_config("anthropic", "m", None, env={})
    assert resolve_config("openai", "m", None, env={"OPENAI_API_KEY": "k"}).api_key == "k"
    assert resolve_config("openai", "m", None, env={"STRELIZIA_API_KEY": "k2"}).api_key == "k2"
    # an OpenAI-compatible local server does not need a key
    assert resolve_config("openai", "m", "http://localhost:1234/v1", env={}).api_key is None


def test_unknown_provider():
    with pytest.raises(LLMError, match="Unknown provider"):
        resolve_config("gemini", "m", None, env={})


def test_local_detection():
    assert LLMConfig("ollama", "m", "http://localhost:11434").is_local
    assert LLMConfig("ollama", "m", "http://127.0.0.1:1").is_local
    assert not LLMConfig("openai", "m", "https://api.openai.com/v1").is_local


def test_request_shapes():
    o = build_request(LLMConfig("ollama", "llama3.1", "http://localhost:11434"), "SYS", "USR")
    assert o.url == "http://localhost:11434/api/chat"
    assert o.body["stream"] is False and o.body["format"] == "json"
    assert o.body["options"]["num_ctx"] == llm.OLLAMA_NUM_CTX
    assert "Authorization" not in o.headers

    a = build_request(LLMConfig("openai", "gpt", "https://api.openai.com/v1", "KEY"), "SYS", "USR")
    assert a.url == "https://api.openai.com/v1/chat/completions"
    assert a.headers["Authorization"] == "Bearer KEY"
    assert a.body["response_format"] == {"type": "json_object"}
    assert "temperature" not in a.body  # newer models reject non-default values

    c = build_request(LLMConfig("anthropic", "claude", "https://api.anthropic.com", "KEY"), "SYS", "USR")
    assert c.url == "https://api.anthropic.com/v1/messages"
    assert c.headers["x-api-key"] == "KEY" and "anthropic-version" in c.headers
    assert c.body["system"] == "SYS" and c.body["messages"] == [{"role": "user", "content": "USR"}]


def test_ollama_call(fake_llm):
    cfg = LLMConfig("ollama", "llama3.1", fake_llm.url)
    assert "database" in complete(cfg, "sys", "user")
    (req,) = fake_llm.requests
    assert req.path == "/api/chat" and req.body["model"] == "llama3.1"
    assert req.body["messages"][1]["content"] == "user"


def test_openai_compatible_call(fake_llm):
    cfg = LLMConfig("openai", "m", fake_llm.url + "/v1", "SECRET")
    assert "database" in complete(cfg, "sys", "user")
    (req,) = fake_llm.requests
    assert req.path == "/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer SECRET"


def test_anthropic_call(fake_llm):
    cfg = LLMConfig("anthropic", "m", fake_llm.url, "SECRET")
    assert "database" in complete(cfg, "sys", "user")
    (req,) = fake_llm.requests
    assert req.path == "/v1/messages" and req.headers["x-api-key"] == "SECRET"


def test_openai_retries_without_response_format_on_400(fake_llm):
    fake_llm.statuses = [400]
    cfg = LLMConfig("openai", "m", fake_llm.url + "/v1", "k")
    assert "database" in complete(cfg, "sys", "user")
    assert len(fake_llm.requests) == 2
    assert "response_format" in fake_llm.requests[0].body
    assert "response_format" not in fake_llm.requests[1].body


def test_error_messages_are_helpful(fake_llm):
    fake_llm.statuses = [401]
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        complete(LLMConfig("openai", "m", fake_llm.url + "/v1", "bad"), "s", "u")
    fake_llm.statuses = [404]
    with pytest.raises(LLMError, match="ollama pull nope"):
        complete(LLMConfig("ollama", "nope", fake_llm.url), "s", "u")
    fake_llm.statuses = [429]
    with pytest.raises(LLMError, match="rate limiting"):
        complete(LLMConfig("anthropic", "m", fake_llm.url, "k"), "s", "u")


def test_connection_refused_mentions_ollama_serve():
    cfg = LLMConfig("ollama", "m", "http://127.0.0.1:9")  # nothing listens on the discard port
    with pytest.raises(LLMError, match="ollama serve"):
        complete(cfg, "s", "u")


def test_timeout(fake_llm):
    fake_llm.delay = 1.0
    cfg = LLMConfig("ollama", "m", fake_llm.url, timeout=0.3)
    with pytest.raises(LLMError, match="did not answer"):
        complete(cfg, "s", "u")
