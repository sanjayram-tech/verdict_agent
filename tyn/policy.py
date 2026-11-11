"""Assessment policy (roles, scope fields, value aliases) loaded from policy.yaml.

Keeping domain knowledge in configuration lets the code's guardrails stay generic:
another organisation edits the policy file, not the code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[1] / "policy.yaml"


@dataclass(frozen=True)
class Policy:
    verifier_roles: tuple[str, ...] = ()
    approver_roles: tuple[str, ...] = ()
    criterion_rules: tuple[dict[str, str], ...] = ()
    required_scope_fields: tuple[str, ...] = ("release", "commit", "service", "environment")
    conditional_scope_fields: dict[str, tuple[str, ...]] = field(default_factory=dict)
    value_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    conflicting_values: dict[str, tuple[str, ...]] = field(default_factory=dict)
    boilerplate_patterns: tuple[str, ...] = ()
    collapse_repeats_min: int = 3

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Policy":
        p = Path(path) if path else DEFAULT_POLICY_PATH
        data: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Policy":
        def tup(v: Any) -> tuple[str, ...]:
            return tuple(str(x) for x in (v or []))
        pol = cls(
            verifier_roles=tup(d.get("verifier_roles")),
            approver_roles=tup(d.get("approver_roles")),
            criterion_rules=tuple(dict(r) for r in (d.get("criterion_rules") or [])),
            required_scope_fields=tup(d.get("required_scope_fields") or cls.required_scope_fields),
            conditional_scope_fields={k: tup(v) for k, v in (d.get("conditional_scope_fields") or {}).items()},
            value_aliases={str(k).lower(): tuple(a.lower() for a in tup(v))
                           for k, v in (d.get("value_aliases") or {}).items()},
            conflicting_values={k: tuple(a.lower() for a in tup(v))
                                for k, v in (d.get("conflicting_values") or {}).items()},
            boilerplate_patterns=tup(d.get("boilerplate_patterns")),
            collapse_repeats_min=int(d.get("collapse_repeats_min", 3)),
        )
        for pat in pol.boilerplate_patterns:
            re.compile(pat)  # fail fast on invalid regexes
        return pol

    # ------------------------------------------------------------------ helpers
    def required_fields(self, target: dict[str, str], criterion: str) -> list[str]:
        crit = criterion.lower()
        fields = [f for f in self.required_scope_fields if f in target]
        for f, words in self.conditional_scope_fields.items():
            if f in target and f not in fields and any(w.lower() in crit for w in words):
                fields.append(f)
        return fields

    def required_approval_roles(self, criterion: str) -> list[str]:
        crit = criterion.lower()
        return [r["require_approval_role"] for r in self.criterion_rules
                if r.get("when_criterion_mentions", "").lower() in crit and r.get("require_approval_role")]

    def is_authoritative(self, kind: str, role: str | None) -> bool:
        roles = {r.lower() for r in (self.verifier_roles if kind == "verified_result"
                                     else self.approver_roles if kind == "approval" else ())}
        return bool(role) and role.lower() in roles
