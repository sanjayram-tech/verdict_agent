"""Provider adapters and model config, tested offline with fake SDK clients."""
from types import SimpleNamespace as NS

import pytest

import tyn.config as config
import tyn.model as model_mod
from tyn.config import ConfigError, ModelConfig
from tyn.model import (AnthropicModel, ModelError, OpenAICompatibleModel, ToolCall, build_model)

SPECS = {"get_issue": {"description": "d", "parameters": {"type": "object", "properties": {"ref": {"type": "string"}},
                                                          "required": ["ref"]}}}


@pytest.fixture(autouse=True)
def no_sleep_no_dotenv(monkeypatch):
    monkeypatch.setattr(model_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    for var in ("LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL", "LLM_TEMPERATURE", "LLM_MAX_TOKENS",
                "LLM_EFFORT", "GEMINI_API_KEY", "GEMINI_MODEL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "OPENROUTER_API_KEY", "ANTHROPIC_FALLBACKS", "LLM_MIN_INTERVAL_S", "GEMINI_MIN_INTERVAL_S"):
        monkeypatch.delenv(var, raising=False)


class HTTPErr(Exception):
    def __init__(self, code):
        super().__init__(f"status {code}")
        self.status_code = code


class Recorder:
    """Fake `create` that returns scripted responses (or raises scripted errors)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, **kw):
        self.calls.append({k: (list(v) if isinstance(v, list) else v) for k, v in kw.items()})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


# ------------------------------------------------------------------ Anthropic
def a_resp(content, stop="tool_use", inp=100, out=20, cache_read=0):
    return NS(content=content, stop_reason=stop, stop_details=None,
              usage=NS(input_tokens=inp, output_tokens=out, cache_read_input_tokens=cache_read,
                       cache_creation_input_tokens=0))


def install_anthropic(m, rec):
    m._client = NS(messages=NS(create=rec), beta=NS(messages=NS(create=rec)))


def test_anthropic_tool_loop_shapes():
    m = AnthropicModel("k", "claude-opus-5-5")
    tool_use = NS(type="tool_use", id="tu_1", name="get_issue", input={"ref": "REQ-01"})
    thinking = NS(type="thinking", thinking="", signature="sig")
    rec = Recorder(a_resp([thinking, tool_use], cache_read=50),
                   a_resp([NS(type="text", text="done")], stop="end_turn"))
    install_anthropic(m, rec)
    chat = m.start_chat("SYS", SPECS)
    t1 = chat.send_user("go")
    assert t1.calls == [ToolCall("get_issue", {"ref": "REQ-01"}, "tu_1")]
    assert t1.usage["prompt"] == 150 and t1.usage["total"] == 170
    first = rec.calls[0]
    assert first["model"] == "claude-opus-5-5" and first["system"] == "SYS"
    assert first["tools"][0]["input_schema"]["required"] == ["ref"]
    assert first["tool_choice"] == {"type": "auto"} and "temperature" not in first
    assert first["betas"] == ["server-side-fallback-2026-07-01"] and first["fallbacks"] == "default"
    t2 = chat.send_tool_results([(t1.calls[0], {"error": "nope"})], note="budget note")
    assert t2.text == "done" and not t2.calls
    msgs = rec.calls[1]["messages"]
    assert msgs[1] == {"role": "assistant", "content": [thinking, tool_use]}  # full content, unchanged
    tr, note = msgs[2]["content"]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "tu_1" and tr["is_error"] is True
    assert note == {"type": "text", "text": "budget note"}


def test_anthropic_fallbacks_only_where_supported():
    assert AnthropicModel("k", "claude-opus-5-5").fallbacks
    assert not AnthropicModel("k", "claude-haiku-4-5").fallbacks
    assert not AnthropicModel("k", "claude-opus-5-5", base_url="https://proxy.example").fallbacks
    assert not AnthropicModel("k", "claude-opus-5-5", fallbacks=False).fallbacks


def test_anthropic_refusal_and_retry():
    retries = []
    m = AnthropicModel("k", "claude-sonnet-5-5", on_retry=lambda *a: retries.append(a), effort="high")
    rec = Recorder(HTTPErr(429), HTTPErr(529), a_resp([NS(type="text", text="ok")], stop="end_turn"))
    install_anthropic(m, rec)
    assert m.start_chat("s", SPECS).send_user("hi").text == "ok"
    assert len(retries) == 2 and rec.calls[-1]["output_config"] == {"effort": "high"}
    install_anthropic(m, Recorder(a_resp([], stop="refusal")))
    with pytest.raises(ModelError, match="refusal"):
        m.start_chat("s", SPECS).send_user("hi")
    install_anthropic(m, Recorder(HTTPErr(400)))
    with pytest.raises(ModelError, match="HTTP 400"):
        m.start_chat("s", SPECS).send_user("hi")


# ------------------------------------------------------------------ OpenAI-compatible
def o_resp(content=None, tool_calls=None, finish="tool_calls"):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls), finish_reason=finish)],
              usage=NS(prompt_tokens=10, completion_tokens=5, total_tokens=15,
                       completion_tokens_details=NS(reasoning_tokens=2)))


def tc(cid, name, args):
    return NS(id=cid, function=NS(name=name, arguments=args))


def install_openai(m, rec):
    m._client = NS(chat=NS(completions=NS(create=rec)))


def test_openai_compatible_tool_loop_shapes():
    m = OpenAICompatibleModel("k", "some-model", base_url="http://localhost:9999/v1")
    rec = Recorder(o_resp(tool_calls=[tc("c1", "get_issue", '{"ref": "REQ-01"}'), tc(None, "get_issue", "{bad")]),
                   o_resp(content="final", finish="stop"))
    install_openai(m, rec)
    chat = m.start_chat("SYS", SPECS)
    t1 = chat.send_user("go")
    assert t1.calls[0] == ToolCall("get_issue", {"ref": "REQ-01"}, "c1")
    assert "__invalid_arguments__" in t1.calls[1].args and t1.calls[1].call_id == "call_2"
    assert t1.usage == {"prompt": 10, "output": 5, "thoughts": 2, "total": 15}
    first = rec.calls[0]
    assert first["messages"][0] == {"role": "system", "content": "SYS"}
    assert first["tools"][0]["function"]["name"] == "get_issue" and "temperature" not in first
    chat.send_tool_results([(t1.calls[0], {"ok": 1}), (t1.calls[1], {"error": "bad args"})], note="n")
    msgs = rec.calls[1]["messages"]
    assert msgs[2]["role"] == "assistant" and [c["id"] for c in msgs[2]["tool_calls"]] == ["c1", "call_2"]
    assert msgs[3]["role"] == "tool" and msgs[3]["tool_call_id"] == "c1"
    assert msgs[4]["tool_call_id"] == "call_2" and msgs[5] == {"role": "user", "content": "n"}


def test_openai_temperature_passed_when_set():
    m = OpenAICompatibleModel("k", "m", temperature=0.0, max_tokens=500)
    rec = Recorder(o_resp(content="x", finish="stop"))
    install_openai(m, rec)
    m.start_chat("s", SPECS).send_user("hi")
    assert rec.calls[0]["temperature"] == 0.0 and rec.calls[0]["max_tokens"] == 500


# ------------------------------------------------------------------ Gemini (native)
def g_resp(parts=None, finish="STOP"):
    content = None if parts is None else NS(parts=parts, role="model")
    return NS(candidates=[NS(content=content, finish_reason=finish)], prompt_feedback=None,
              usage_metadata=NS(prompt_token_count=10, candidates_token_count=2, thoughts_token_count=0,
                                total_token_count=12))


def test_gemini_malformed_call_is_recoverable_and_roles_alternate():
    from tyn.model import MALFORMED_CALL, GeminiModel
    m = GeminiModel("dummy-key", "gemini-2.5-flash")
    fc = NS(name="get_issue", args={"ref": "REQ-01"}, id="f1")
    rec = Recorder(g_resp(finish="FinishReason.MALFORMED_FUNCTION_CALL"),
                   g_resp([NS(function_call=fc, text=None, thought=False)]))
    m._client = NS(models=NS(generate_content=lambda **kw: rec(**{**kw, "contents": list(kw["contents"])})))
    chat = m.start_chat("sys", SPECS)
    t1 = chat.send_user("go")
    assert t1.calls == [] and t1.finish_reason == MALFORMED_CALL
    t2 = chat.send_user("your call was malformed, retry")
    assert t2.calls[0].name == "get_issue"
    second = rec.calls[1]["contents"]
    assert len(second) == 1 and second[0].role == "user" and len(second[0].parts) == 2  # merged, no user/user pair
    rec2 = Recorder(g_resp(finish="FinishReason.SAFETY"))
    m._client = NS(models=NS(generate_content=rec2))
    with pytest.raises(ModelError, match="no content"):
        m.start_chat("sys", SPECS).send_user("go")


def test_connect_errors_are_retried():
    class ConnectError(Exception):
        pass
    retries = []
    m = OpenAICompatibleModel("k", "m", on_retry=lambda *a: retries.append(a))
    install_openai(m, Recorder(ConnectError("dns"), o_resp(content="ok", finish="stop")))
    assert m.start_chat("s", SPECS).send_user("hi").text == "ok" and len(retries) == 1


# ------------------------------------------------------------------ config + factory
def test_config_defaults_and_validation(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    c = ModelConfig.from_env()
    assert (c.provider, c.model, c.api_key) == ("gemini", "gemini-2.5-flash", "g-key")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    c = ModelConfig.from_env()
    assert c.model == "claude-opus-5-5" and c.anthropic_fallbacks
    with pytest.raises(ConfigError, match="LLM_MODEL"):
        ModelConfig.from_env("openrouter")
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        ModelConfig.from_env("openrouter", "vendor/model")
    c = ModelConfig.from_env("ollama", "qwen3")  # local: no key needed
    assert c.api_key is None
    with pytest.raises(ConfigError, match="not supported"):
        ModelConfig.from_env("bogus")


def test_generic_key_and_overrides(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL", "my-model")
    monkeypatch.setenv("LLM_API_KEY", "generic")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.2")
    monkeypatch.setenv("LLM_MIN_INTERVAL_S", "6")
    c = ModelConfig.from_env()
    assert (c.api_key, c.temperature, c.min_interval_s) == ("generic", 0.2, 6.0)
    assert ModelConfig.from_env(model="other").model == "other"


def test_factory_builds_each_provider(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    m = build_model(ModelConfig.from_env("ollama", "qwen3"))
    assert isinstance(m, OpenAICompatibleModel) and m.provider == "ollama"
    assert str(m._client.base_url).startswith("http://localhost:11434/v1")
    assert isinstance(build_model(ModelConfig.from_env("anthropic")), AnthropicModel)
    g = build_model(ModelConfig.from_env("gemini"))
    assert g.provider == "gemini" and g.temperature == 0.0
