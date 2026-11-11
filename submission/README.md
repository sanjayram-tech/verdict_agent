# Submitted run

This folder holds the outputs of one real assessment run against the imported Jira project. They are copied from `runs/<timestamp>/`:

- `report.md`: the release-readiness report (decisions, explanations, Jira ticket and comment references, outstanding items).
- `trace.jsonl`: every model turn and tool call, with arguments, result summaries, latency, retries and guardrail rejections.
- `metrics.json`: wall time, model, token usage, and tool and model call counts.
- `decisions.json`: machine-readable decisions. Score them with `python tests/eval_report.py submission/decisions.json`.

_Pending: the real run needs Jira and Gemini credentials in `.env`. See the top-level README._
