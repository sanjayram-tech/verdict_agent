"""Nothing may assume this dataset's size (12 requirements, 400 records, a few comments per
ticket). A synthetic manifest with many requirements and a ticket with many comments must be
handled completely: no silent caps, and run-wide budgets scale with the plan."""
import json
from datetime import datetime, timezone

from tyn.agent import Agent, Budgets, Tracer
from tyn.importer import pick_search_probe, sync
from tyn.model import ToolCall, Turn
from tyn.tools import INVESTIGATION_TOOLS, Toolbox

from .conftest import FakeJira, ScriptedModel
from .perturb import perturb

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
TARGET = {"release": "R9.0", "commit": "abc1234", "service": "svc", "environment": "production"}
N_REQ = 130


def synthetic(n_req: int = N_REQ, n_comments: int = 0) -> dict:
    hdr = "Recorded event 2026-11-0{d}T10:{m:02d}:00+05:30 | actor Alex Q | role QA Lead\n"
    reqs = [{"source_id": f"RQ-{i:03d}", "kind": "requirement", "summary": f"Requirement {i}", "release": "R9.0",
             "recorded_status": "Done", "links": [],
             "description": f"Requirement ID: RQ-{i:03d}\nMandatory: yes\nAcceptance criterion: Check {i} holds.",
             "comments": [hdr.format(d=1 + (k % 8), m=k % 60) + f"Note {k} about RUN-{i}-{k}." for k in range(n_comments)]
             if i == 1 else []}
            for i in range(1, n_req + 1)]
    manifest = {"source_id": "MANIFEST", "kind": "manifest", "summary": "Release manifest", "release": "R9.0",
                "recorded_status": "Done", "comments": [], "links": [r["source_id"] for r in reqs],
                "description": "Synthetic.\nTarget: " + json.dumps(TARGET) + "\nRules: assess every linked requirement."}
    return {"dataset_label": "synthetic-scale", "target": TARGET, "records": [manifest, *reqs]}


class QuickDecider:
    """Discovery/investigation policy that records INSUFFICIENT immediately (no evidence)."""
    def on_user(self, text):
        if "Requirement" not in text:
            return Turn([ToolCall("get_manifest", {})], usage={"total": 1})
        return Turn([ToolCall("record_decision", {"decision": "INSUFFICIENT_EVIDENCE", "rationale": "none",
                                                  "evidence": [], "outstanding": ["verify"]})], usage={"total": 1})

    def on_results(self, results, note):
        call, res = results[-1]
        if call.name == "get_manifest":
            return Turn([ToolCall("list_requirements", {})], usage={"total": 1})
        if call.name == "list_requirements":
            ids = [r["id"] for r in res["jira_data"]["requirements"]]
            return Turn([ToolCall("submit_assessment_plan", {"requirement_ids": ids})], usage={"total": 1})
        return Turn([], usage={"total": 1})


def test_large_manifest_is_fully_listed_planned_and_assessed():
    fj = FakeJira()
    sync(fj, synthetic(), "SC", workers=1)
    tb = Toolbox(fj, "SC", "synthetic-scale", "R9.0", now=NOW)
    model = ScriptedModel(QuickDecider, discovery=QuickDecider)
    run = Agent(tb, model, Tracer(None)).run()
    assert len(tb.requirements) == N_REQ  # no 100-result cap on listing
    assert len(run.requirements) == N_REQ and run.status == "COMPLETE"
    assert not any(r.agent_failure for r in run.requirements)  # run-wide budgets scaled with the plan
    assert run.verdict == "NEEDS_REVIEW"


def test_budgets_scale_with_plan_unless_set():
    b = Budgets()
    assert b.total_tool_calls_for(12) < b.total_tool_calls_for(130)
    assert b.wall_s_for(130) > b.wall_s_for(12)
    fixed = Budgets(max_total_tool_calls=50, max_wall_s=60)
    assert fixed.total_tool_calls_for(130) == 50 and fixed.wall_s_for(130) == 60


def test_comments_are_paged_not_truncated():
    fj = FakeJira()
    sync(fj, synthetic(n_req=2, n_comments=95), "SC", workers=1)
    tb = Toolbox(fj, "SC", "synthetic-scale", "R9.0", now=NOW)
    tb.call("get_manifest", {}, {"get_manifest"})
    tb.call("list_requirements", {}, {"list_requirements"})
    tb.start_session("RQ-001")
    seen, offset = [], 0
    while True:
        res = tb.call("get_comments", {"ref": "RQ-001", "offset": offset}, INVESTIGATION_TOOLS)
        seen += [c["ref"] for c in res["jira_data"]["comments"]]
        assert len(tb.session.fetched_comments) == len(seen)  # only returned comments count as fetched
        if "next_offset" not in res["paging"]:
            break
        offset = res["paging"]["next_offset"]
    assert len(seen) == len(set(seen)) == 95
    assert "error" in tb.call("get_comments", {"ref": "RQ-001", "offset": -1}, INVESTIGATION_TOOLS)


def test_results_differing_only_in_numbers_are_never_collapsed():
    """Evidence like '9 of 10 pass' / '10 of 10 pass' on one ticket must all be shown, while
    true boilerplate repeated on the ticket is still collapsed."""
    d = synthetic(n_req=1)
    hdr = "Recorded event 2026-11-08T1{h}:00:00+05:30 | actor Alex Q | role QA Lead\n"
    results = [hdr.format(h=k) + f"Retest: release R9.0; commit abc1234; service svc; environment production. "
               f"{k + 5} of 10 cases pass." for k in range(4)]
    noise = [hdr.format(h=k) + f"Investigation note {k}: checking caches." for k in range(5)]
    d["records"][1]["comments"] = results + noise
    fj = FakeJira()
    sync(fj, d, "SC", workers=1)
    tb = Toolbox(fj, "SC", "synthetic-scale", "R9.0", now=NOW)
    tb.call("get_manifest", {}, {"get_manifest"})
    tb.call("list_requirements", {}, {"list_requirements"})
    tb.start_session("RQ-001")
    res = tb.call("get_comments", {"ref": "RQ-001"}, INVESTIGATION_TOOLS)
    texts = [c["text"] for c in res["jira_data"]["comments"]]
    assert sum("cases pass" in t for t in texts) == 4  # every result shown
    assert sum("Investigation note" in t for t in texts) == 1 and res["collapsed_repeats"] == 4


def test_search_probe_is_derived_from_the_data(dataset):
    tok, sid = pick_search_probe(dataset["records"])
    assert any(tok in c for r in dataset["records"] if r["source_id"] == sid for c in r["comments"])
    assert sum(any(tok in t for t in [r["summary"], r["description"], *r["comments"]]) for r in dataset["records"]) == 1
    d, ids, _ = perturb(dataset)
    tok2, sid2 = pick_search_probe(d["records"])
    assert sid2 in ids.values()
    assert pick_search_probe([{"source_id": "A", "summary": "", "description": "", "comments": ["no ids here"]}]) is None
