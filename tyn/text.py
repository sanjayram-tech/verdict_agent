"""Deterministic, dataset-agnostic text helpers: comment headers, repeat collapsing,
target-value matching, injection signals and safe output escaping.

Nothing here encodes this dataset's wording: scope checks look for the *target values*
from the manifest (plus policy aliases), not for a particular sentence format.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .policy import Policy

# ---------------------------------------------------------------- comment header
# Importer contract: each imported comment starts with the recorded scenario metadata.
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


def parse_time(recorded_at: str | None) -> datetime | None:
    if not recorded_at:
        return None
    try:
        dt = datetime.fromisoformat(recorded_at)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sort_key(recorded_at: str | None) -> tuple[int, float]:
    """Chronological sort by the *recorded* scenario time; unparseable last."""
    dt = parse_time(recorded_at)
    return (0, dt.timestamp()) if dt else (1, 0.0)


# ---------------------------------------------------------------- repeats / boilerplate
def normalize_for_repeat(text: str) -> str:
    """Digits and whitespace normalised, so 'note 1' / 'note 2' count as repeats."""
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text.lower())).strip()


def matches_boilerplate(text: str, policy: Policy) -> bool:
    t = text.strip()
    return any(re.fullmatch(p, t, re.I | re.S) for p in policy.boilerplate_patterns)


# ---------------------------------------------------------------- target values
def _value_pattern(value: str) -> re.Pattern[str]:
    v = value.strip()
    if re.fullmatch(r"[0-9a-f]{7,40}", v, re.I):  # commit hashes: allow the longer form of a short sha
        return re.compile(rf"(?<![0-9a-z]){re.escape(v)}[0-9a-f]*(?![0-9a-z])", re.I)
    return re.compile(rf"(?<![\w.-]){re.escape(v)}(?![\w-]|\.\w)", re.I)


def mentions(text: str, value: str, aliases: tuple[str, ...] = ()) -> bool:
    return any(_value_pattern(v).search(text) for v in (value, *aliases) if v)


def check_scope(text: str, target: dict[str, str], required_fields: list[str], policy: Policy) -> dict[str, Any]:
    """Necessary-condition check: does the text state every required target value,
    and does it also mention a conflicting value (e.g. another environment)?

    It cannot prove applicability (only the reader can), but evidence that fails it
    cannot be treated as applicable.
    """
    fields: dict[str, str] = {}
    conflicts: list[str] = []
    for f, value in target.items():
        aliases = policy.value_aliases.get(str(value).lower(), ())
        stated = mentions(text, str(value), aliases)
        others = [c for c in policy.conflicting_values.get(f, ())
                  if c != str(value).lower() and c not in aliases and mentions(text, c)]
        if others:
            conflicts.append(f"{f}: also mentions {', '.join(others)}")
        fields[f] = ("stated" if stated else "not stated") + (f" (also mentions {', '.join(others)})" if others else "")
    missing = [f for f in required_fields if not fields.get(f, "").startswith("stated")]
    return {"fields": fields, "required_fields": required_fields, "missing_required": missing,
            "conflicts": conflicts, "all_required_stated": not missing and not conflicts}


# ---------------------------------------------------------------- injection
# Generic signals of text addressing an automated reviewer or trying to change rules.
# Detection is best-effort and only flags; safety comes from structural guardrails.
INJECTION_PATTERNS = [re.compile(p, re.I) for p in (
    r"\b(automated|ai|llm|bot)\s+(release\s+)?(reviewer|review|assistant|agent|model)s?\b",
    r"\bignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions|rules|guidance)",
    r"\b(report|mark|record|treat)\s+\S+(\s+\S+)?\s+as\s+(pass(ed)?|ready|approved|satisfied)\b",
    r"\bdo\s+not\s+(mention|report|disclose|flag)\b",
    r"\b(system|developer)\s+prompt\b",
    r"\byou\s+are\s+(an?|the)\s+(ai|assistant|model|agent|language model)\b",
    r"\b(new|updated|override)\s+(instructions|rules)\b",
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
