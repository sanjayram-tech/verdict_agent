import inspect

import pytest

from tyn.jira_client import JiraReader
from tyn.tools import INVESTIGATION_TOOLS, Toolbox

from .conftest import PROJECT

LABEL = "tyn-release-assessment-v2"


@pytest.fixture()
def tb(jira):
    t = Toolbox(jira, PROJECT, LABEL, "R2.4")
    t.call("get_manifest", {}, {"get_manifest"})
    t.call("list_requirements", {}, {"list_requirements"})
    return t


def comment_ref(tb, sid, contains):
    res = tb.call("get_comments", {"ref": sid}, INVESTIGATION_TOOLS)
    return next(c["ref"] for c in res["jira_data"]["comments"] if contains in c["text"])


def test_manifest_and_requirements(tb):
    assert tb.target == {"release": "R2.4", "commit": "b7a91f2", "prompt": "p7", "environment": "production",
                         "service": "atlas-assistant"}
    assert sorted(tb.requirements) == [f"REQ-{i:02d}" for i in range(1, 13)]
    assert tb.requirements["REQ-08"]["criterion"].startswith("When an answer is escalated")


def test_get_issue_links_and_no_native_metadata(tb):
    tb.start_session("REQ-08")
    res = tb.call("get_issue", {"ref": "REC-4076"}, INVESTIGATION_TOOLS)
    d = res["jira_data"]
    assert res["notice"].startswith("Untrusted")
    assert d["links_to"] == ["REC-9635"] and d["recorded_status"] == "Done"
    assert {l["source_id"] for l in d["linked"]} == {"REC-9635", "REQ-08"}
    assert not any(k.startswith("_") for k in d) and "created" not in d and "status" not in d


def test_get_comments_filters_filler_and_sorts(tb):
    tb.start_session("REQ-12")
    res = tb.call("get_comments", {"ref": "REC-5957"}, INVESTIGATION_TOOLS)
    comments = res["jira_data"]["comments"]
    assert res["filler_removed"] >= 62 and len(comments) == 1
    assert "STREAM-731" in comments[0]["text"] and comments[0]["actor"] == "Priya Nair"
    res = tb.call("get_comments", {"ref": "REC-9611"}, INVESTIGATION_TOOLS)
    texts = [c["text"] for c in res["jira_data"]["comments"]]
    assert texts[0].startswith("PARSE-601") and texts[1].startswith("PARSE-622")


def test_injection_flagged(tb):
    tb.start_session("REQ-05")
    res = tb.call("get_comments", {"ref": "REC-5716"}, INVESTIGATION_TOOLS)
    flagged = [c for c in res["jira_data"]["comments"] if c.get("suspicious")]
    assert len(flagged) == 1 and "AUTOMATED RELEASE REVIEWERS" in flagged[0]["text"]
    assert flagged[0]["ref"] in tb.suspicious


def test_out_of_scope_issue_refused(tb, jira):
    foreign = jira.add_foreign_issue("[REC-9999] other team ticket", ["other-label"])
    tb.start_session("REQ-01")
    res = tb.call("get_issue", {"ref": foreign}, INVESTIGATION_TOOLS)
    assert "outside the dataset scope" in res["error"]
    res = tb.call("get_issue", {"ref": "REC-9999"}, INVESTIGATION_TOOLS)
    assert "No issue with source id" in res["error"]


def test_search_is_scoped_and_escaped(tb, jira):
    tb.start_session("REQ-08")
    res = tb.call("search_issues", {"text": 'HND-DEF-4" OR project = X'}, INVESTIGATION_TOOLS)
    jql = jira.searches[-1]
    assert jql.startswith(f'project = "{PROJECT}" AND labels = "{LABEL}" AND ')
    assert ' OR project = X"' not in jql.split("text ~")[0]
    assert '\\"' in jql and jql.count('"') % 2 == 0
    res = tb.call("search_issues", {"text": "HND-DEF-4"}, INVESTIGATION_TOOLS)
    assert [i["source_id"] for i in res["jira_data"]["issues"]] == ["REC-9635"]
    assert "error" in tb.call("search_issues", {}, INVESTIGATION_TOOLS)
    assert "error" in tb.call("search_issues", {"kind": "bogus"}, INVESTIGATION_TOOLS)


