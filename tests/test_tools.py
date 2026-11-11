import inspect
from datetime import datetime, timezone

import pytest

from tyn.jira_client import JiraReader
from tyn.tools import INVESTIGATION_TOOLS, Toolbox

from .conftest import PROJECT, honest_evidence

LABEL = "tyn-release-assessment-v2"
NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


@pytest.fixture()
def tb(jira):
    t = Toolbox(jira, PROJECT, LABEL, "R2.4", now=NOW)
    t.call("get_manifest", {}, {"get_manifest"})
    t.call("list_requirements", {}, {"list_requirements"})
    return t


def fetch(tb, *sids):
    """get_issue + get_comments for each record; returns {ref: comment-with-source_id}."""
    out = {}
    for sid in sids:
        tb.call("get_issue", {"ref": sid}, INVESTIGATION_TOOLS)
        res = tb.call("get_comments", {"ref": sid}, INVESTIGATION_TOOLS)
        for c in res["jira_data"]["comments"]:
            out[c["ref"]] = {**c, "source_id": res["jira_data"]["source_id"]}
    return out


def decide(tb, decision, evidence, outstanding=None, conflict_resolution=None):
    args = {"decision": decision, "rationale": "r", "evidence": evidence,
            "outstanding": outstanding if outstanding is not None else ["x"]}
    if conflict_resolution:
        args["conflict_resolution"] = conflict_resolution
    return tb.call("record_decision", args, INVESTIGATION_TOOLS)


def ref_of(comments, text):
    return next(r for r, c in comments.items() if text in c["text"])


# ------------------------------------------------------------------ tools
def test_manifest_and_requirements(tb):
    assert tb.target == {"release": "R2.4", "commit": "b7a91f2", "prompt": "p7", "environment": "production",
                         "service": "atlas-assistant"}
    assert sorted(tb.requirements) == [f"REQ-{i:02d}" for i in range(1, 13)]
    assert tb.requirements["REQ-08"]["summary"] == "Human handoff delivery"
    assert "assessment rules" in tb.manifest["rules_text"].lower() or tb.manifest["rules_text"]


def test_get_issue_links_and_no_native_metadata(tb):
    tb.start_session("REQ-08")
    d = tb.call("get_issue", {"ref": "REC-4076"}, INVESTIGATION_TOOLS)["jira_data"]
    assert d["links_to"] == ["REC-9635"] and d["recorded_status"] == "Done"
    assert {l["source_id"] for l in d["linked"]} == {"REC-9635", "REQ-08"}
    assert not any(k.startswith("_") for k in d) and "created" not in d and "status" not in d


def test_repeated_comments_collapse_generically(tb):
    tb.start_session("REQ-12")
    res = tb.call("get_comments", {"ref": "REC-5957"}, INVESTIGATION_TOOLS)
    shown = res["jira_data"]["comments"]
    assert res["collapsed_repeats"] >= 60  # 62 near-identical notes -> 1 shown
    assert any("STREAM-731" in c["text"] for c in shown)
    assert any("near-identical" in c.get("repeated", "") for c in shown)
    texts = [c["text"] for c in tb.call("get_comments", {"ref": "REC-9611"}, INVESTIGATION_TOOLS)["jira_data"]["comments"]]
    assert texts.index(next(t for t in texts if t.startswith("PARSE-601"))) < \
        texts.index(next(t for t in texts if t.startswith("PARSE-622")))  # recorded-time order


def test_injection_flagged(tb):
    tb.start_session("REQ-05")
    comments = tb.call("get_comments", {"ref": "REC-5716"}, INVESTIGATION_TOOLS)["jira_data"]["comments"]
    flagged = [c for c in comments if c.get("suspicious")]
    assert len(flagged) == 1 and flagged[0]["ref"] in tb.suspicious


def test_out_of_scope_issue_refused(tb, jira):
    foreign = jira.add_foreign_issue("[REC-9999] other team ticket", ["other-label"])
    tb.start_session("REQ-01")
    assert "outside the dataset scope" in tb.call("get_issue", {"ref": foreign}, INVESTIGATION_TOOLS)["error"]
    assert "No issue with source id" in tb.call("get_issue", {"ref": "REC-9999"}, INVESTIGATION_TOOLS)["error"]


