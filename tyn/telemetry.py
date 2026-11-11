"""Optional OpenTelemetry monitoring (vendor-neutral; Langfuse-ready).

Every agent event already flows through `Tracer.emit()`. This module turns those
events into an OpenTelemetry span tree and exports it over OTLP/HTTP:

  release-assessment (agent)                      one trace per run
  ├─ llm call (generation)  ×N                   discovery model turns
  ├─ get_manifest / list_requirements (tool)
  └─ REQ-xx (agent)                               one span per requirement
     ├─ llm call (generation)                     gen_ai.* model + token usage
     ├─ get_issue / get_comments / ... (tool)     latency, cache hit, errors
     └─ guardrail rejection (guardrail)           WARNING level, problems

Design rules:
* Off unless configured (Langfuse keys or a generic OTLP endpoint). TYN_TELEMETRY=off disables.
* Metadata only by default (ids, decisions, counts, timings, token usage). Ticket text, tool
  arguments/results and model text are sent only with TYN_TELEMETRY_CAPTURE_CONTENT=true, and
  always after the Tracer's secret redaction.
* Monitoring never breaks a run: every handler is wrapped; exporter failures are logged once.
* Uses its own TracerProvider (no global side effects).
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Span, Status, StatusCode, set_span_in_context

_MAX_ATTR = 4000  # keep attribute payloads bounded


def _json(v: Any) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= _MAX_ATTR else s[: _MAX_ATTR - 12] + "…[trimmed]"


class Telemetry:
    def __init__(self, exporter: SpanExporter, capture_content: bool = False, batch: bool = True,
                 service_name: str = "verdict-agent", environment: str | None = None):
        self.capture = capture_content
        self.provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        self.provider.add_span_processor(BatchSpanProcessor(exporter) if batch else SimpleSpanProcessor(exporter))
        self.tracer = self.provider.get_tracer("tyn.agent")
        self.environment = environment
        self.root: Span | None = None
        self.req_spans: dict[str, Span] = {}
        self.common: dict[str, Any] = {}
        self._warned = False

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_env(cls, run_id: str, capture_override: bool | None = None) -> "Telemetry | None":
        """Langfuse (LANGFUSE_PUBLIC_KEY/SECRET_KEY[/HOST]) or any OTLP backend
        (OTEL_EXPORTER_OTLP_ENDPOINT[/HEADERS]); None when not configured or disabled."""
        if os.environ.get("TYN_TELEMETRY", "").strip().lower() in ("off", "0", "false", "no"):
            return None
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        pk, sk = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip(), os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
        if pk and sk:
            host = (os.environ.get("LANGFUSE_HOST") or "https://cloud.langfuse.com").strip().rstrip("/")
            auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
            exporter = OTLPSpanExporter(endpoint=f"{host}/api/public/otel/v1/traces", timeout=10,
                                        headers={"Authorization": f"Basic {auth}",
                                                 "x-langfuse-ingestion-version": "4"})
        elif os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
            exporter = OTLPSpanExporter(timeout=10)  # reads the standard OTEL_EXPORTER_OTLP_* variables
        else:
            return None
        capture = capture_override if capture_override is not None else \
            os.environ.get("TYN_TELEMETRY_CAPTURE_CONTENT", "").strip().lower() in ("1", "true", "yes")
        t = cls(exporter, capture_content=capture, environment=os.environ.get("TYN_ENVIRONMENT") or None)
        t.common["session.id"] = t.common["langfuse.session.id"] = run_id
        return t

    # ------------------------------------------------------------------ public API
    def on_event(self, event: str, rec: dict[str, Any]) -> None:
        """Called by Tracer.emit with the *redacted* event record. Never raises."""
        try:
            handler = getattr(self, f"_on_{event}", None)
            if handler:
                handler(rec)
        except Exception as exc:  # noqa: BLE001 - monitoring must never break the run
            if not self._warned:
                self._warned = True
                print(f"  telemetry disabled for this event type after error: {type(exc).__name__}: {exc}",
                      file=sys.stderr)

    def shutdown(self) -> None:
        try:
            for span in list(self.req_spans.values()):
                span.end()
            if self.root is not None and self.root.is_recording():
                self.root.end()
            self.provider.force_flush(timeout_millis=10_000)
            self.provider.shutdown()
        except Exception as exc:  # noqa: BLE001
            print(f"  telemetry flush failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------ helpers
    def _attrs(self, extra: dict[str, Any]) -> dict[str, Any]:
        out = dict(self.common)
        for k, v in extra.items():
            if v is None:
                continue
            if isinstance(v, (str, bool, int, float)):
                out[k] = v
            elif isinstance(v, (list, tuple)) and all(isinstance(x, str) for x in v):
                out[k] = [x[:500] for x in v]  # OTel supports homogeneous string arrays (e.g. tags)
            else:
                out[k] = _json(v)
        return out

    def _parent(self, req: str | None) -> Span | None:
        return self.req_spans.get(req) if req else self.root

    def _child(self, name: str, req: str | None, attrs: dict[str, Any], start_ns: int | None = None) -> Span:
        parent = self._parent(req)
        ctx = set_span_in_context(parent) if parent is not None else None
        return self.tracer.start_span(name, context=ctx, attributes=self._attrs(attrs), start_time=start_ns)

    # ------------------------------------------------------------------ event handlers
    def _on_run_start(self, r: dict[str, Any]) -> None:
        tags = [t for t in (r.get("release"), r.get("provider"), r.get("model")) if t]
        self.common["langfuse.trace.tags"] = tags
        if self.environment:
            self.common["langfuse.environment"] = self.environment
        self.root = self.tracer.start_span("release-assessment", attributes=self._attrs({
            "langfuse.trace.name": f"release-assessment {r.get('release')}",
            "langfuse.observation.type": "agent",
            "langfuse.trace.metadata.project": r.get("project"),
            "langfuse.trace.metadata.dataset_label": r.get("label"),
            "langfuse.trace.metadata.release": r.get("release"),
            "gen_ai.system": r.get("provider"), "gen_ai.request.model": r.get("model"),
            "tyn.budgets": r.get("budgets"),
        }))

    def _on_requirement_start(self, r: dict[str, Any]) -> None:
        req = r["req"]
        attrs = {"langfuse.observation.type": "agent", "tyn.requirement.id": req, "tyn.requirement.key": r.get("key")}
        if self.capture:
            attrs["langfuse.observation.input"] = r.get("criterion")
        self.req_spans[req] = self._child(req, None, attrs)

    def _on_model_turn(self, r: dict[str, Any]) -> None:
        now = time.time_ns()
        u = r.get("usage") or {}
        attrs = {
            "langfuse.observation.type": "generation",
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": self.root.attributes.get("gen_ai.request.model") if self.root else None,
            "gen_ai.system": self.root.attributes.get("gen_ai.system") if self.root else None,
            "gen_ai.usage.input_tokens": int(u.get("prompt", 0)),
            "gen_ai.usage.output_tokens": int(u.get("output", 0)),
            "gen_ai.usage.reasoning_tokens": int(u.get("thoughts", 0)),
            "gen_ai.response.finish_reasons": [str(r.get("finish_reason") or "")],
            "tyn.phase": r.get("phase"), "tyn.step": r.get("step"),
            "tyn.tool_calls_requested": [c.get("name") for c in r.get("tool_calls") or []],
        }
        if self.capture:
            attrs["langfuse.observation.output"] = {"text": r.get("text"), "tool_calls": r.get("tool_calls")}
        span = self._child("llm call", r.get("req"), attrs, start_ns=now - int(r.get("latency_ms", 0)) * 1_000_000)
        span.end(end_time=now)

    def _on_tool_call(self, r: dict[str, Any]) -> None:
        now = time.time_ns()
        attrs = {"langfuse.observation.type": "tool", "gen_ai.tool.name": r.get("tool"),
                 "tyn.phase": r.get("phase"), "tyn.step": r.get("step"), "tyn.cache_hit": bool(r.get("cache_hit")),
                 "tyn.tool.arg_names": sorted((r.get("args") or {}).keys())}
        if self.capture:
            attrs["gen_ai.tool.call.arguments"] = r.get("args")
            attrs["gen_ai.tool.call.result"] = r.get("result")
        span = self._child(r.get("tool") or "tool", r.get("req"), attrs,
                           start_ns=now - int(r.get("latency_ms", 0)) * 1_000_000)
        if r.get("error"):
            span.set_attribute("langfuse.observation.level", "ERROR")
            span.set_status(Status(StatusCode.ERROR, str(r["error"])[:500]))
        span.end(end_time=now)

    def _on_guardrail_rejection(self, r: dict[str, Any]) -> None:
        span = self._child("guardrail rejection", r.get("req"), {
            "langfuse.observation.type": "guardrail", "langfuse.observation.level": "WARNING",
            "langfuse.observation.status_message": "; ".join(r.get("problems") or [])[:1000],
            "tyn.attempted_decision": r.get("attempted")})
        span.end()

    def _on_retry(self, r: dict[str, Any]) -> None:
        target = self.root
        if target is not None:
            target.add_event("retry", {"target": str(r.get("target")), "attempt": int(r.get("attempt", 0)),
                                       "wait_s": float(r.get("wait_s", 0))})

    def _on_malformed_call(self, r: dict[str, Any]) -> None:
        span = self._parent(r.get("req"))
        if span is not None:
            span.add_event("malformed tool call", {"step": int(r.get("step") or 0)})

    def _on_failure(self, r: dict[str, Any]) -> None:
        span = self._parent(r.get("req"))
        if span is not None:
            span.add_event("failure", {"error": str(r.get("error"))[:1000]})
            span.set_attribute("langfuse.observation.level", "ERROR")
            span.set_attribute("langfuse.observation.status_message", str(r.get("error"))[:1000])

    def _on_decision(self, r: dict[str, Any]) -> None:
        span = self.req_spans.pop(r["req"], None)
        if span is None:
            return
        tokens = r.get("tokens") or {}
        span.set_attributes(self._attrs({
            "tyn.decision": r.get("decision"), "tyn.agent_failure": r.get("agent_failure"),
            "tyn.tool_calls": r.get("tool_calls"), "tyn.evidence_count": len(r.get("evidence_refs") or []),
            "gen_ai.usage.input_tokens": int(tokens.get("prompt", 0)),
            "gen_ai.usage.output_tokens": int(tokens.get("output", 0)),
            "langfuse.observation.output": r.get("decision"),
        }))
        if r.get("agent_failure"):
            span.set_status(Status(StatusCode.ERROR, str(r["agent_failure"])[:500]))
        span.end()

    def _on_run_end(self, r: dict[str, Any]) -> None:
        if self.root is None:
            return
        m = r.get("metrics") or {}
        tok = m.get("tokens") or {}
        self.root.set_attributes(self._attrs({
            "tyn.status": r.get("status"), "tyn.verdict": r.get("verdict"),
            "langfuse.observation.output": r.get("verdict") or r.get("status"),
            "tyn.wall_time_s": m.get("wall_time_s"), "tyn.model_calls": m.get("model_calls"),
            "tyn.tool_calls": m.get("tool_calls"), "tyn.tool_errors": m.get("tool_errors"),
            "gen_ai.usage.input_tokens": int(tok.get("prompt", 0)),
            "gen_ai.usage.output_tokens": int(tok.get("output", 0)),
            "tyn.warnings": r.get("warnings"),
        }))
        if r.get("status") not in ("COMPLETE", "PARTIAL"):
            self.root.set_status(Status(StatusCode.ERROR, str(r.get("status"))))
        self.root.end()
