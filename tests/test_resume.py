"""Checkpoint + resume: an interrupted or partly failed run continues into the same run folder,
reusing validated decisions and re-assessing only failed or missing requirements."""
import json
from datetime import datetime, timezone

import pytest

from tyn.agent import Agent, Tracer
from tyn.checkpoint import Checkpoint, CheckpointError
from tyn.model import ModelError
from tyn.report import render_markdown
from tyn.tools import Toolbox

from .conftest import PROJECT, DiscoveryPolicy, ExplorerPolicy, ScriptedModel, honest_decider

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
LABEL = "tyn-release-assessment-v2"


def decisions_of(expected):
    return {k: v["decision"] for k, v in expected["decisions"].items()}


class Counting(ScriptedModel):
    """ScriptedModel that records which requirement each investigation chat was for."""

    def __init__(self, make_investigator, fail_when=None):
        super().__init__(make_investigator)
        self.chats: list[str] = []
        self.discovery_chats = 0
        self.fail_when = fail_when or (lambda req, n: None)

    def start_chat(self, system, tools):
        if "submit_assessment_plan" in tools:
            self.discovery_chats += 1
            return super().start_chat(system, tools)
        chat = super().start_chat(system, tools)
        model = self
        original = chat.policy.on_user

        def on_user(text):
            req = text.split("Requirement ", 1)[1].split()[0]
            model.chats.append(req)
            exc = model.fail_when(req, len(model.chats))
            if exc:
                raise exc
            return original(text)
        chat.policy.on_user = on_user
        return chat


def session(jira, tmp_path, model, ckpt, resume=False):
    tb = Toolbox(jira, PROJECT, LABEL, "R2.4", now=NOW)
    tracer = Tracer(tmp_path / "trace.jsonl", append=resume)

    def on_result(rr):
        ckpt.set_target(tb.target)
        ckpt.save_result(rr)
    run = Agent(tb, model, tracer, on_result=on_result, resume=ckpt.resume_state() if resume else None).run()
    ckpt.set_target(run.target)
    ckpt.record_session(run.metrics["sessions"][-1])
    return run, tb


def honest(labels, expected):
    return lambda: ExplorerPolicy(honest_decider(labels, decisions_of(expected)))


def test_interrupted_run_resumes_without_redoing_work(jira, expected, labels, tmp_path):
    ckpt = Checkpoint.create(tmp_path, PROJECT, LABEL, "R2.4", None)
    first = Counting(honest(labels, expected),
                     fail_when=lambda req, n: KeyboardInterrupt() if req == "REQ-05" else None)
    run1, _ = session(jira, tmp_path, first, ckpt)
    assert run1.status == "INCOMPLETE" and run1.verdict is None
    assert Checkpoint.load(tmp_path).completed() == ["REQ-01", "REQ-02", "REQ-03", "REQ-04"]

    second = Counting(honest(labels, expected))
    run2, tb = session(jira, tmp_path, second, Checkpoint.load(tmp_path), resume=True)
    assert second.discovery_chats == 0  # resume re-reads Jira directly, no model calls for discovery
    assert second.chats == [f"REQ-{i:02d}" for i in range(5, 13)]  # REQ-01..04 were not re-assessed
    assert {r.id: r.decision for r in run2.requirements} == decisions_of(expected)
    assert run2.status == "COMPLETE" and run2.verdict == "BLOCKED"
    assert {r.id: r.session for r in run2.requirements}["REQ-01"] == 1
    assert {r.id: r.session for r in run2.requirements}["REQ-12"] == 2
    m = run2.metrics
    assert len(m["sessions"]) == 2 and m["model_calls"] == sum(s["model_calls"] for s in m["sessions"])
    events = [json.loads(l)["event"] for l in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events.count("run_start") == 2 and "resume" in events  # trace appended, not overwritten
    md = render_markdown(run2, tb.comment_index, PROJECT, LABEL, None)
    assert "This run was resumed: 2 sessions" in md
    assert "Priya Nair (QA Lead)" in md.split("### REQ-01")[1].split("### REQ-02")[0]  # citations survive resume


def test_failed_requirements_are_reassessed_on_resume(jira, expected, labels, tmp_path):
    ckpt = Checkpoint.create(tmp_path, PROJECT, LABEL, "R2.4", None)
    quota = Counting(honest(labels, expected),
                     fail_when=lambda req, n: ModelError("HTTP 429 RESOURCE_EXHAUSTED") if req in ("REQ-03", "REQ-07") else None)
    run1, _ = session(jira, tmp_path, quota, ckpt)
    failed = sorted(r.id for r in run1.requirements if r.agent_failure)
    assert failed == ["REQ-03", "REQ-07"] and run1.status == "COMPLETE"  # fail-safe INSUFFICIENT, verdict computed

    again = Counting(honest(labels, expected))
    run2, _ = session(jira, tmp_path, again, Checkpoint.load(tmp_path), resume=True)
    assert again.chats == ["REQ-03", "REQ-07"]
    assert {r.id: r.decision for r in run2.requirements} == decisions_of(expected)
    assert not any(r.agent_failure for r in run2.requirements)


def test_injection_flags_survive_resume(jira, expected, labels, tmp_path):
    ckpt = Checkpoint.create(tmp_path, PROJECT, LABEL, "R2.4", None)
    first = Counting(honest(labels, expected),
                     fail_when=lambda req, n: KeyboardInterrupt() if req == "REQ-06" else None)
    session(jira, tmp_path, first, ckpt)  # REQ-05 (with the injected note) decided in session 1
    run2, _ = session(jira, tmp_path, Counting(honest(labels, expected)), Checkpoint.load(tmp_path), resume=True)
    assert [s["source_id"] for s in run2.suspicious] == ["REC-5716"]


def test_resume_refuses_when_target_changed(jira, expected, labels, tmp_path):
    ckpt = Checkpoint.create(tmp_path, PROJECT, LABEL, "R2.4", None)
    session(jira, tmp_path, Counting(honest(labels, expected),
                                     fail_when=lambda req, n: KeyboardInterrupt() if req == "REQ-02" else None), ckpt)
    ckpt = Checkpoint.load(tmp_path)
    ckpt.data["target"] = {**ckpt.data["target"], "commit": "0000000"}
    second = Counting(honest(labels, expected))
    run2, _ = session(jira, tmp_path, second, ckpt, resume=True)
    assert run2.status == "ABORTED" and second.chats == []
    assert any("Target changed" in w for w in run2.warnings)


def test_checkpoint_validation(tmp_path):
    with pytest.raises(CheckpointError, match="No checkpoint.json"):
        Checkpoint.load(tmp_path / "missing")
    ckpt = Checkpoint.create(tmp_path, "KAN", LABEL, "R2.4", None)
    ckpt.check_matches("KAN", None, "R2.4")
    with pytest.raises(CheckpointError, match="does not match"):
        ckpt.check_matches("OTHER", None, None)
    assert not list(tmp_path.glob("*.tmp"))  # atomic write leaves no temp files