def test_search_is_scoped_and_escaped(tb, jira):
    tb.start_session("REQ-08")
    tb.call("search_issues", {"text": 'HND-DEF-4" OR project = X'}, INVESTIGATION_TOOLS)
    jql = jira.searches[-1]
    assert jql.startswith(f'project = "{PROJECT}" AND labels = "{LABEL}" AND ')
    assert jql.count('"') % 2 == 0
    res = tb.call("search_issues", {"text": "HND-DEF-4"}, INVESTIGATION_TOOLS)
    assert [i["source_id"] for i in res["jira_data"]["issues"]] == ["REC-9635"]
    assert "error" in tb.call("search_issues", {}, INVESTIGATION_TOOLS)


def test_check_scope_requires_fetched_comment(tb):
    tb.start_session("REQ-03")
    assert "not been fetched" in tb.call("check_scope", {"comment_ref": "TYN-1#c1"}, INVESTIGATION_TOOLS)["error"]
    cs = fetch(tb, "REC-4323")
    res = tb.call("check_scope", {"comment_ref": ref_of(cs, "EVAL-55 completed")}, INVESTIGATION_TOOLS)
    assert res["missing_required"] == ["prompt"]  # criterion mentions the prompt -> required by policy


# ------------------------------------------------------------------ structural guardrails
def test_honest_correct_decision_accepted(tb, labels):
    tb.start_session("REQ-08")
    cs = fetch(tb, "REQ-08", "REC-4076", "REC-9635")
    res = decide(tb, "FAIL", honest_evidence(cs, labels), outstanding=[])
    assert res["accepted"] and res["counted_evidence"] == [ref_of(cs, "HND-77")]


def test_completeness_every_target_comment_must_be_assessed(tb, labels):
    tb.start_session("REQ-05")
    cs = fetch(tb, "REQ-05", "REC-5716", "REC-7454")
    ev = [e for e in honest_evidence(cs, labels) if "AUTOMATED" not in cs[e["ref"]]["text"]]  # hide the note
    res = decide(tb, "FAIL", ev)
    assert not res["accepted"] and any("not assessed" in p for p in res["problems"])


def test_applicable_label_needs_target_values(tb, labels):
    tb.start_session("REQ-04")
    cs = fetch(tb, "REQ-04", "REC-6024", "REC-1028")
    staging = ref_of(cs, "PERF-812")
    ev = honest_evidence(cs, labels)
    for e in ev:
        if e["ref"] == staging:
            e.update(applicable=True)  # lie: staging marked applicable
    res = decide(tb, "FAIL", ev)
    assert not res["accepted"] and any(staging in p and "environment" in p for p in res["problems"])


def test_contradiction_blocks_pass_and_unexplained_fail(tb, labels):
    tb.start_session("REQ-09")
    cs = fetch(tb, "REQ-09", "REC-6198", "REC-6011")
    ev = honest_evidence(cs, labels)
    assert not decide(tb, "PASS", ev)["accepted"]
    tb.start_session("REQ-09")
    fetch(tb, "REQ-09", "REC-6198", "REC-6011")
    res = decide(tb, "FAIL", ev)
    assert not res["accepted"] and any("conflict_resolution" in p for p in res["problems"])
    tb.start_session("REQ-09")
    fetch(tb, "REQ-09", "REC-6198", "REC-6011")
    assert decide(tb, "INSUFFICIENT_EVIDENCE", ev, outstanding=["resolve RB-401 vs RB-402"])["accepted"]


