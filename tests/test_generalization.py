"""The tools and guardrails must not depend on this dataset's identifiers, values, roles,
wording or order. Every test here runs the real importer, tools, agent loop and guardrails
on a *perturbed* copy of the dataset, or on what-if variants where the right answer changes.
"""
import copy
from datetime import datetime, timezone

import pytest

from tyn.agent import Agent, Tracer
from tyn.importer import sync
from tyn.tools import Toolbox

from .conftest import FakeJira, ExplorerPolicy, ScriptedModel, honest_decider
from .perturb import NEW_LABEL, NEW_RELEASE, map_labels, perturb

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
PROJ = "GEN"


def run_on(data, release, policy, labels, decisions, only=None):
    fj = FakeJira()
    sync(fj, data, PROJ, workers=1)
    tb = Toolbox(fj, PROJ, data["dataset_label"], release, policy=policy, now=NOW)
    model = ScriptedModel(lambda: ExplorerPolicy(honest_decider(labels, decisions)))
    return Agent(tb, model, Tracer(None), only=only).run(), tb


@pytest.fixture(scope="module")
def perturbed(dataset):
    return perturb(dataset)


def test_perturbed_dataset_really_differs(dataset, perturbed):
    d, ids, _ = perturbed
    blob = str(d)
    for old in ("REQ-08", "REC-9635", "b7a91f2", "atlas-assistant", "R2.4", "QA Lead", "Security Owner",
                "tyn-release-assessment-v2"):
        assert old not in blob, old
    assert d["target"]["release"] == NEW_RELEASE and d["dataset_label"] == NEW_LABEL
    assert "environment prod;" in blob or "environment prod." in blob  # alias wording in comments


def test_correct_decisions_accepted_on_perturbed_data(expected, labels, perturbed):
    d, ids, policy = perturbed
    truth = {ids[k]: v["decision"] for k, v in expected["decisions"].items()}
    run, tb = run_on(d, NEW_RELEASE, policy, map_labels(labels, ids), truth)
    assert {r.id: r.decision for r in run.requirements} == truth
    assert all(not r.guardrail_rejections and not r.agent_failure for r in run.requirements), \
        [(r.id, r.guardrail_rejections) for r in run.requirements if r.guardrail_rejections]
    assert run.verdict == "BLOCKED"
    assert [s["source_id"] for s in run.suspicious] == [ids["REC-5716"]]


def test_wrong_decisions_rejected_on_perturbed_data(expected, labels, perturbed):
    d, ids, policy = perturbed
    truth = {ids[k]: v["decision"] for k, v in expected["decisions"].items()}
    for wrong in ("PASS", "FAIL"):
        attempt = {r: wrong for r, dec in truth.items() if dec != wrong}
        run, _ = run_on(d, NEW_RELEASE, policy, map_labels(labels, ids), attempt, only=sorted(attempt))
        assert all(r.decision == "INSUFFICIENT_EVIDENCE" and r.guardrail_rejections for r in run.requirements)


def test_policy_roles_are_configuration_not_code(expected, labels, perturbed):
    """With a policy that only authorises 'QA Lead', perturbed results by 'Test Engineer' must not
    count: authority comes from the policy file, not from code."""
    from tyn.policy import Policy
    d, ids, perturbed_pol = perturbed
    qa_only = Policy.from_dict({"verifier_roles": ["QA Lead"], "approver_roles": ["Head of Security"],
                                "value_aliases": {"production": ["prod"]}})
    run, _ = run_on(d, NEW_RELEASE, qa_only, map_labels(labels, ids), {ids["REQ-10"]: "PASS"},
                    only=[ids["REQ-10"]])
    assert run.requirements[0].decision == "INSUFFICIENT_EVIDENCE" and run.requirements[0].guardrail_rejections


# ------------------------------------------------------------------ what-if variants (answer changes)
def _edit_comments(data, sid, fn):
    d = copy.deepcopy(data)
    rec = next(r for r in d["records"] if r["source_id"] == sid)
    rec["comments"] = fn(rec["comments"])
    return d


def test_removing_contradiction_turns_insufficient_into_pass(dataset, labels):
    from tyn.policy import Policy
    d = _edit_comments(dataset, "REC-6198", lambda cs: [c for c in cs if "RB-402" not in c])
    run, _ = run_on(d, "R2.4", Policy.load(), labels, {"REQ-09": "PASS"}, only=["REQ-09"])
    assert run.requirements[0].decision == "PASS" and not run.requirements[0].guardrail_rejections


def test_removing_failure_evidence_makes_fail_unsupported(dataset, labels):
    from tyn.policy import Policy
    d = _edit_comments(dataset, "REC-1001", lambda cs: [c for c in cs if "ISO-318" not in c])
    run, _ = run_on(d, "R2.4", Policy.load(), labels, {"REQ-02": "FAIL"}, only=["REQ-02"])
    r = run.requirements[0]
    assert r.decision == "INSUFFICIENT_EVIDENCE" and r.guardrail_rejections  # missing proof is not failure


def test_new_explicit_retest_can_supersede_a_failure(dataset, labels):
    from tyn.policy import Policy
    retest = ("Recorded event 2026-11-10T10:00:00+05:30 | actor Priya Nair | role QA Lead\n"
              "Retest HND-78 explicitly supersedes HND-77: release R2.4; commit b7a91f2; service atlas-assistant; "
              "environment production. 12 of 12 escalated conversations reached the support queue; HND-DEF-4 closed.")
    d = _edit_comments(dataset, "REC-9635", lambda cs: cs + [retest])
    labs = dict(labels)
    labs["REC-9635|2026-11-08T14:00:00+05:30"] = {**labels["REC-9635|2026-11-08T14:00:00+05:30"],
                                                  "superseded_by": "REC-9635|2026-11-10T10:00:00+05:30"}
    labs["REC-9635|2026-11-10T10:00:00+05:30"] = {"kind": "verified_result", "outcome": "supports", "applicable": True}
    run, _ = run_on(d, "R2.4", Policy.load(), labs, {"REQ-08": "PASS"}, only=["REQ-08"])
    assert run.requirements[0].decision == "PASS" and not run.requirements[0].guardrail_rejections
