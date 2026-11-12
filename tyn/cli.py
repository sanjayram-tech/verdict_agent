"""Run the release-readiness assessment.

  python -m tyn.cli assess [--project KAN] --label tyn-release-assessment-v2 --release R2.4 [--out runs]
  python -m tyn.cli assess --resume runs/<timestamp>      # continue an interrupted / quota-limited run
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .agent import Agent, Budgets, ReqResult, Tracer
from .checkpoint import Checkpoint, CheckpointError
from .config import ConfigError, JiraConfig, ModelConfig, default_project, secret_values
from .jira_client import JiraReader
from .model import PROVIDERS, build_model
from .policy import Policy
from .report import write_outputs
from .tools import Toolbox


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tyn.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("assess", help="assess release readiness from Jira evidence")
    a.add_argument("--project", help="Jira project key (default: JIRA_PROJECT; required unless --resume)")
    a.add_argument("--label", help="dataset scope label, e.g. tyn-release-assessment-v2 (required unless --resume)")
    a.add_argument("--release", help="target release, e.g. R2.4 (required unless --resume)")
    a.add_argument("--resume", default=None, metavar="RUN_DIR",
                   help="continue the run in RUN_DIR: keeps validated decisions, re-assesses failed or missing ones")
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
    a.add_argument("--max-wall-s", type=float, default=None,
                   help="run wall-clock limit in seconds (default: scales with the number of requirements)")
    a.add_argument("--max-total-tool-calls", type=int, default=None,
                   help="run-wide tool-call limit (default: scales with the number of requirements)")
    args = ap.parse_args(argv)

    # ---- run folder: new, or the one being resumed
    try:
        if args.resume:
            if args.requirements:
                raise CheckpointError("--requirements cannot be combined with --resume (the original scope is kept)")
            out_dir = Path(args.resume)
            ckpt = Checkpoint.load(out_dir)
            ckpt.check_matches(args.project, args.label, args.release)
            project, label, release, only = (ckpt.data["project"], ckpt.data["label"], ckpt.data["release"],
                                             ckpt.data.get("only"))
        else:
            args.project = args.project or default_project()
            missing = [f"--{n}" for n in ("project", "label", "release") if not getattr(args, n)]
            if missing:
                missing = [("--project (or JIRA_PROJECT)" if m == "--project" else m) for m in missing]
                raise CheckpointError(f"{', '.join(missing)} required (or use --resume RUN_DIR)")
            project, label, release = args.project, args.label, args.release
            only = [r.strip().upper() for r in args.requirements.split(",") if r.strip()] if args.requirements else None
            out_dir = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
            ckpt = None
        jcfg = JiraConfig.from_env()
        mcfg = ModelConfig.from_env(args.provider, args.model)
    except (ConfigError, CheckpointError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if ckpt is None:
        ckpt = Checkpoint.create(out_dir, project, label, release, only)

    # ---- monitoring (same session id across resumed sessions)
    run_id = f"{release}-{out_dir.name}"
    telemetry = None
    if not args.no_telemetry:
        try:
            from .telemetry import Telemetry
            telemetry = Telemetry.from_env(run_id=run_id, capture_override=True if args.capture_content else None)
        except Exception as exc:  # noqa: BLE001 - monitoring is optional
            print(f"  telemetry not started: {type(exc).__name__}: {exc}", file=sys.stderr)
    if telemetry:
        print(f"  telemetry: exporting OpenTelemetry traces (content capture "
              f"{'on' if telemetry.capture else 'off'}); session {run_id}")
    tracer = Tracer(out_dir / "trace.jsonl", secret_values(), sink=telemetry, append=bool(args.resume))

    def on_retry(what: str, attempt: int, wait: float) -> None:
        tracer.emit("retry", target=what, attempt=attempt, wait_s=wait)
        print(f"  retry {attempt} ({what}) in {wait:.0f}s", file=sys.stderr)

    jira = JiraReader(jcfg, on_retry=on_retry)
    if mcfg.provider == "anthropic" and mcfg.temperature is not None:
        print("  note: LLM_TEMPERATURE is ignored for anthropic (current Claude models reject sampling params)",
              file=sys.stderr)
    model = build_model(mcfg, on_retry=on_retry)
    toolbox = Toolbox(jira, project, label, release, policy=Policy.load(args.policy))
    budgets = Budgets(max_tool_calls_per_req=args.max_tool_calls_per_req, max_wall_s=args.max_wall_s,
                      max_total_tool_calls=args.max_total_tool_calls)

    def on_result(rr: ReqResult) -> None:
        ckpt.set_target(toolbox.target)
        ckpt.save_result(rr)

    if args.resume:
        print(f"Resuming {out_dir}: {len(ckpt.completed())} requirement(s) already decided "
              f"({', '.join(ckpt.completed()) or 'none'}); the rest will be assessed")
    print(f"Assessing {release} in {project} (label {label}) with {mcfg.provider}:{model.name} → {out_dir}")
    agent = Agent(toolbox, model, tracer, budgets, only=only, on_result=on_result,
                  resume=ckpt.resume_state() if args.resume else None)
    try:
        run = agent.run()
    finally:
        if telemetry:
            telemetry.shutdown()  # flush spans even if the run crashed
    ckpt.set_target(run.target)
    ckpt.record_session(run.metrics["sessions"][-1])
    write_outputs(run, out_dir, toolbox.comment_index, project, label, jcfg.base_url)

    for r in run.requirements:
        reused = " (from session %d)" % r.session if r.session != agent.session_no else ""
        print(f"  {r.id:7} {r.decision:22}{reused} {'(agent failure: ' + r.agent_failure + ')' if r.agent_failure else ''}")
    print(f"Overall: {run.verdict or run.status}   wall {run.metrics['wall_time_s']}s   "
          f"tokens {run.metrics['tokens']['total']}   tool calls {run.metrics['tool_calls']}")
    print(f"Report: {out_dir / 'report.md'}")
    unfinished = run.status == "INCOMPLETE" or any(r.agent_failure for r in run.requirements)
    if unfinished:
        print(f"Some requirements were not assessed or failed. Resume later with:\n"
              f"  python -m tyn.cli assess --resume {out_dir}")
    return 0 if run.status in ("COMPLETE", "PARTIAL") else 1


if __name__ == "__main__":
    sys.exit(main())
