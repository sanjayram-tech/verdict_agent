"""Run the release-readiness assessment.

  python -m tyn.cli assess --project TYN --label tyn-release-assessment-v2 --release R2.4 [--out runs]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .agent import Agent, Budgets, Tracer
from .config import ConfigError, JiraConfig, ModelConfig, secret_values
from .jira_client import JiraReader
from .model import PROVIDERS, build_model
from .policy import Policy
from .report import write_outputs
from .tools import Toolbox


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tyn.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("assess", help="assess release readiness from Jira evidence")
    a.add_argument("--project", required=True, help="Jira project key")
    a.add_argument("--label", required=True, help="dataset scope label, e.g. tyn-release-assessment-v2")
    a.add_argument("--release", required=True, help="target release, e.g. R2.4")
    a.add_argument("--out", default="runs", help="output directory (a timestamped subfolder is created)")
    a.add_argument("--provider", default=None, choices=PROVIDERS, help="override LLM_PROVIDER")
    a.add_argument("--model", default=None, help="override LLM_MODEL")
    a.add_argument("--policy", default=None, help="policy file (default: policy.yaml in the repo root)")
    a.add_argument("--requirements", default=None,
                   help="comma-separated subset to assess, e.g. REQ-05,REQ-08 (smoke test; no overall verdict)")
    a.add_argument("--no-telemetry", action="store_true",
                   help="disable OpenTelemetry export even if Langfuse/OTLP settings are present")
    a.add_argument("--capture-content", action="store_true",
                   help="include (redacted) tool arguments/results and model text in telemetry spans")
    a.add_argument("--max-tool-calls-per-req", type=int, default=Budgets.max_tool_calls_per_req)
    a.add_argument("--max-wall-s", type=float, default=Budgets.max_wall_s)
    args = ap.parse_args(argv)

    try:
        jcfg = JiraConfig.from_env()
        mcfg = ModelConfig.from_env(args.provider, args.model)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    out_dir = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    telemetry = None
    if not args.no_telemetry:
        try:
            from .telemetry import Telemetry
            telemetry = Telemetry.from_env(run_id=f"{args.release}-{out_dir.name}",
                                           capture_override=True if args.capture_content else None)
        except Exception as exc:  # noqa: BLE001 - monitoring is optional
            print(f"  telemetry not started: {type(exc).__name__}: {exc}", file=sys.stderr)
    if telemetry:
        print(f"  telemetry: exporting OpenTelemetry traces (content capture "
              f"{'on' if telemetry.capture else 'off'}); session {args.release}-{out_dir.name}")
    tracer = Tracer(out_dir / "trace.jsonl", secret_values(), sink=telemetry)

    def on_retry(what: str, attempt: int, wait: float) -> None:
        tracer.emit("retry", target=what, attempt=attempt, wait_s=wait)
        print(f"  retry {attempt} ({what}) in {wait:.0f}s", file=sys.stderr)

    jira = JiraReader(jcfg, on_retry=on_retry)
    if mcfg.provider == "anthropic" and mcfg.temperature is not None:
        print("  note: LLM_TEMPERATURE is ignored for anthropic (current Claude models reject sampling params)",
              file=sys.stderr)
    model = build_model(mcfg, on_retry=on_retry)
    toolbox = Toolbox(jira, args.project, args.label, args.release, policy=Policy.load(args.policy))
    budgets = Budgets(max_tool_calls_per_req=args.max_tool_calls_per_req, max_wall_s=args.max_wall_s)
    print(f"Assessing {args.release} in {args.project} (label {args.label}) with "
          f"{mcfg.provider}:{model.name} → {out_dir}")
    only = [r.strip().upper() for r in args.requirements.split(",") if r.strip()] if args.requirements else None
    try:
        run = Agent(toolbox, model, tracer, budgets, only=only).run()
    finally:
        if telemetry:
            telemetry.shutdown()  # flush spans even if the run crashed
    write_outputs(run, out_dir, toolbox.comment_index, args.project, args.label, jcfg.base_url)

    for r in run.requirements:
        print(f"  {r.id:7} {r.decision:22} {'(agent failure: ' + r.agent_failure + ')' if r.agent_failure else ''}")
    print(f"Overall: {run.verdict or run.status}   wall {run.metrics['wall_time_s']}s   "
          f"tokens {run.metrics['tokens']['total']}   tool calls {run.metrics['tool_calls']}")
    print(f"Report: {out_dir / 'report.md'}")
    return 0 if run.status in ("COMPLETE", "PARTIAL") else 1


if __name__ == "__main__":
    sys.exit(main())
