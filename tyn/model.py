"""Model adapters. The agent depends only on `Model.start_chat()` and `Chat.send_*()`
returning `Turn`s, so any provider can be plugged in (and tests use a scripted model).

Providers (set LLM_PROVIDER):
  gemini     native Google GenAI SDK
  anthropic  native Anthropic SDK (Claude)
  openai     any OpenAI-compatible Chat Completions endpoint: OpenAI, Azure, OpenRouter,
             Groq, DeepSeek, Mistral, Together, Ollama, vLLM, LM Studio, ... (LLM_BASE_URL)
  openrouter / groq / deepseek / mistral / together / ollama
             shortcuts for `openai` with that service's base URL preset

All adapters use manual function calling (the agent loop runs the tools), share
the same retry/backoff + rate spacing, and report token usage in one shape.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    call_id: str | None = None


@dataclass
class Turn:
    calls: list[ToolCall]
    text: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    finish_reason: str | None = None


class ModelError(RuntimeError):
    """Model call failed after retries (or non-retryable). Reported, never hidden."""


# Normalised finish reason for "the model attempted a tool call but its arguments were invalid".
MALFORMED_CALL = "MALFORMED_FUNCTION_CALL"


class Chat(Protocol):
    def send_user(self, text: str) -> Turn: ...
    def send_tool_results(self, results: list[tuple[ToolCall, dict[str, Any]]], note: str | None = None) -> Turn: ...


class Model(Protocol):
    name: str
    def start_chat(self, system: str, tools: dict[str, dict[str, Any]]) -> Chat: ...


OPENAI_COMPATIBLE_PRESETS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com",
    "mistral": "https://api.mistral.ai/v1",
    "together": "https://api.together.xyz/v1",
    "ollama": "http://localhost:11434/v1",
}
PROVIDERS = ("gemini", "anthropic", "openai", *OPENAI_COMPATIBLE_PRESETS)


def _usage(prompt: int = 0, output: int = 0, thoughts: int = 0, total: int | None = None) -> dict[str, int]:
    """Normalised usage. `thoughts` is informational: Anthropic/OpenAI count reasoning inside
    output tokens; Gemini reports it separately but includes it in its own total."""
    prompt, output, thoughts = prompt or 0, output or 0, thoughts or 0
    return {"prompt": prompt, "output": output, "thoughts": thoughts, "total": total or prompt + output}


# --------------------------------------------------------------------------- shared retry
class _Base:
    """Rate spacing + bounded retry with exponential backoff, shared by all providers."""

    name: str

    def __init__(self, model: str, min_interval_s: float, max_retries: int,
                 on_retry: Callable[[str, int, float], None] | None, provider: str):
        self.name = model
        self.provider = provider
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.on_retry = on_retry
        self._last_call = 0.0

    def _classify(self, exc: Exception) -> tuple[bool, str]:  # (retryable, label)
        raise NotImplementedError

    def _call(self, fn: Callable[[], Any]) -> tuple[Any, int]:
        delay = 5.0
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval_s - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = start = time.time()
            try:
                return fn(), int((time.time() - start) * 1000)
            except Exception as exc:  # noqa: BLE001 - classified below
                retryable, label = self._classify(exc)
                if not retryable or attempt >= self.max_retries:
                    raise ModelError(f"{self.provider} {label}: {str(exc)[:300]}") from None
                if self.on_retry:
                    self.on_retry(f"{self.provider}:{label}", attempt + 1, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
        raise ModelError("retries exhausted")  # pragma: no cover


def _status_classify(exc: Exception) -> tuple[bool, str]:
    """Works for anthropic/openai SDK exceptions (status_code) and network errors."""
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(code, int):
        return (code in (408, 409, 429) or code >= 500), f"HTTP {code}"
    name = type(exc).__name__
    # Network-level failures from httpx/httpx2/requests/aiohttp (ConnectError, ConnectionError,
    # ReadTimeout, RemoteProtocolError, ...) are transient and safe to retry for model calls.
    if any(k in name for k in ("Connect", "Timeout", "ReadError", "WriteError", "RemoteProtocol", "Network")):
        return True, name
    return False, name


# --------------------------------------------------------------------------- Gemini (native)
class GeminiModel(_Base):
    def __init__(self, api_key: str, model: str, min_interval_s: float = 0.0, temperature: float | None = 0.0,
                 max_retries: int = 4, timeout_s: float = 120.0,
                 on_retry: Callable[[str, int, float], None] | None = None, base_url: str | None = None):
        super().__init__(model, min_interval_s, max_retries, on_retry, "gemini")
        from google import genai
        from google.genai import types

        self._types = types
        self.temperature = temperature
        opts: dict[str, Any] = {"timeout": int(timeout_s * 1000)}
        if base_url:
            opts["base_url"] = base_url
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(**opts))

    def _classify(self, exc: Exception) -> tuple[bool, str]:
        from google.genai import errors
        if isinstance(exc, errors.APIError):
            code = getattr(exc, "code", 0) or 0
            return (code == 429 or code >= 500), f"HTTP {code}"
        return _status_classify(exc)

    def start_chat(self, system: str, tools: dict[str, dict[str, Any]]) -> "GeminiChat":
        t = self._types
        decls = [t.FunctionDeclaration(name=n, description=s["description"], parameters_json_schema=s["parameters"])
                 for n, s in tools.items()]
        cfg: dict[str, Any] = dict(
            system_instruction=system,
            tools=[t.Tool(function_declarations=decls)],
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            tool_config=t.ToolConfig(function_calling_config=t.FunctionCallingConfig(mode="AUTO")),
        )
        if self.temperature is not None:
            cfg["temperature"] = self.temperature
        return GeminiChat(self, t.GenerateContentConfig(**cfg))


class GeminiChat:
    def __init__(self, model: GeminiModel, config: Any):
        self.m = model
        self.config = config
        self.contents: list[Any] = []

    def send_user(self, text: str) -> Turn:
        t = self.m._types
        if self.contents and self.contents[-1].role == "user":
            # No model turn since the last user turn (e.g. a malformed call): keep roles alternating.
            self.contents[-1].parts.append(t.Part(text=text))
        else:
            self.contents.append(t.Content(role="user", parts=[t.Part(text=text)]))
        return self._step()

    def send_tool_results(self, results: list[tuple[ToolCall, dict[str, Any]]], note: str | None = None) -> Turn:
        t = self.m._types
        parts = [t.Part(function_response=t.FunctionResponse(id=c.call_id, name=c.name, response=r))
                 for c, r in results]
        if note:
            parts.append(t.Part(text=note))
        self.contents.append(t.Content(role="user", parts=parts))
        return self._step()

    def _step(self) -> Turn:
        resp, latency = self.m._call(lambda: self.m._client.models.generate_content(
            model=self.m.name, contents=self.contents, config=self.config))
        um = resp.usage_metadata
        usage = _usage(um.prompt_token_count, um.candidates_token_count, um.thoughts_token_count,
                       um.total_token_count) if um else _usage()
        cand = resp.candidates[0] if resp.candidates else None
        finish = str(cand.finish_reason) if cand and cand.finish_reason else None
        if cand is None or cand.content is None or not cand.content.parts:
            if finish and "MALFORMED_FUNCTION_CALL" in finish:
                # Recoverable: the model tried to call a tool but produced invalid arguments.
                # Return an empty turn so the agent can tell it and let it retry (bounded by nudges).
                return Turn([], "", usage, latency, MALFORMED_CALL)
            raise ModelError(f"gemini returned no content (finish_reason={finish}, "
                             f"feedback={getattr(resp, 'prompt_feedback', None)})")
        self.contents.append(cand.content)  # keeps thought signatures for the next turn
        calls, texts = [], []
        for p in cand.content.parts:
            if p.function_call:
                calls.append(ToolCall(p.function_call.name, dict(p.function_call.args or {}), p.function_call.id))
            elif p.text and not p.thought:
                texts.append(p.text)
        return Turn(calls, "".join(texts), usage, latency, finish)


# --------------------------------------------------------------------------- Anthropic (native)
# Models that accept server-side refusal fallbacks (see Anthropic docs: refusals-and-fallback).
_ANTHROPIC_FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5",
                              "claude-sonnet-5-5"}


class AnthropicModel(_Base):
    def __init__(self, api_key: str | None, model: str, min_interval_s: float = 0.0, max_retries: int = 4,
                 timeout_s: float = 300.0, on_retry: Callable[[str, int, float], None] | None = None,
                 base_url: str | None = None, effort: str | None = None, max_tokens: int = 16000,
                 fallbacks: bool = True):
        super().__init__(model, min_interval_s, max_retries, on_retry, "anthropic")
        import anthropic

        # SDK retries off: our shared loop retries so every attempt is traced.
        self._client = anthropic.Anthropic(api_key=api_key or None, base_url=base_url or None,
                                           timeout=timeout_s, max_retries=0)
        self.effort = effort
        self.max_tokens = max_tokens
        # Server-side refusal fallback: only on the first-party API and supporting models.
        self.fallbacks = fallbacks and not base_url and model in _ANTHROPIC_FALLBACK_MODELS

    def _classify(self, exc: Exception) -> tuple[bool, str]:
        return _status_classify(exc)

    def start_chat(self, system: str, tools: dict[str, dict[str, Any]]) -> "AnthropicChat":
        return AnthropicChat(self, system, [{"name": n, "description": s["description"],
                                             "input_schema": s["parameters"]} for n, s in tools.items()])

    def create(self, **kw: Any) -> Any:
        if self.effort:
            kw["output_config"] = {"effort": self.effort}
        if self.fallbacks:
            return self._client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                     fallbacks="default", **kw)
        return self._client.messages.create(**kw)


class AnthropicChat:
    def __init__(self, model: AnthropicModel, system: str, tools: list[dict[str, Any]]):
        self.m = model
        self.system = system
        self.tools = tools
        self.messages: list[dict[str, Any]] = []

    def send_user(self, text: str) -> Turn:
        self.messages.append({"role": "user", "content": text})
        return self._step()

    def send_tool_results(self, results: list[tuple[ToolCall, dict[str, Any]]], note: str | None = None) -> Turn:
        # All tool results for one assistant turn go back in a single user message.
        content: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": c.call_id, "is_error": "error" in r,
             "content": json.dumps(r, ensure_ascii=False, default=str)} for c, r in results]
        if note:
            content.append({"type": "text", "text": note})
        self.messages.append({"role": "user", "content": content})
        return self._step()

    def _step(self) -> Turn:
        resp, latency = self.m._call(lambda: self.m.create(
            model=self.m.name, max_tokens=self.m.max_tokens, system=self.system, tools=self.tools,
            tool_choice={"type": "auto"}, messages=self.messages, cache_control={"type": "ephemeral"}))
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise ModelError(f"anthropic refusal (category={getattr(details, 'category', None)})")
        # Append the full content (thinking, tool_use, fallback blocks) unchanged.
        self.messages.append({"role": "assistant", "content": resp.content})
        u = resp.usage
        prompt = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) + \
                 (getattr(u, "cache_creation_input_tokens", 0) or 0)
        calls, texts = [], []
        for b in resp.content:
            if b.type == "tool_use":
                calls.append(ToolCall(b.name, dict(b.input or {}), b.id))
            elif b.type == "text":
                texts.append(b.text)
        return Turn(calls, "".join(texts), _usage(prompt, u.output_tokens), latency, resp.stop_reason)


# --------------------------------------------------------------------------- OpenAI-compatible
class OpenAICompatibleModel(_Base):
    def __init__(self, api_key: str | None, model: str, base_url: str | None = None,
                 min_interval_s: float = 0.0, temperature: float | None = None, max_tokens: int | None = None,
                 max_retries: int = 4, timeout_s: float = 300.0,
                 on_retry: Callable[[str, int, float], None] | None = None, provider: str = "openai"):
        super().__init__(model, min_interval_s, max_retries, on_retry, provider)
        import openai

        # Local servers (Ollama, vLLM, LM Studio) accept any key.
        self._client = openai.OpenAI(api_key=api_key or "not-needed", base_url=base_url or None,
                                     timeout=timeout_s, max_retries=0)
        self.temperature = temperature
        self.max_tokens = max_tokens

    def _classify(self, exc: Exception) -> tuple[bool, str]:
        return _status_classify(exc)

    def start_chat(self, system: str, tools: dict[str, dict[str, Any]]) -> "OpenAIChat":
        return OpenAIChat(self, system, [{"type": "function", "function": {
            "name": n, "description": s["description"], "parameters": s["parameters"]}} for n, s in tools.items()])


class OpenAIChat:
    def __init__(self, model: OpenAICompatibleModel, system: str, tools: list[dict[str, Any]]):
        self.m = model
        self.tools = tools
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        self._n = 0

    def send_user(self, text: str) -> Turn:
        self.messages.append({"role": "user", "content": text})
        return self._step()

    def send_tool_results(self, results: list[tuple[ToolCall, dict[str, Any]]], note: str | None = None) -> Turn:
        for c, r in results:
            self.messages.append({"role": "tool", "tool_call_id": c.call_id,
                                  "content": json.dumps(r, ensure_ascii=False, default=str)})
        if note:
            self.messages.append({"role": "user", "content": note})
        return self._step()

    def _step(self) -> Turn:
        kw: dict[str, Any] = {"model": self.m.name, "messages": self.messages, "tools": self.tools,
                              "tool_choice": "auto"}
        if self.m.temperature is not None:
            kw["temperature"] = self.m.temperature
        if self.m.max_tokens:
            kw["max_tokens"] = self.m.max_tokens
        resp, latency = self.m._call(lambda: self.m._client.chat.completions.create(**kw))
        if not resp.choices:
            raise ModelError(f"{self.m.provider} returned no choices")
        choice = resp.choices[0]
        msg = choice.message
        calls, raw_calls = [], []
        for tc in msg.tool_calls or []:
            self._n += 1
            cid = tc.id or f"call_{self._n}"
            raw = tc.function.arguments or "{}"
            try:
                args = json.loads(raw)
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
            except ValueError:
                args = {"__invalid_arguments__": raw[:200]}  # tool layer reports this as a bad-arguments error
            calls.append(ToolCall(tc.function.name, args, cid))
            raw_calls.append({"id": cid, "type": "function",
                              "function": {"name": tc.function.name, "arguments": raw}})
        assistant: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if raw_calls:
            assistant["tool_calls"] = raw_calls
        self.messages.append(assistant)
        u = resp.usage
        reasoning = 0
        if u is not None and getattr(u, "completion_tokens_details", None) is not None:
            reasoning = getattr(u.completion_tokens_details, "reasoning_tokens", 0) or 0
        usage = _usage(u.prompt_tokens, u.completion_tokens, reasoning, u.total_tokens) if u else _usage()
        return Turn(calls, msg.content or "", usage, latency, choice.finish_reason)


# --------------------------------------------------------------------------- factory
def build_model(cfg: Any, on_retry: Callable[[str, int, float], None] | None = None) -> Model:
    """Create the adapter described by a `tyn.config.ModelConfig`."""
    p = cfg.provider
    if p == "gemini":
        return GeminiModel(cfg.api_key, cfg.model, cfg.min_interval_s,
                           0.0 if cfg.temperature is None else cfg.temperature,
                           on_retry=on_retry, base_url=cfg.base_url)
    if p == "anthropic":
        return AnthropicModel(cfg.api_key, cfg.model, cfg.min_interval_s, on_retry=on_retry,
                              base_url=cfg.base_url, effort=cfg.effort, max_tokens=cfg.max_tokens or 16000,
                              fallbacks=cfg.anthropic_fallbacks)
    if p == "openai" or p in OPENAI_COMPATIBLE_PRESETS:
        return OpenAICompatibleModel(cfg.api_key, cfg.model, cfg.base_url or OPENAI_COMPATIBLE_PRESETS.get(p),
                                     cfg.min_interval_s, cfg.temperature, cfg.max_tokens, on_retry=on_retry,
                                     provider=p)
    raise ValueError(f"Unknown LLM_PROVIDER '{p}'. Choose one of {PROVIDERS}")
