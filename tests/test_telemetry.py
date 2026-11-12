"""OpenTelemetry bridge, tested offline with the in-memory span exporter."""
from datetime import datetime, timezone

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import tyn.telemetry as tel
from tyn.agent import Agent, Tracer
from tyn.telemetry import Telemetry
from tyn.tools import Toolbox

from .conftest import PROJECT, ExplorerPolicy, ScriptedModel, honest_decider

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
LABEL = "tyn-release-assessment-v2"
SECRET = "sekret-token-123"


def run_with_telemetry(jira, labels, decisions, capture=False):
    exp = InMemorySpanExporter()
    t = Telemetry(exp, capture_content=capture, batch=False)
    t.common["session.id"] = t.common["langfuse.session.id"] = "R2.4-test"
    tracer = Tracer(None, [SECRET], sink=t)
    tb = Toolbox(jira, PROJECT, LABEL, "R2.4", now=NOW)
    model = ScriptedModel(lambda: ExplorerPolicy(honest_decider(labels, decisions)))
    run = Agent(tb, model, tracer, only=sorted(decisions)).run()
    t.shutdown()
    return run, exp.get_finished_spans(), tracer


@pytest.fixture()
def spans(jira, labels):
    # REQ-09 gets a wrong PASS first so a guardrail rejection is traced
    _, spans, _ = run_with_telemetry(jira, labels, {"REQ-08": "FAIL", "REQ-09": "PASS"})
    return spans


def test_span_tree(spans):
    roots = [s for s in spans if s.parent is None]
    assert len(roots) == 1 and roots[0].name == "release-assessment"
    root = roots[0]
    assert root.attributes["langfuse.trace.name"] == "release-assessment R2.4"
    assert root.attributes["tyn.status"] == "PARTIAL"
    reqs = {s.name: s for s in spans if s.attributes.get("tyn.requirement.id")}
    assert set(reqs) == {"REQ-08", "REQ-09"}
    assert all(s.parent.span_id == root.context.span_id for s in reqs.values())
    assert reqs["REQ-08"].attributes["tyn.decision"] == "FAIL"
    assert reqs["REQ-09"].attributes["tyn.decision"] == "INSUFFICIENT_EVIDENCE"


def test_generation_tool_and_guardrail_spans(spans):
    by_type = {}
    for s in spans:
        by_type.setdefault(s.attributes.get("langfuse.observation.type"), []).append(s)
    gens = by_type["generation"]
    assert gens and all(g.attributes["gen_ai.usage.input_tokens"] == 100 for g in gens)
    tools = by_type["tool"]
    assert {"get_issue", "get_comments", "record_decision"} <= {t.name for t in tools}
    req09 = next(s for s in spans if s.name == "REQ-09")
    guard = [g for g in by_type["guardrail"] if g.parent.span_id == req09.context.span_id]
    assert guard and guard[0].attributes["langfuse.observation.level"] == "WARNING"
    assert "PASS is not allowed" in guard[0].attributes["langfuse.observation.status_message"]


def test_token_usage_only_on_generation_spans(spans):
    """Langfuse sums gen_ai.usage.* across a trace, so totals must not be repeated on parent spans."""
    usage_types = {s.attributes.get("langfuse.observation.type") for s in spans
                   if "gen_ai.usage.input_tokens" in s.attributes or "gen_ai.usage.output_tokens" in s.attributes}
    assert usage_types == {"generation"}
    gens = [s for s in spans if s.attributes.get("langfuse.observation.type") == "generation"]
    root = next(s for s in spans if s.parent is None)
    assert root.attributes["tyn.tokens.input"] == sum(g.attributes["gen_ai.usage.input_tokens"] for g in gens)
    req08 = next(s for s in spans if s.name == "REQ-08")
    assert req08.attributes["tyn.tokens.input"] > 0


def test_session_on_every_span_and_no_content_by_default(spans):
    assert all(s.attributes.get("langfuse.session.id") == "R2.4-test" for s in spans)
    content_keys = {"gen_ai.tool.call.arguments", "gen_ai.tool.call.result", "langfuse.observation.input"}
    assert not any(content_keys & set(s.attributes) for s in spans)
    gen_outputs = [s.attributes.get("langfuse.observation.output") for s in spans
                   if s.attributes.get("langfuse.observation.type") == "generation"]
    assert not any(gen_outputs)


