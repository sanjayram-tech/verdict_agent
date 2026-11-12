# Submitted run

## Approach

A tool-using AI agent decides whether release R2.4 is ready, using only evidence stored in Jira. The supplied dataset was imported into a Jira Cloud project. The agent never sees the JSON file: it investigates Jira through tools.

- **Discovery.** The agent reads the release manifest and the requirements linked from it. The target build (release, commit, service, environment, prompt) comes from the manifest.
- **One requirement at a time.** Each requirement gets a fresh, bounded model conversation, in which the model chooses its own lookups:
  - `get_issue` and `get_comments`, to read records and follow linked dependencies;
  - `search_issues`, to look for defect closures or retests;
  - `check_scope`, to compare a result with the target build.
- **Structured decisions.** The model records PASS, FAIL or INSUFFICIENT_EVIDENCE together with an evidence assessment. For every relevant comment it fetched, it gives the comment's kind, whether it supports or contradicts the criterion, whether it applies to the target build, and what supersedes it.
- **Verdict in code.** Any FAIL → BLOCKED; otherwise any INSUFFICIENT_EVIDENCE → NEEDS_REVIEW; only all PASS → READY.

**What's implemented:**
- **Guardrails:** code checks every decision against the evidence the model fetched (scope, authority, supersession, completeness), and falls back to INSUFFICIENT_EVIDENCE rather than ever forcing a PASS.
- **Safety:** Jira text is treated as data, not instructions, so injected notes are flagged and ignored. The agent can't write to Jira or browse the web, and secrets are redacted.
- **Context control:** queries are locked to the dataset label, boilerplate comments are collapsed, and each run has budgets.
- **Robustness:** works with any model provider, retries failures, and can resume an interrupted run. Langfuse monitoring is optional.

**Result:** **BLOCKED**, with 4 PASS, 4 FAIL and 4 INSUFFICIENT_EVIDENCE. It scored 12/12 against the hand-derived expected answers (`python tests/eval_report.py submission/decisions.json`).

## Metrics

| Metric | Value |
|---|---|
| Elapsed time | 551 s (9.2 min) |
| Model calls | 88 |
| Tokens | 362,357 input · 12,745 output · 375,102 total |
| Most expensive requirement | REQ-12: 65,213 tokens, 13 model turns, 15 tool calls |

## Files

- **`report.md`**: the release-readiness report:
  - the overall decision and a summary table;
  - for each requirement: the decision, explanation, evidence assessment table, cited Jira tickets and comments (with recorded actor, role and time), and any guardrail rejections;
  - suspicious content found, failures and warnings, and run metrics.
- **`trace.jsonl`**: the investigation trace. Every model turn and tool call in order, with arguments, result summaries, latency, retries and guardrail rejections. Secrets are redacted.
- **`metrics.json`**: measured execution time, model, token usage, model and tool call counts, and per-requirement figures.
- **`decisions.json`**: machine-readable decisions with their evidence assessments, used for scoring.
