"""Generate perturbed copies of the dataset to test that tools and guardrails are not
keyed to this dataset's identifiers, values, roles or ordering.

perturb() renames source ids, release/commit/service values, the dataset label and
verifier/approver roles, rewrites 'production' as the alias 'prod' in comments, and
shuffles comment order. It returns the new dataset, an old->new source-id map, and a
matching Policy (since roles are organisation configuration).
"""
from __future__ import annotations

import copy
import json
import random
import re
from typing import Any

from tyn.policy import Policy

VALUE_MAP = {"R2.4": "R7.1", "R2.3": "R7.0", "R2.2": "R6.9", "R2.1": "R6.8",
             "b7a91f2": "3c9e0d4", "atlas-assistant": "helios-api"}
ROLE_MAP = {"QA Lead": "Test Engineer", "Security Owner": "Head of Security"}
NEW_LABEL = "acme-release-check"
NEW_RELEASE = "R7.1"


def perturbed_policy() -> Policy:
    return Policy.from_dict({
        "verifier_roles": ["Test Engineer"],
        "approver_roles": ["Head of Security"],
        "criterion_rules": [{"when_criterion_mentions": "head of security", "require_approval_role": "Head of Security"}],
        "required_scope_fields": ["release", "commit", "service", "environment"],
        "conditional_scope_fields": {"prompt": ["prompt"]},
        "value_aliases": {"production": ["prod"]},
        "conflicting_values": {"environment": ["staging", "development", "sandbox", "uat"]},
    })


def _id_map(records: list[dict[str, Any]]) -> dict[str, str]:
    m = {}
    for r in records:
        sid = r["source_id"]
        if sid.startswith("REQ-"):
            m[sid] = f"RQ-{100 + int(sid[4:])}"
        elif sid.startswith("REC-"):
            m[sid] = f"EV-{(int(sid[4:]) * 7 + 13) % 90000 + 10000}"
        else:
            m[sid] = sid
    assert len(set(m.values())) == len(m), "id map must be unique"
    return m


def _sub(text: str, ids: dict[str, str], comment: bool) -> str:
    text = re.sub(r"\b(REQ-\d{2}|REC-\d{4})\b", lambda mo: ids.get(mo.group(1), mo.group(1)), text)
    for old, new in {**VALUE_MAP, **ROLE_MAP}.items():
        text = text.replace(old, new)
    if comment:
        text = re.sub(r"(?<![\w-])production(?![\w-])", "prod", text)
        text = re.sub(r"(?<![\w-])Production(?![\w-])", "Prod", text)
    return text


def perturb(data: dict[str, Any], seed: int = 7) -> tuple[dict[str, Any], dict[str, str], Policy]:
    d = copy.deepcopy(data)
    ids = _id_map(d["records"])
    rng = random.Random(seed)
    d["dataset_label"] = NEW_LABEL
    d["target"] = json.loads(_sub(json.dumps(d["target"]), ids, comment=False))
    for r in d["records"]:
        r["source_id"] = ids[r["source_id"]]
        r["release"] = VALUE_MAP.get(r["release"], r["release"])
        r["summary"] = _sub(r["summary"], ids, False)
        r["description"] = _sub(r["description"], ids, False)
        r["links"] = [ids[l] for l in r["links"]]
        r["comments"] = [_sub(c, ids, True) for c in r["comments"]]
        rng.shuffle(r["comments"])
    rng.shuffle(d["records"])
    return d, ids, perturbed_policy()


def map_labels(labels: dict[str, dict[str, Any]], ids: dict[str, str]) -> dict[str, dict[str, Any]]:
    def key(k: str) -> str:
        sid, ts = k.split("|", 1)
        return f"{ids.get(sid, sid)}|{ts}"
    out = {}
    for k, v in labels.items():
        v = dict(v)
        if v.get("superseded_by"):
            v["superseded_by"] = key(v["superseded_by"])
        out[key(k)] = v
    return out