def test_content_capture_is_opt_in_and_redacted(jira, labels):
    _, spans, tracer = run_with_telemetry(jira, labels, {"REQ-08": "FAIL"}, capture=True)
    tool_spans = [s for s in spans if s.attributes.get("langfuse.observation.type") == "tool"]
    assert any("REC-9635" in s.attributes.get("gen_ai.tool.call.arguments", "") or
               "HND-77" in s.attributes.get("gen_ai.tool.call.result", "") for s in tool_spans)
    # Langfuse shows Input/Output from langfuse.observation.*: set on tool and generation spans
    assert all("langfuse.observation.input" in s.attributes and "langfuse.observation.output" in s.attributes
               for s in tool_spans)
    gens = [s for s in spans if s.attributes.get("langfuse.observation.type") == "generation"]
    assert gens and all("langfuse.observation.input" in g.attributes for g in gens)
    inputs = " ".join(g.attributes["langfuse.observation.input"] for g in gens)
    assert '"user"' in inputs and '"tool_results"' in inputs  # first turn = user message, later = tool results
    # anything sent goes through the Tracer's redaction first
    exp = InMemorySpanExporter()
    t = Telemetry(exp, capture_content=True, batch=False)
    tr = Tracer(None, [SECRET], sink=t)
    tr.emit("run_start", release="R2.4", provider="x", model="m", project="P", label="L", budgets={})
    tr.emit("tool_call", req=None, tool="search_issues", args={"text": SECRET}, result=f"token={SECRET}",
            latency_ms=5, cache_hit=False, error=None, phase="investigate", step=1)
    t.shutdown()
    blob = str([dict(s.attributes) for s in exp.get_finished_spans()])
    assert SECRET not in blob and "[REDACTED]" in blob


def test_broken_sink_never_breaks_the_run(jira, labels):
    class Broken:
        def on_event(self, event, rec):
            raise RuntimeError("exporter down")
    tb = Toolbox(jira, PROJECT, LABEL, "R2.4", now=NOW)
    model = ScriptedModel(lambda: ExplorerPolicy(honest_decider(labels, {"REQ-08": "FAIL"})))
    run = Agent(tb, model, Tracer(None, sink=Broken()), only=["REQ-08"]).run()
    assert run.requirements[0].decision == "FAIL"


def test_handler_errors_are_contained():
    exp = InMemorySpanExporter()
    t = Telemetry(exp, batch=False)
    t.on_event("decision", {})  # malformed record: handler raises internally, must not propagate
    t.on_event("unknown_event", {"x": 1})
    t.shutdown()


# ------------------------------------------------------------------ configuration
def test_from_env(monkeypatch):
    for v in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST", "OTEL_EXPORTER_OTLP_ENDPOINT",
              "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "TYN_TELEMETRY", "TYN_TELEMETRY_CAPTURE_CONTENT"):
        monkeypatch.delenv(v, raising=False)
    assert Telemetry.from_env("run") is None  # not configured -> off
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-1")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-2")
    monkeypatch.setenv("LANGFUSE_HOST", "https://us.cloud.langfuse.com/")
    captured = {}

    class FakeExporter(InMemorySpanExporter):
        def __init__(self, endpoint=None, headers=None, timeout=None):
            super().__init__()
            captured.update(endpoint=endpoint, headers=headers)
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as otlp
    monkeypatch.setattr(otlp, "OTLPSpanExporter", FakeExporter)
    t = Telemetry.from_env("R2.4-x")
    assert t is not None and not t.capture and t.common["langfuse.session.id"] == "R2.4-x"
    assert captured["endpoint"] == "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
    assert captured["headers"]["Authorization"] == "Basic cGstbGYtMTpzay1sZi0y"  # base64("pk-lf-1:sk-lf-2")
    assert captured["headers"]["x-langfuse-ingestion-version"] == "4"
    t.shutdown()
    monkeypatch.setenv("TYN_TELEMETRY", "off")
    assert Telemetry.from_env("run") is None
