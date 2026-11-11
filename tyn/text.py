"""Deterministic text helpers: comment headers, filler detection, scope extraction,
injection detection and safe output escaping. No network, no model."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------- comment header
HEADER_RE = re.compile(r"^Recorded event (?P<ts>\S+) \| actor (?P<actor>.+?) \| role (?P<role>.+?)\s*$")


def parse_comment(body: str) -> dict[str, Any]:
    """Split 'Recorded event <ts> | actor <a> | role <r>\\n<text>' into fields.

    Comments whose first line does not match are kept with header_unparsed=True.
    """
    first, _, rest = body.partition("\n")
    m = HEADER_RE.match(first.strip())
    if not m:
        return {"recorded_at": None, "actor": None, "role": None, "text": body.strip(), "header_unparsed": True}
    return {"recorded_at": m["ts"], "actor": m["actor"].strip(), "role": m["role"].strip(),
            "text": rest.strip(), "header_unparsed": False}


def sort_key(recorded_at: str | None) -> tuple[int, float]:
    """Chronological sort by the *recorded* scenario time; unparseable last."""
    if not recorded_at:
        return (1, 0.0)
    try:
        return (0, datetime.fromisoformat(recorded_at).timestamp())
    except ValueError:
        return (1, 0.0)


# ---------------------------------------------------------------- filler
# A comment is filler only if its ENTIRE text matches one of these patterns, so a
# filler opening followed by real content is never dropped.
FILLER_PATTERNS = [re.compile(p) for p in (
    r"Picked this up\. Will update once the change is in review\.",
    r"Discussed in the handover call; details are in the comments here\.",
    r"Tracking for the release checklist\.",
    r"Status updated\.",
    r"Reminder: please keep results in this ticket so the release review can find them\.",
    r"Linked the related work so dependencies are visible\.",
    r"Streaming investigation note \d+: checking buffering and UI rendering\.",
)]


def is_filler(text: str) -> bool:
    t = text.strip()
    return any(p.fullmatch(t) for p in FILLER_PATTERNS)


# ---------------------------------------------------------------- scope
_SCOPE_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "release": [re.compile(r"\brelease\s+(R\d+\.\d+)\b", re.I)],
    "commit": [re.compile(r"\bcommit\s+([0-9a-f]{7,40})\b", re.I)],
    "service": [re.compile(r"\bservice\s+([a-z][a-z0-9-]*[a-z0-9])\b", re.I)],
    "environment": [
        re.compile(r"\benvironment\s+([a-z]+)\b", re.I),
        re.compile(r"\bapproval scope:\s*([a-z]+)\b", re.I),
        re.compile(r"\bfor\s+(production|staging)\b", re.I),
    ],
    "prompt": [re.compile(r"\bprompt(?:\s+bundle)?\s+(p\d+)\b", re.I)],
}
SCOPE_FIELDS = ("release", "commit", "service", "environment", "prompt")


def extract_scope(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for field, pats in _SCOPE_PATTERNS.items():
        vals: list[str] = []
        for p in pats:
            for m in p.finditer(text):
                v = m.group(1)
                v = v.lower() if field in ("service", "environment", "commit", "prompt") else v.upper()
                if v not in vals:
                    vals.append(v)
        found[field] = vals
    return found


def check_scope(text: str, target: dict[str, str], require_prompt: bool) -> dict[str, Any]:
    """Compare scope statements in `text` with the target, field by field.

    A field matches only if it is stated exactly once-valued and equals the target.
    """
    found = extract_scope(text)
    required = ["release", "commit", "service", "environment"] + (["prompt"] if require_prompt else [])
    fields: dict[str, Any] = {}
    full = True
    for f in SCOPE_FIELDS:
        want = str(target.get(f, "")).lower() if f != "release" else str(target.get(f, "")).upper()
        vals = found[f]
        if not vals:
            status = "not stated"
        elif len(vals) > 1:
            status = f"conflicting values {vals}"
        elif vals[0] == want:
            status = "match"
        else:
            status = f"MISMATCH (found {vals[0]}, target {want})"
        fields[f] = status
        if f in required and status != "match":
            full = False
    return {"fields": fields, "required_fields": required, "full_match": full}


# ---------------------------------------------------------------- injection
INJECTION_PATTERNS = [re.compile(p, re.I) for p in (
    r"automated (release )?review(er)?s?",
    r"ignore (all |any )?(previous|prior|above) (instructions|rules)",
    r"\breport\s+REQ-\d+\s+as\s+(PASS|READY)",
    r"\bmark (this|it|REQ-\d+) as (PASS|READY)\b",
    r"do not mention",
    r"\bwaived\b",
    r"\b(system|developer) prompt\b",
    r"you are (an?|the) (ai|assistant|model|agent)",
)]


def injection_signals(text: str) -> list[str]:
    return [p.pattern for p in INJECTION_PATTERNS if p.search(text)]


# ---------------------------------------------------------------- output safety
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>()#!|~])")


def md_escape(text: str, limit: int | None = None) -> str:
    """Neutralise markdown/HTML in quoted ticket text (no links, images, tags)."""
    t = " ".join(text.split())
    if limit and len(t) > limit:
        t = t[: limit - 1] + "…"
    return _MD_SPECIAL.sub(r"\\\1", t)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 15] + " …[truncated]"
