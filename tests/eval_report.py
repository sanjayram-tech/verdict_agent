"""Compare a run's decisions.json against an answer key (hand-derived expected outcome).

  python tests/eval_report.py runs/<timestamp>/decisions.json
  python tests/eval_report.py runs/<timestamp>/decisions.json --expected my_expected.json

The default answer key is tests/fixtures/expected_decisions.json (for the supplied dataset).
For another dataset, write a key in the same shape:
  {"verdict": "BLOCKED", "decisions": {"<requirement id>": {"decision": "PASS", "why": "..."}, ...}}
Requirements in the run but not in the key are reported as unscored.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_EXPECTED = Path(__file__).parent / "fixtures" / "expected_decisions.json"


def main(path: str, expected_path: str | Path = DEFAULT_EXPECTED) -> int:
    got = json.loads(Path(path).read_text(encoding="utf-8"))
    exp = json.loads(Path(expected_path).read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in got["requirements"]}
    width = max([len("REQ")] + [len(k) for k in exp["decisions"]] + [len(k) for k in by_id])
    correct = 0
    print(f"{'REQ':{width}} {'expected':22} {'got':22} ok")
    for rid, e in exp["decisions"].items():
        g = by_id.get(rid, {}).get("decision", "MISSING")
        ok = g == e["decision"]
        correct += ok
        print(f"{rid:{width}} {e['decision']:22} {g:22} {'✓' if ok else '✗  (' + e.get('why', '') + ')'}")
    unscored = sorted(set(by_id) - set(exp["decisions"]))
    if unscored:
        print(f"\nUnscored (not in the answer key): {', '.join(unscored)}")
    v_ok = got.get("verdict") == exp.get("verdict")
    print(f"\nRequirements correct: {correct}/{len(exp['decisions'])}")
    print(f"Verdict: expected {exp.get('verdict')}, got {got.get('verdict')} {'✓' if v_ok else '✗'}")
    return 0 if correct == len(exp["decisions"]) and v_ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("decisions", help="runs/<timestamp>/decisions.json")
    ap.add_argument("--expected", default=str(DEFAULT_EXPECTED), help="answer key JSON")
    a = ap.parse_args()
    sys.exit(main(a.decisions, a.expected))