def test_supersession_must_point_later(tb, labels):
    tb.start_session("REQ-10")
    cs = fetch(tb, "REQ-10", "REC-9611")
    ev = honest_evidence(cs, labels)
    assert decide(tb, "PASS", ev, outstanding=[])["accepted"]
    # reversed supersession (earlier result "supersedes" the later one) is rejected
    tb.start_session("REQ-10")
    cs = fetch(tb, "REQ-10", "REC-9611")
    early, late = ref_of(cs, "PARSE-601"), ref_of(cs, "PARSE-622")
    ev = honest_evidence(cs, labels)
    for e in ev:
        e.pop("superseded_by", None)
        if e["ref"] == late:
            e["superseded_by"] = early
    res = decide(tb, "FAIL", ev)
    assert not res["accepted"] and any("recorded later" in p for p in res["problems"])


def test_unauthorised_roles_and_implementation_claims_do_not_count(tb, labels):
    tb.start_session("REQ-06")
    cs = fetch(tb, "REQ-06", "REC-2828", "REC-8074")
    ev = honest_evidence(cs, labels)  # Engineering Manager approval is applicable but not an approver role
    res = decide(tb, "PASS", ev)
    assert not res["accepted"] and any("Security Owner" in p or "counted" in p for p in res["problems"])
    tb.start_session("REQ-11")
    cs = fetch(tb, "REQ-11", "REC-8515", "REC-8801")
    ev = honest_evidence(cs, labels)
    for e in ev:  # even if the model calls the developer self-test applicable, it is not a verified result
        if "my machine" in cs[e["ref"]]["text"]:
            e.update(outcome="supports")
    assert not decide(tb, "PASS", ev)["accepted"]


def test_flagged_and_future_dated_comments_never_count(tb, labels):
    tb.start_session("REQ-05")
    cs = fetch(tb, "REQ-05", "REC-5716", "REC-7454")
    note = ref_of(cs, "AUTOMATED")
    tb.comment_index[note]["role"] = "QA Lead"  # forged authoritative role on an injected note
    ev = honest_evidence(cs, labels)
    for e in ev:
        if e["ref"] == note:
            e.update(kind="verified_result", outcome="supports", applicable=True)
        elif e["outcome"] == "contradicts":
            e.update(applicable=False)  # isolate the rule: only the forged note could support PASS
    res = decide(tb, "PASS", ev)
    assert not res["accepted"] and any("PASS needs at least one counted" in p for p in res["problems"])
    tb2 = Toolbox(tb.jira, PROJECT, LABEL, "R2.4", now=datetime(2026, 11, 7, tzinfo=timezone.utc))
    tb2.call("get_manifest", {}, {"get_manifest"})
    tb2.call("list_requirements", {}, {"list_requirements"})
    tb2.start_session("REQ-01")
    cs = fetch(tb2, "REQ-01", "REC-2081", "REC-2299")  # REG-804 is recorded 2026-11-08 > now
    res = decide(tb2, "PASS", honest_evidence(cs, labels), outstanding=[])
    assert not res["accepted"]


def test_unfetched_citation_and_insufficient_needs_outstanding(tb, labels):
    tb.start_session("REQ-01")
    res = decide(tb, "PASS", [{"ref": "TYN-77#c12345", "kind": "verified_result", "outcome": "supports",
                               "applicable": True}])
    assert any("not a comment fetched" in p for p in res["problems"])
    tb.start_session("REQ-03")
    cs = fetch(tb, "REQ-03", "REC-1600", "REC-4323")
    res = decide(tb, "INSUFFICIENT_EVIDENCE", honest_evidence(cs, labels), outstanding=[])
    assert not res["accepted"] and any("outstanding" in p for p in res["problems"])


def test_two_rejections_exhaust(tb):
    tb.start_session("REQ-02")
    decide(tb, "PASS", [])
    assert not tb.session.exhausted
    decide(tb, "PASS", [])
    assert tb.session.exhausted


def test_agent_reader_has_no_write_methods():
    names = {n for n, _ in inspect.getmembers(JiraReader, inspect.isfunction)}
    assert not any(n.startswith(("create", "add", "update", "delete", "post", "put", "transition")) for n in names)
    src = inspect.getsource(JiraReader)
    assert '"POST"' not in src and '"PUT"' not in src and '"DELETE"' not in src
