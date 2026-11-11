"""Compare a run's decisions.json against the hand-derived expected outcome.

  python tests/eval_report.py runs/<timestamp>/decisions.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EXPECTED = Path(__file__).parent / "fixtures" / "expected_decisions.json"


def main(path: str) -> int:
    got = json.loads(Path(path).read_text(encoding="utf-8"))
    exp = json.loads(EXPECTED.read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in got["requirements"]}
    correct = 0
    print(f"{'REQ':7} {'expected':22} {'got':22} ok")
    for rid, e in exp["decisions"].items():
        g = by_id.get(rid, {}).get("decision", "MISSING")
        ok = g == e["decision"]
        correct += ok
        print(f"{rid:7} {e['decision']:22} {g:22} {'✓' if ok else '✗  (' + e['why'] + ')'}")
    v_ok = got.get("verdict") == exp["verdict"]
    print(f"\nRequirements correct: {correct}/{len(exp['decisions'])}")
    print(f"Verdict: expected {exp['verdict']}, got {got.get('verdict')} {'✓' if v_ok else '✗'}")
    return 0 if correct == len(exp["decisions"]) and v_ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
