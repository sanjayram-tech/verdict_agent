import json

from tyn.agent import Agent, Budgets, Tracer, compute_verdict, redact
from tyn.model import ModelError, ToolCall
from tyn.report import render_markdown
from tyn.tools import Toolbox

from .conftest import PROJECT, ExplorerPolicy, ScriptedModel, turn

LABEL = "tyn-release-assessment-v2"


def oracle(expected):
    def decide(req, comments, scopes):
        d = expected["decisions"][req]["decision"]
        full = [r for r, s in scopes.items() if s["full_match"]]
        if d == "PASS":
            refs = [r for r in full if comments[r]["role"] in ("QA Lead", "Security Owner")]
        elif d == "FAIL":
            refs = full
        else:
            refs = list(comments)
        return d, refs, [] if d == "PASS" else ["follow up"]
    return decide


def run_agent(jira, model, budgets=None, tmp_path=None):
    tb = Toolbox(jira, PROJECT, LABEL, "R2.4")
    tracer = Tracer(tmp_path / "trace.jsonl" if tmp_path else None, ["sekret-token-123"])
    return Agent(tb, model, tracer, budgets).run(), tb, tracer


def test_verdict_rules():
    assert compute_verdict(["PASS", "FAIL", "INSUFFICIENT_EVIDENCE"]) == "BLOCKED"
    assert compute_verdict(["PASS", "INSUFFICIENT_EVIDENCE"]) == "NEEDS_REVIEW"
    assert compute_verdict(["PASS", "PASS"]) == "READY"
    assert compute_verdict([]) == "NEEDS_REVIEW"


def test_end_to_end_correct_decisions_pass_guardrails(jira, expected, tmp_path):
    """The guardrails must accept the correct answers (no false rejections) and the
    explorer must reach multi-hop evidence such as REQ-08 -> REC-4076 -> REC-9635."""
    run, tb, tracer = run_agent(jira, ScriptedModel(lambda: ExplorerPolicy(oracle(expected))), tmp_path=tmp_path)
    got = {r.id: r.decision for r in run.requirements}
    assert got == {k: v["decision"] for k, v in expected["decisions"].items()}
    assert all(not r.guardrail_rejections and not r.agent_failure for r in run.requirements)
    assert run.status == "COMPLETE" and run.verdict == "BLOCKED"
    req08 = next(r for r in run.requirements if r.id == "REQ-08")
    assert any(tb.comment_index[ref]["source_id"] == "REC-9635" for ref in req08.evidence_refs)
    assert [s["source_id"] for s in run.suspicious] == ["REC-5716"]
    md = render_markdown(run, tb.comment_index, PROJECT, LABEL, "https://example.atlassian.net")
    assert "Overall decision:** **BLOCKED" in md and "Suspicious content" in md and "Priya Nair (QA Lead)" in md
    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(l)["event"] == "tool_call" for l in lines)


def test_model_wrongly_passing_is_forced_to_insufficient(jira, expected):
    def always_pass(req, comments, scopes):
        return "PASS", list(comments), []
    run, _, _ = run_agent(jira, ScriptedModel(lambda: ExplorerPolicy(always_pass)))
    got = {r.id: r for r in run.requirements}
    # Developer-only / wrong-scope evidence cannot produce PASS
    for rid in ("REQ-06", "REQ-11", "REQ-03"):
        assert got[rid].decision != "PASS" and got[rid].guardrail_rejections
    assert run.verdict != "READY"


class Exploding:
    def on_user(self, text):
        raise ModelError("Gemini API error 503: overloaded")


def test_model_failure_is_fail_safe_and_run_continues(jira):
    run, _, _ = run_agent(jira, ScriptedModel(Exploding))
    assert len(run.requirements) == 12 and run.status == "COMPLETE"
    assert all(r.decision == "INSUFFICIENT_EVIDENCE" and "503" in r.agent_failure for r in run.requirements)
    assert run.verdict == "NEEDS_REVIEW"


class Looping:
    def on_user(self, text):
        return turn(ToolCall("get_issue", {"ref": "REQ-01"}))

    def on_results(self, results, note):
        return turn(ToolCall("get_issue", {"ref": "REQ-01"}))


def test_loop_detector(jira):
    run, _, _ = run_agent(jira, ScriptedModel(Looping))
    assert all("loop detected" in r.agent_failure for r in run.requirements)
    assert run.verdict != "READY"


class Wandering:
    def __init__(self):
        self.i = 0

    def on_user(self, text):
        return self.on_results([], None)

    def on_results(self, results, note):
        self.i += 1
        return turn(ToolCall("search_issues", {"text": f"probe {self.i}"}))


def test_tool_budget_exhaustion(jira):
    run, _, _ = run_agent(jira, ScriptedModel(Wandering), Budgets(max_tool_calls_per_req=4))
    r = run.requirements[0]
    assert r.decision == "INSUFFICIENT_EVIDENCE" and "budget" in r.agent_failure
    assert r.tool_calls <= 5


def test_discovery_failure_falls_back(jira, expected):
    class BrokenDiscovery:
        def on_user(self, text):
            raise ModelError("boom")
    model = ScriptedModel(lambda: ExplorerPolicy(oracle(expected)), discovery=BrokenDiscovery)
    run, _, _ = run_agent(jira, model)
    assert len(run.requirements) == 12 and any("Discovery model failure" in w for w in run.warnings)


def test_trace_redacts_secrets(tmp_path):
    tr = Tracer(tmp_path / "t.jsonl", ["sekret-token-123"])
    tr.emit("x", note="token=abc123 and sekret-token-123 and AIzaSyA1234567890123456789012345")
    body = (tmp_path / "t.jsonl").read_text(encoding="utf-8")
    assert "sekret" not in body and "AIza" not in body and "abc123" not in body
    assert redact("Authorization: Basic dXNlcjp0b2tlbjEyMzQ1Njc4", []).count("[REDACTED]") == 1
