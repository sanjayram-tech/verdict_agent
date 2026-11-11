"""Offline test doubles: an in-memory Jira (populated by the real importer) and a
scripted model that drives the real agent loop through the real tools."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tyn.importer import load_dataset, sync  # noqa: E402
from tyn.jira_client import JiraError  # noqa: E402
from tyn.model import ToolCall, Turn  # noqa: E402

DATASET = ROOT / "inputs" / "dataset.json"
PROJECT = "TYN"


# --------------------------------------------------------------------------- fake Jira
class FakeJira:
    def __init__(self) -> None:
        self.issues: dict[str, dict[str, Any]] = {}
        self.n = 0
        self.writes = {"issues": 0, "comments": 0, "links": 0}
        self.searches: list[str] = []
        self.get_issue_calls = 0

    # -- read
    def search(self, jql: str, fields: list[str], max_results: int | None = None):
        self.searches.append(jql)
        proj = re.search(r'project = "([^"]+)"', jql)
        labels = re.findall(r'labels = "([^"]+)"', jql)
        phrase = re.search(r'text ~ "\\"(.+?)\\""', jql)
        out = []
        for key in sorted(self.issues, key=lambda k: int(k.split("-")[1])):
            iss = self.issues[key]
            if proj and not key.startswith(proj.group(1) + "-"):
                continue
            if any(l not in iss["fields"]["labels"] for l in labels):
                continue
            if phrase:
                from tyn.adf import adf_to_text
                hay = " ".join([iss["fields"]["summary"], adf_to_text(iss["fields"]["description"])] +
                               [adf_to_text(c["body"]) for c in iss["comments"]]).lower()
                if phrase.group(1).lower() not in hay:
                    continue
            out.append({"key": key, "fields": {f: iss["fields"].get(f) for f in fields}})
            if max_results and len(out) >= max_results:
                break
        return iter(out)

    def get_issue(self, key: str, fields: list[str]) -> dict[str, Any]:
        self.get_issue_calls += 1
        if key not in self.issues:
            raise JiraError(404, "Issue does not exist or you do not have permission to see it.")
        iss = self.issues[key]
        return {"key": key, "fields": {f: iss["fields"].get(f) for f in fields}}

    def get_comments(self, key: str) -> list[dict[str, Any]]:
        if key not in self.issues:
            raise JiraError(404, "not found")
        return list(self.issues[key]["comments"])

    # -- write (importer only)
    def create_issues_bulk(self, issue_fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for f in issue_fields:
            self.n += 1
            key = f"{f['project']['key']}-{self.n}"
            self.issues[key] = {"fields": {"summary": f["summary"], "description": f["description"],
                                           "labels": list(f["labels"]), "issuelinks": []}, "comments": []}
            self.writes["issues"] += 1
            out.append({"key": key, "id": str(self.n)})
        return out

    def add_comment(self, key: str, body_adf: dict[str, Any]) -> dict[str, Any]:
        cid = str(10000 + sum(len(i["comments"]) for i in self.issues.values()))
        self.issues[key]["comments"].append({"id": cid, "body": body_adf})
        self.writes["comments"] += 1
        return {"id": cid}

    def create_link(self, link_type: str, from_key: str, to_key: str) -> None:
        a, b = self.issues[from_key], self.issues[to_key]
        a["fields"]["issuelinks"].append({"type": {"name": link_type},
                                          "outwardIssue": {"key": to_key, "fields": {"summary": b["fields"]["summary"]}}})
        b["fields"]["issuelinks"].append({"type": {"name": link_type},
                                          "inwardIssue": {"key": from_key, "fields": {"summary": a["fields"]["summary"]}}})
        self.writes["links"] += 1

    def delete_issue(self, key: str) -> None:
        del self.issues[key]

    def add_foreign_issue(self, summary: str, labels: list[str]) -> str:
        from tyn.adf import text_to_adf
        self.n += 1
        key = f"{PROJECT}-{self.n}"
        self.issues[key] = {"fields": {"summary": summary, "description": text_to_adf("foreign"),
                                       "labels": labels, "issuelinks": []}, "comments": []}
        return key


@pytest.fixture(scope="session")
def dataset() -> dict[str, Any]:
    return load_dataset(DATASET)


@pytest.fixture()
def jira(dataset) -> FakeJira:
    fj = FakeJira()
    sync(fj, dataset, PROJECT, workers=1)
    return fj


@pytest.fixture(scope="session")
def expected() -> dict[str, Any]:
    return json.loads((ROOT / "tests" / "fixtures" / "expected_decisions.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def labels() -> dict[str, Any]:
    return json.loads((ROOT / "tests" / "fixtures" / "expected_evidence.json").read_text(encoding="utf-8"))["labels"]


DEFAULT_LABEL = {"kind": "other", "outcome": "neutral", "applicable": False}


def honest_evidence(comments: dict[str, dict[str, Any]], labels: dict[str, Any],
                    overrides: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Label every fetched comment the way an honest reviewer would (fixture labels)."""
    key_to_ref = {f"{c['source_id']}|{c['recorded_at']}": ref for ref, c in comments.items()}
    out = []
    for ref, c in comments.items():
        k = f"{c['source_id']}|{c['recorded_at']}"
        lab = {**DEFAULT_LABEL, **labels.get(k, {}), **(overrides or {}).get(k, {})}
        item = {"ref": ref, "kind": lab["kind"], "outcome": lab["outcome"], "applicable": lab["applicable"]}
        if lab.get("superseded_by") in key_to_ref:
            item["superseded_by"] = key_to_ref[lab["superseded_by"]]
        out.append(item)
    return out


