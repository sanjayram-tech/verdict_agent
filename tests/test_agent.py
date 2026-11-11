import json
from datetime import datetime, timezone

from tyn.agent import Agent, Budgets, Tracer, compute_verdict, redact
from tyn.model import ModelError, ToolCall
from tyn.report import render_markdown
from tyn.tools import Toolbox

from .conftest import PROJECT, ExplorerPolicy, ScriptedModel, honest_decider, turn

LABEL = "tyn-release-assessment-v2"
NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


def run_agent(jira, model, budgets=None, tmp_path=None, only=None):
    tb = Toolbox(jira, PROJECT, LABEL, "R2.4", now=NOW)
    tracer = Tracer(tmp_path / "trace.jsonl" if tmp_path else None, ["sekret-token-123"])
    return Agent(tb, model, tracer, budgets, only=only).run(), tb, tracer


def decisions_of(expected):
    return {k: v["decision"] for k, v in expected["decisions"].items()}


def test_verdict_rules():
    assert compute_verdict(["PASS", "FAIL", "INSUFFICIENT_EVIDENCE"]) == "BLOCKED"
    assert compute_verdict(["PASS", "INSUFFICIENT_EVIDENCE"]) == "NEEDS_REVIEW"
    assert compute_verdict(["PASS", "PASS"]) == "READY"
    assert compute_verdict([]) == "NEEDS_REVIEW"


def test_end_to_end_honest_labels_correct_decisions(jira, expected, labels, tmp_path):
    """Correct decisions with honest labels pass every guardrail (no false rejections), and the
    explorer reaches multi-hop evidence such as REQ-08 -> REC-4076 -> REC-9635."""
    model = ScriptedModel(lambda: ExplorerPolicy(honest_decider(labels, decisions_of(expected))))
    run, tb, _ = run_agent(jira, model, tmp_path=tmp_path)
    assert {r.id: r.decision for r in run.requirements} == decisions_of(expected)
    assert all(not r.guardrail_rejections and not r.agent_failure for r in run.requirements), \
        [(r.id, r.guardrail_rejections, r.agent_failure) for r in run.requirements if r.guardrail_rejections or r.agent_failure]
    assert run.status == "COMPLETE" and run.verdict == "BLOCKED"
    req08 = next(r for r in run.requirements if r.id == "REQ-08")
    counted = [e["ref"] for e in req08.evidence if e["counted"]]
    assert [tb.comment_index[c]["source_id"] for c in counted] == ["REC-9635"]
    assert [s["source_id"] for s in run.suspicious] == ["REC-5716"]
    md = render_markdown(run, tb.comment_index, PROJECT, LABEL, "https://example.atlassian.net")
    assert "Overall decision:** **BLOCKED" in md and "Evidence assessment" in md and "Priya Nair (QA Lead)" in md
    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(l)["event"] == "tool_call" for l in lines)


def test_with_honest_labels_only_correct_or_insufficient_is_accepted(jira, expected, labels):
    """For every requirement, any wrong definite decision is rejected by the guardrails."""
    truth = decisions_of(expected)
    for wrong in ("PASS", "FAIL"):
        attempt = {r: wrong for r, d in truth.items() if d != wrong}
        model = ScriptedModel(lambda: ExplorerPolicy(honest_decider(labels, attempt)))
        run, _, _ = run_agent(jira, model, only=sorted(attempt))
        for r in run.requirements:
            assert r.decision == "INSUFFICIENT_EVIDENCE" and r.guardrail_rejections, (wrong, r.id)


def test_model_labelling_everything_as_supporting_cannot_pass_unsupported(jira):
    def gullible(req, comments):
        ev = [{"ref": ref, "kind": "verified_result", "outcome": "supports", "applicable": True} for ref in comments]
        return {"decision": "PASS", "rationale": "all good", "evidence": ev, "outstanding": []}
    run, _, _ = run_agent(jira, ScriptedModel(lambda: ExplorerPolicy(gullible)),
                          only=["REQ-03", "REQ-06", "REQ-11"])
    for r in run.requirements:  # wrong prompt / staging approval / developer-only: applicability is checked in code
        assert r.decision != "PASS" and r.guardrail_rejections, r.id
    assert run.verdict is None and run.status == "PARTIAL"


def test_malformed_tool_call_is_nudged_and_recovers(jira, expected, labels):
    from tyn.model import MALFORMED_CALL, Turn

    class FlakyFirst(ExplorerPolicy):
        """Produces one malformed call before behaving normally."""
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.flaked = False
            self.pending = None
            self.notes = []

        def on_user(self, text):
            if not self.flaked:  # first reply: malformed call
                self.flaked, self.pending = True, text
                return Turn([], "", {"total": 1}, 0, MALFORMED_CALL)
            if self.pending is not None:  # the agent's nudge arrives: now behave
                self.notes.append(text)
                original, self.pending = self.pending, None
                return super().on_user(original)
            return super().on_user(text)

    policies = []

    def make():
        p = FlakyFirst(honest_decider(labels, decisions_of(expected)))
        policies.append(p)
        return p
    run, _, tracer = run_agent(jira, ScriptedModel(make), only=["REQ-08"])
    r = run.requirements[0]
    assert r.decision == "FAIL" and not r.agent_failure
    assert "malformed" in policies[0].notes[0].lower()
    assert any(e["event"] == "malformed_call" for e in tracer.events)


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


def test_discovery_failure_falls_back(jira, expected, labels):
    class BrokenDiscovery:
        def on_user(self, text):
            raise ModelError("boom")
    model = ScriptedModel(lambda: ExplorerPolicy(honest_decider(labels, decisions_of(expected))),
                          discovery=BrokenDiscovery)
    run, _, _ = run_agent(jira, model)
    assert len(run.requirements) == 12 and any("Discovery model failure" in w for w in run.warnings)


def test_trace_redacts_secrets(tmp_path):
    tr = Tracer(tmp_path / "t.jsonl", ["sekret-token-123"])
    tr.emit("x", note="token=abc123 and sekret-token-123 and AIzaSyA1234567890123456789012345")
    body = (tmp_path / "t.jsonl").read_text(encoding="utf-8")
    assert "sekret" not in body and "AIza" not in body and "abc123" not in body
    assert redact("Authorization: Basic dXNlcjp0b2tlbjEyMzQ1Njc4", []).count("[REDACTED]") == 1
