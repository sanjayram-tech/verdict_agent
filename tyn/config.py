"""Configuration loaded from environment variables (optionally a local .env file).

Credentials are never accepted as command-line arguments so they do not end up
in shell history, traces or reports.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


def _get(name: str, default: str | None = None, required: bool = True) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(f"Missing required environment variable {name} (see .env.example)")
    return value or ""


@dataclass(frozen=True)
class JiraConfig:
    base_url: str
    email: str
    api_token: str
    issue_type: str = "Task"
    link_type: str = "Relates"

    @classmethod
    def from_env(cls) -> "JiraConfig":
        load_dotenv()
        return cls(
            base_url=_get("JIRA_BASE_URL").rstrip("/"),
            email=_get("JIRA_EMAIL"),
            api_token=_get("JIRA_API_TOKEN"),
            issue_type=_get("JIRA_ISSUE_TYPE", "Task"),
            link_type=_get("JIRA_LINK_TYPE", "Relates"),
        )


DEFAULT_MODELS = {"gemini": "gemini-2.5-flash", "anthropic": "claude-opus-5-5"}
PROVIDER_KEY_VARS = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
                     "openrouter": "OPENROUTER_API_KEY", "groq": "GROQ_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
                     "mistral": "MISTRAL_API_KEY", "together": "TOGETHER_API_KEY"}
KEYLESS_PROVIDERS = {"ollama"}  # local servers


def _opt_float(name: str) -> float | None:
    v = os.environ.get(name, "").strip()
    return float(v) if v else None


@dataclass(frozen=True)
class ModelConfig:
    """Provider-agnostic model settings (see .env.example and tyn/model.py)."""
    provider: str
    model: str
    api_key: str | None
    base_url: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    effort: str | None = None           # Anthropic output_config.effort
    anthropic_fallbacks: bool = True    # Anthropic server-side refusal fallbacks
    min_interval_s: float = 0.0         # spacing between model calls (rate limits)

    @classmethod
    def from_env(cls, provider: str | None = None, model: str | None = None) -> "ModelConfig":
        from .model import PROVIDERS

        load_dotenv()
        provider = (provider or os.environ.get("LLM_PROVIDER") or "gemini").strip().lower()
        if provider not in PROVIDERS:
            raise ConfigError(f"LLM_PROVIDER '{provider}' is not supported; choose one of {', '.join(PROVIDERS)}")
        model = model or os.environ.get("LLM_MODEL") or (os.environ.get("GEMINI_MODEL") if provider == "gemini" else None) \
            or DEFAULT_MODELS.get(provider)
        if not model:
            raise ConfigError(f"Set LLM_MODEL (or --model) for provider '{provider}'")
        key_var = PROVIDER_KEY_VARS.get(provider)
        api_key = os.environ.get("LLM_API_KEY") or (os.environ.get(key_var) if key_var else None)
        if not api_key and provider not in KEYLESS_PROVIDERS and provider != "anthropic" \
                and not (provider == "openai" and os.environ.get("LLM_BASE_URL")):
            raise ConfigError(f"Missing API key: set LLM_API_KEY (or {key_var}) for provider '{provider}'")
        max_tokens = os.environ.get("LLM_MAX_TOKENS", "").strip()
        interval = os.environ.get("LLM_MIN_INTERVAL_S") or os.environ.get("GEMINI_MIN_INTERVAL_S") or "0"
        return cls(
            provider=provider,
            model=model,
            api_key=api_key,  # anthropic: None lets the SDK use ANTHROPIC_AUTH_TOKEN / `ant auth login`
            base_url=(os.environ.get("LLM_BASE_URL") or "").strip() or None,
            temperature=_opt_float("LLM_TEMPERATURE"),
            max_tokens=int(max_tokens) if max_tokens else None,
            effort=(os.environ.get("LLM_EFFORT") or "").strip() or None,
            anthropic_fallbacks=os.environ.get("ANTHROPIC_FALLBACKS", "1").strip() not in ("0", "false", "no"),
            min_interval_s=float(interval),
        )


def secret_values() -> list[str]:
    """Values that must never appear in traces/reports (used by the redactor)."""
    load_dotenv()
    out = []
    for name in ("JIRA_API_TOKEN", "JIRA_EMAIL", "LLM_API_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY",
                 *PROVIDER_KEY_VARS.values()):
        v = os.environ.get(name)
        if v and len(v) >= 6:
            out.append(v)
    return out
