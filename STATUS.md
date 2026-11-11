# Project status: what's done and what's next

_Last updated: 2026-11-12._ For setup and usage see [README.md](README.md); for design choices and limits see [DESIGN.md](DESIGN.md).

## At a glance

| Area | Status |
|---|---|
| Importer: Jira import, verify, dedupe | ✅ Done, and verified against a live Jira Cloud site |
| Agent: tools, guardrails, any LLM provider | ✅ Done; parts verified live (see below) |
| Offline test suite | ✅ 78 tests passing |
| Monitoring with OpenTelemetry and Langfuse | ✅ Done; tested offline only |
| Checkpoint and `--resume` | ✅ Done; tested offline only |
| **Full 12-requirement run** (report, trace, metrics) | ❌ **Not done.** Blocked by the Gemini free-tier daily quota |
| `submission/` folder | ❌ Placeholder only, waiting on the full run |

## Brief deliverables checklist

| Deliverable (from the brief) | Status | Where |
|---|---|---|
| Working source code and pinned dependencies | ✅ | `tyn/`, `requirements.txt` (exact versions) |
| A repeatable importer and clear setup instructions | ✅ | `tyn/importer.py`, [README §1–3](README.md) |
| One actual generated report: requirement IDs, decisions, explanations, Jira ticket and comment references | ❌ | Will go in `submission/report.md` |
| An investigation trace showing tool calls | ❌ (smoke runs only) | Will go in `submission/trace.jsonl` |
| Measured execution time and model/token usage | ❌ (smoke runs only) | Will go in `submission/metrics.json` |
| Short explanation of design choices, known limits and AI assistance | ✅ (could be shortened) | [DESIGN.md](DESIGN.md) |
| No credentials in the submission | ✅ | `.env` gitignored; staged changes scanned for secrets before every commit |

## What's done

### 1. Jira import (`tyn/importer.py`)
- **Three passes:** issues, then comments in recorded-time order, then links. Every issue carries the label `tyn-release-assessment-v2` plus `src-*`, `kind-*`, `release-*` and `recorded-status-*` labels; requirement issues also get `rr-requirement`.
- **Verbatim text:** sent as Atlassian Document Format (ADF), so record text round-trips exactly. Scenario actor, role and timestamp stay in each comment's first line.
- **Idempotent and resumable:** each pass reads what exists first. Create requests are never retried after a timeout. This was learned the hard way: one bulk create was duplicated 4 times, and the guarded `dedupe` command cleaned it up.
- **Live result:** imported into project **KAN** on a Jira Cloud site. `verify` shows **400 issues, 2,059 comments, 76 links, VERIFY OK**, and searching comment text works. A second import run created **0** issues, comments and links.

### 2. Agent (`tyn/agent.py`, `tyn/tools.py`)
- **Phases:** discovery (manifest → requirements → plan), then one fresh, bounded model conversation per requirement. The verdict (BLOCKED / NEEDS_REVIEW / READY) is computed in code.
- **Read-only tools, locked to the dataset label:**
  - `get_issue` and `get_comments` (comments sorted by recorded time, repeats collapsed);
  - `search_issues`: typed arguments, and code builds the JQL;
  - `check_scope`: matches target values, not a sentence format;
  - `record_decision`.
- **Structural guardrails:** the model labels every relevant comment (kind, outcome, applicable, superseded by), and code checks:
  - completeness: every relevant comment must be assessed;
  - that comments marked applicable state the target values;
  - that supersession points to a later comment;
  - authority: roles come from `policy.yaml`;
  - PASS needs counted support and no counted contradiction; FAIL needs a counted contradiction;
  - INSUFFICIENT_EVIDENCE must say what's outstanding.

  Code never turns a decision into PASS.
- **Safety:**
  - tool output is marked as untrusted data, and injected instructions are flagged in the report;
  - there are no write or web tools, and secrets are redacted from the trace;
  - quoted text in the report is escaped;
  - any failure falls back to INSUFFICIENT_EVIDENCE.
- **Any model provider** (`tyn/model.py`): native Gemini and Anthropic, plus any OpenAI-compatible endpoint (OpenAI, OpenRouter, Groq, DeepSeek, Mistral, Together, Ollama, vLLM). Retries and backoff are shared, and a malformed tool call is recoverable.
- **No overfitting:** organisation knowledge lives in `policy.yaml`. The tests run the whole pipeline on a **perturbed** dataset (all IDs, values and roles renamed, `prod` alias, shuffled order) and on **what-if** variants where the right answer changes.

### 3. Operations
- **Checkpoint and resume:** `checkpoint.json` is written after each requirement. `--resume runs/<ts>` reuses validated decisions, re-runs failed or missing ones, refuses a changed target, and produces one merged report.
- **Monitoring:** optional OpenTelemetry export to Langfuse, or any OTLP backend. It's off unless configured, sends metadata only by default, and never breaks a run.
- **Smoke tests:** `--requirements REQ-05,REQ-08` runs a subset; the run is marked PARTIAL with no verdict.

