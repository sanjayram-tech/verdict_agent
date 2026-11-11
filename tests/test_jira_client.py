"""Retry safety: writes must never be blindly retried (that duplicated issues in a real run)."""
import pytest
import requests

import tyn.jira_client as jc
from tyn.config import JiraConfig
from tyn.importer import DuplicateIssuesError, existing_issues, plan_dedupe, sync
from tyn.jira_client import JiraError, JiraWriter

from .conftest import PROJECT, FakeJira

CFG = JiraConfig(base_url="https://example.atlassian.net", email="e@x", api_token="t")


class FakeResp:
    def __init__(self, status, body=b'{"ok": true}'):
        self.status_code = status
        self.content = body
        self.headers = {}
        self.text = body.decode()

    def json(self):
        import json
        return json.loads(self.content)


@pytest.fixture()
def writer(monkeypatch):
    monkeypatch.setattr(jc.time, "sleep", lambda s: None)
    return JiraWriter(CFG)


def script(writer, *outcomes):
    calls = []

    def request(method, url, timeout=None, **kw):
        calls.append((method, timeout))
        o = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(o, Exception):
            raise o
        return FakeResp(o)
    writer._s.request = request
    return calls


def test_post_timeout_is_not_retried(writer):
    calls = script(writer, requests.ReadTimeout("slow"))
    with pytest.raises(JiraError, match="may have been applied"):
        writer.create_issues_bulk([{"summary": "x"}])
    assert len(calls) == 1 and calls[0] == ("POST", JiraWriter.WRITE_TIMEOUT)


def test_post_502_not_retried_but_429_is(writer):
    calls = script(writer, 502)
    with pytest.raises(JiraError):
        writer.add_comment("K-1", {"type": "doc"})
    assert len(calls) == 1
    calls = script(writer, 429, 200)
    writer.add_comment("K-1", {"type": "doc"})
    assert len(calls) == 2


def test_get_timeout_is_retried(writer):
    calls = script(writer, requests.ReadTimeout("slow"), requests.ConnectionError("x"), 200)
    assert writer.get_issue("K-1", ["summary"]) == {"ok": True}
    assert len(calls) == 3 and calls[0][0] == "GET"


def test_duplicates_detected_and_dedupe_keeps_copy_with_data(dataset):
    fj = FakeJira()
    sync(fj, dataset, PROJECT, workers=1)
    # simulate the retried bulk-create: an extra copy of one record (no comments/links), created later
    sid = "REC-9635"
    orig_key = next(k for k, i in fj.issues.items() if f"src-{sid}" in i["fields"]["labels"])
    dup_key = fj.create_issues_bulk([{"project": {"key": PROJECT}, "summary": fj.issues[orig_key]["fields"]["summary"],
                                      "description": fj.issues[orig_key]["fields"]["description"],
                                      "labels": fj.issues[orig_key]["fields"]["labels"]}])[0]["key"]
    with pytest.raises(DuplicateIssuesError, match="dedupe"):
        existing_issues(fj, PROJECT, dataset["dataset_label"])
    plan = plan_dedupe(fj, PROJECT, dataset["dataset_label"])
    assert plan == [{"source_id": sid, "keep": orig_key, "delete": [dup_key],
                     "detail": {orig_key: {"comments": 5, "links": 1}, dup_key: {"comments": 0, "links": 0}}}]
    fj.delete_issue(dup_key)
    assert len(existing_issues(fj, PROJECT, dataset["dataset_label"])) == 400


def test_dedupe_ties_keep_lowest_key(dataset):
    fj = FakeJira()
    first = {**dataset, "records": [{**dataset["records"][0], "comments": [], "links": []}]}
    for _ in range(3):
        fj.create_issues_bulk([__import__("tyn.importer", fromlist=["issue_fields"]).issue_fields(
            first["records"][0], PROJECT, "Task", dataset["dataset_label"])])
    plan = plan_dedupe(fj, PROJECT, dataset["dataset_label"])
    assert plan[0]["keep"] == "TYN-1" and plan[0]["delete"] == ["TYN-2", "TYN-3"]