def honest_decider(labels: dict[str, Any], decisions: dict[str, str]):
    """decide() for ExplorerPolicy: honest labels + the given decision per requirement."""
    def decide(req: str, comments: dict[str, dict[str, Any]]) -> dict[str, Any]:
        d = decisions[req]
        return {"decision": d, "rationale": f"scripted {d}", "evidence": honest_evidence(comments, labels),
                "outstanding": ["further verification on the target build"] if d == "INSUFFICIENT_EVIDENCE" else []}
    return decide


# --------------------------------------------------------------------------- scripted model
class ScriptedChat:
    def __init__(self, policy: Any):
        self.policy = policy

    def send_user(self, text: str) -> Turn:
        return self.policy.on_user(text)

    def send_tool_results(self, results: list[tuple[ToolCall, dict[str, Any]]], note: str | None = None) -> Turn:
        return self.policy.on_results(results, note)


class ScriptedModel:
    name = "scripted-test-model"

    def __init__(self, investigator: Callable[[], Any], discovery: Callable[[], Any] | None = None):
        self.investigator = investigator
        self.discovery = discovery or DiscoveryPolicy

    def start_chat(self, system: str, tools: dict[str, Any]) -> ScriptedChat:
        if "submit_assessment_plan" in tools:
            return ScriptedChat(self.discovery())
        return ScriptedChat(self.investigator())


def turn(*calls: ToolCall) -> Turn:
    return Turn(calls=list(calls), usage={"prompt": 100, "output": 10, "thoughts": 0, "total": 110})


class DiscoveryPolicy:
    def __init__(self) -> None:
        self.step = 0

    def on_user(self, text: str) -> Turn:
        return turn(ToolCall("get_manifest", {}))

    def on_results(self, results, note) -> Turn:
        call, res = results[-1]
        if call.name == "get_manifest":
            return turn(ToolCall("list_requirements", {}))
        ids = [r["id"] for r in res["jira_data"]["requirements"]]
        return turn(ToolCall("submit_assessment_plan", {"requirement_ids": ids}))


class ExplorerPolicy:
    """Follows links breadth-first (<=3 hops, never into the manifest), collecting every
    comment, then asks `decide(req_id, comments)` for the record_decision arguments.
    No dataset-specific id prefixes are assumed."""

    def __init__(self, decide: Callable[..., dict[str, Any]], check_scope: bool = False):
        self.decide = decide
        self.do_scope = check_scope
        self.queue: list[tuple[str, int]] = []
        self.visited: set[str] = set()
        self.comments: dict[str, dict[str, Any]] = {}
        self.scopes: dict[str, dict[str, Any]] = {}
        self.to_scope: list[str] = []
        self.req = ""
        self.depths: dict[str, int] = {}
        self.rejections: list[list[str]] = []

    def on_user(self, text: str) -> Turn:
        self.req = re.search(r"Requirement (\S+)", text).group(1)
        self.visited.add(self.req)
        return turn(ToolCall("get_issue", {"ref": self.req}), ToolCall("get_comments", {"ref": self.req}))

    def on_results(self, results, note) -> Turn:
        for call, res in results:
            data = res.get("jira_data", {})
            if call.name == "get_issue" and "linked" in data:
                depth = self.depths.get(data["source_id"], 0)
                for l in data["linked"]:
                    sid = l["source_id"]
                    if sid and sid not in self.visited and sid != "MANIFEST" and depth < 3:
                        self.visited.add(sid)
                        self.depths[sid] = depth + 1
                        self.queue.append((sid, depth + 1))
            if call.name == "get_comments":
                for c in data.get("comments", []):
                    c["source_id"] = data.get("source_id")
                    self.comments[c["ref"]] = c
                    if self.do_scope:
                        self.to_scope.append(c["ref"])
            if call.name == "check_scope" and "all_required_stated" in res:
                self.scopes[res["comment_ref"]] = res
            if call.name == "record_decision" and not res.get("accepted"):
                self.rejections.append(res.get("problems", []))
                args = dict(call.args)
                args.update(decision="INSUFFICIENT_EVIDENCE", rationale="fallback after rejection",
                            outstanding=["manual review"])
                return turn(ToolCall("record_decision", args))
        if self.queue:
            sid, _ = self.queue.pop(0)
            return turn(ToolCall("get_issue", {"ref": sid}), ToolCall("get_comments", {"ref": sid}))
        if self.to_scope:
            ref = self.to_scope.pop(0)
            return turn(ToolCall("check_scope", {"comment_ref": ref}))
        return turn(ToolCall("record_decision", self.decide(self.req, self.comments)))