### 4. Live verification so far (Gemini `gemini-2.5-flash`, project KAN)

| Run | Result | Notes |
|---|---|---|
| REQ-05, REQ-08 | FAIL, FAIL ✅ | REQ-08 followed REQ-08 → REC-4076 → REC-9635 and searched for HND-DEF-4. REQ-05 rejected the injected "waiver" note and listed it in the report. Uses the earlier guardrail version. |
| REQ-07 | PASS ✅ | New structured assessment: REDACT-870 labelled superseded by REDACT-901; REDACT-905 (other service) not applicable. |
| REQ-09 | Not decided ⚠ | First attempt: Gemini `MALFORMED_FUNCTION_CALL` (now handled, tested offline). Re-run: blocked by `429 RESOURCE_EXHAUSTED`, the free-tier daily quota. |

Smoke runs averaged **about 5 model calls and about 10k tokens per requirement**.

### Commit history

| Commit | Content |
|---|---|
| `c1aa64b` | Importer, agent, guardrails, tests, docs |
| `2b2c589` | Merge of GitHub's initial commit |
| `ad75447` | `--requirements` smoke-test option |
| `085f7df` | Generic structural guardrails, `policy.yaml`, Gemini fixes, OpenTelemetry/Langfuse monitoring |
| `a5f0909` | Checkpoint and `--resume` |

## What to do next

### Required for submission (in order)
1. **Get model quota for the full run.** Options:
   - wait for the Gemini free-tier daily reset (midnight Pacific);
   - enable billing on the key;
   - set another provider in `.env` (a stronger model is a good idea for the submitted report).

   On a free tier, set `LLM_MIN_INTERVAL_S=12`.
2. **Run the full assessment:**
   ```bash
   python -m tyn.cli assess --project KAN --label tyn-release-assessment-v2 --release R2.4
   ```
   If it stops partway (e.g. quota), finish the same run later:
   ```bash
   python -m tyn.cli assess --resume runs/<timestamp>
   ```
3. **Score and inspect the run:**
   - Score it:
     ```bash
     python tests/eval_report.py runs/<timestamp>/decisions.json
     ```
     The expected result is 12/12 and **BLOCKED**:
     - PASS: REQ-01, 04, 07 and 10;
     - FAIL: REQ-02, 05, 08 and 12;
     - INSUFFICIENT_EVIDENCE: REQ-03, 06, 09 and 11.
   - In the trace, check that REQ-08 reached REC-9635, that REQ-05 ignored the injection, and that the report lists REC-5716 under "Suspicious content found".
   - **Report any mismatch honestly** rather than tuning the agent to the expected answers.
4. **Fill `submission/`:**
   - copy `report.md`, `trace.jsonl`, `metrics.json` and `decisions.json` into it;
   - replace the placeholder `submission/README.md` with the run details (project KAN, provider and model, date, eval score, sessions if resumed).
5. **Update the docs:**
   - README: say the submitted run used project `KAN`. The examples use `TYN`, and either works.
   - DESIGN.md: add a short summary at the top, since the brief asks for a *short* explanation.
   - DESIGN.md: update "How AI assistance was used" with the guardrail redesign and the monitoring and resume work.
6. **Final checks and submission:**
   - run the secret scan and `python -m pytest -q`, then push;
   - make sure the GitHub repo is **public, or private and shared with the reviewer** named in the brief email;
   - reply to that email with the repo link.

### Optional improvements
| Item | Why | Effort |
|---|---|---|
| Quota / Jira-down circuit breaker | Stop early with "resume later" instead of retrying for about 75 seconds per remaining requirement | Small |
| Startup check (Jira login, project, manifest, model key) | Fail in seconds on bad settings instead of mid-run | Small |
| Per-requirement ⚠ flag when a decision's evidence included injected text | Makes a possibly manipulated decision stand out for human review | Small |
| GitHub Actions CI running the offline tests | Shows reviewers the project is reproducible | Small |
| Live check of Langfuse export | Only tested offline so far; needs Langfuse keys | Small |
| Static HTML report (evidence tables, filters, tool-call timeline) | Nicer review experience; not required by the brief | About half a day |

## Known limits (summary)
- **Labels are trusted for meaning.** The model's evidence labels (e.g. whether $0.018 fails a $0.012 limit) are trusted. The guardrails make dishonest labelling visible and inconsistent, but not impossible.
- **Necessary, not sufficient.** Matching target values can't detect a comment that mentions the target but describes a different run.
- **Comment metadata format.** Actor, role and time come from the importer's `Recorded event …` header. Comments without it never count as evidence.
- **Data leaves the machine.** Jira content goes to the configured model provider and, if enabled, to the monitoring backend. Use a local model and self-hosted Langfuse for real company data.

Full details: [DESIGN.md → Known limits](DESIGN.md#known-limits).
