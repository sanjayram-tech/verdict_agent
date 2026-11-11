"""Run checkpoint: lets an interrupted or quota-limited run be resumed into the same run folder.

`checkpoint.json` is rewritten atomically after every requirement, so whatever stops the
run (Ctrl+C, crash, exhausted quota), finished requirements are never lost. On resume,
requirements with a validated decision are reused; requirements that ended in an agent
failure (quota, network, loop, budget) or were never reached are assessed again.
"""
from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent import ReqResult

CHECKPOINT_FILE = "checkpoint.json"
VERSION = 1


def result_to_dict(rr: ReqResult) -> dict[str, Any]:
    return dataclasses.asdict(rr)


def result_from_dict(d: dict[str, Any]) -> ReqResult:
    names = {f.name for f in dataclasses.fields(ReqResult)}
    return ReqResult(**{k: v for k, v in d.items() if k in names})


class CheckpointError(RuntimeError):
    pass


class Checkpoint:
    def __init__(self, run_dir: Path, data: dict[str, Any]):
        self.run_dir = run_dir
        self.path = run_dir / CHECKPOINT_FILE
        self.data = data

    # ------------------------------------------------------------------ create / load
    @classmethod
    def create(cls, run_dir: Path, project: str, label: str, release: str, only: list[str] | None) -> "Checkpoint":
        run_dir.mkdir(parents=True, exist_ok=True)
        cp = cls(run_dir, {"version": VERSION, "project": project, "label": label, "release": release,
                           "only": only, "target": None, "sessions": [], "requirements": {}})
        cp._write()
        return cp

    @classmethod
    def load(cls, run_dir: Path) -> "Checkpoint":
        path = run_dir / CHECKPOINT_FILE
        if not path.exists():
            raise CheckpointError(f"No {CHECKPOINT_FILE} in {run_dir}; only runs started with this version can be resumed")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != VERSION:
            raise CheckpointError(f"Unsupported checkpoint version {data.get('version')}")
        return cls(run_dir, data)

    def check_matches(self, project: str | None, label: str | None, release: str | None) -> None:
        for key, given in (("project", project), ("label", label), ("release", release)):
            if given and given != self.data[key]:
                raise CheckpointError(f"--{key} {given} does not match the run being resumed ({self.data[key]})")

    # ------------------------------------------------------------------ state used by the agent
    def resume_state(self) -> dict[str, Any]:
        return {"target": self.data.get("target"),
                "results": {k: result_from_dict(v) for k, v in self.data["requirements"].items()},
                "sessions": list(self.data["sessions"])}

    def completed(self) -> list[str]:
        return sorted(k for k, v in self.data["requirements"].items() if not v.get("agent_failure"))

    # ------------------------------------------------------------------ writes
    def set_target(self, target: dict[str, str] | None) -> None:
        if target and not self.data.get("target"):
            self.data["target"] = target
            self._write()

    def save_result(self, rr: ReqResult) -> None:
        self.data["requirements"][rr.id] = result_to_dict(rr)
        self._write()

    def record_session(self, session: dict[str, Any]) -> None:
        self.data["sessions"].append(session)
        self._write()

    def _write(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written checkpoint


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