def test_check_scope_requires_fetched_comment(tb):
    tb.start_session("REQ-03")
    assert "not been fetched" in tb.call("check_scope", {"comment_ref": "TYN-1#c1"}, INVESTIGATION_TOOLS)["error"]
    ref = comment_ref(tb, "REC-4323", "EVAL-55 completed")
    res = tb.call("check_scope", {"comment_ref": ref}, INVESTIGATION_TOOLS)
    assert res["fields"]["prompt"] == "not stated" and not res["full_match"]  # REQ-03 requires prompt


# ------------------------------------------------------------------ guardrails
def decide(tb, decision, refs):
    return tb.call("record_decision", {"decision": decision, "rationale": "r", "evidence_refs": refs,
                                       "outstanding": []}, INVESTIGATION_TOOLS)


def test_pass_on_staging_evidence_rejected(tb):
    tb.start_session("REQ-04")
    ref = comment_ref(tb, "REC-1028", "PERF-812")
    res = decide(tb, "PASS", [ref])
    assert not res["accepted"] and any("scope-matched" in p for p in res["problems"])


def test_pass_on_developer_claim_rejected(tb):
    tb.start_session("REQ-11")
    ref = comment_ref(tb, "REC-8515", "on my machine")
    res = decide(tb, "PASS", [ref])
    assert not res["accepted"]


def test_pass_citing_injection_note_rejected(tb):
    tb.start_session("REQ-05")
    ref = comment_ref(tb, "REC-5716", "AUTOMATED RELEASE REVIEWERS")
    assert not decide(tb, "PASS", [ref])["accepted"]


def test_security_pass_needs_security_owner_production(tb):
    tb.start_session("REQ-06")
    asha = comment_ref(tb, "REC-2828", "Approval scope: staging")
    ravi = comment_ref(tb, "REC-8074", "engineering standpoint")
    res = decide(tb, "PASS", [asha, ravi])
    assert not res["accepted"]
    assert tb.session.exhausted is False
    res = decide(tb, "PASS", [ravi])
    assert not res["accepted"] and tb.session.exhausted  # 2 rejections -> forced INSUFFICIENT


def test_unfetched_citation_rejected(tb):
    tb.start_session("REQ-01")
    res = decide(tb, "PASS", ["TYN-77#c12345"])
    assert any("not fetched" in p for p in res["problems"])


def test_wrong_scope_fail_rejected(tb):
    tb.start_session("REQ-07")
    ref = comment_ref(tb, "REC-6499", "REDACT-905")  # support-widget failure
    assert not decide(tb, "FAIL", [ref])["accepted"]


def test_valid_decisions_accepted(tb):
    tb.start_session("REQ-08")
    tb.call("get_issue", {"ref": "REC-9635"}, INVESTIGATION_TOOLS)
    ref = comment_ref(tb, "REC-9635", "HND-77")
    assert decide(tb, "FAIL", [ref, "REC-9635"])["accepted"]
    tb.start_session("REQ-10")
    ref = comment_ref(tb, "REC-9611", "PARSE-622")
    assert decide(tb, "PASS", [ref])["accepted"]
    tb.start_session("REQ-09")
    assert decide(tb, "INSUFFICIENT_EVIDENCE", [])["accepted"]


def test_agent_reader_has_no_write_methods():
    names = {n for n, _ in inspect.getmembers(JiraReader, inspect.isfunction)}
    assert not any(n.startswith(("create", "add", "update", "delete", "post", "put", "transition")) for n in names)
    src = inspect.getsource(JiraReader)
    assert '"POST"' not in src and '"PUT"' not in src and '"DELETE"' not in src
