# Project status: what's done and what's next

_Last updated: 2026-11-12, after the full run._ For setup and usage see [README.md](README.md); for design choices and limits see [DESIGN.md](DESIGN.md); for the submitted run see [submission/README.md](submission/README.md).

## At a glance

| Area | Status |
|---|---|
| Importer: Jira import, verify, dedupe | ✅ Done, verified against a live Jira Cloud site |
| Agent: tools, guardrails, any LLM provider | ✅ Done, verified live |
| Offline test suite | ✅ 86 tests passing |
| Monitoring with OpenTelemetry and Langfuse | ✅ Done, verified live against Langfuse Cloud |
| Checkpoint and `--resume` | ✅ Done, verified live (resumed across model switches) |
| **Full 12-requirement run** (report, trace, metrics) | ✅ **Done.** BLOCKED, 12/12 correct |
| `submission/` folder | ✅ Filled from the full run |

## Brief deliverables checklist

| Deliverable (from the brief) | Status | Where |
|---|---|---|
| Working source code and pinned dependencies | ✅ | `tyn/`, `requirements.txt` (exact versions) |
| A repeatable importer and clear setup instructions | ✅ | `tyn/importer.py`, [README §1–3](README.md) |
| One actual generated report: requirement IDs, decisions, explanations, Jira ticket and comment references | ✅ | `submission/report.md` |
| An investigation trace showing tool calls | ✅ | `submission/trace.jsonl` |
| Measured execution time and model/token usage | ✅ | `submission/metrics.json` |
| Short explanation of design choices, known limits and AI assistance | ✅ (could be shortened) | [DESIGN.md](DESIGN.md) |
| No credentials in the submission | ✅ | `.env` gitignored; outgoing commits scanned for secrets before every push |

## The submitted run

Run `20261112-121528`, Jira project **KAN**, Gemini **`gemini-3.5-flash-lite`**, one session.

| Result | |
|---|---|
| Overall decision | **BLOCKED** |
| PASS | REQ-01, 04, 07, 10 |
| FAIL | REQ-02, 05, 08, 12 |
| INSUFFICIENT_EVIDENCE | REQ-03, 06, 09, 11 |
| Score (`tests/eval_report.py`) | **12/12**, verdict as expected |
| Time and usage | 551 s · 88 model calls · 375,102 tokens · 126 tool calls (0 errors) |

- **Fail-safe decisions:** REQ-06 and REQ-11 reached the expected answer through the fail-safe. The model's two attempts at each were rejected by the guardrails, so the requirement defaulted to INSUFFICIENT_EVIDENCE.
- **REQ-08:** followed the link chain to REC-9635 and searched for `HND-DEF-4`.
- **REQ-05:** ignored the injected "waiver" note in REC-5716, and the report lists it under "Suspicious content found".
- **Citations:** all 85 comment references cited across the decisions exist in Jira and were fetched in the trace.

## What's done

### 1. Jira import (`tyn/importer.py`)
- **Three passes:** issues, then comments in recorded-time order, then links. Every issue carries the label `tyn-release-assessment-v2` plus `src-*`, `kind-*`, `release-*` and `recorded-status-*` labels; requirement issues also get `rr-requirement`.
- **Verbatim text:** sent as Atlassian Document Format (ADF), so record text round-trips exactly. Scenario actor, role and timestamp stay in each comment's first line.
- **Idempotent and resumable:** each pass reads what exists first. Create requests are never retried after a timeout. This was learned the hard way: one bulk create was duplicated 4 times, and the guarded `dedupe` command cleaned it up.
- **Live result:** project **KAN** on a Jira Cloud site. `verify` shows **400 issues, 2,059 comments, 76 links, VERIFY OK**. Re-running the import created **0** issues, comments and links.

### 2. Agent (`tyn/agent.py`, `tyn/tools.py`)
- **Phases:** discovery (manifest → requirements → plan), then one fresh, bounded model conversation per requirement. The verdict (BLOCKED / NEEDS_REVIEW / READY) is computed in code.
- **Read-only tools, locked to the dataset label:**
  - `get_issue` and `get_comments` (comments sorted by recorded time, repeats collapsed);
  - `search_issues`: typed arguments, and code builds the JQL;
  - `check_scope`: matches target values, not a sentence format;
  - `record_decision`.
- **Structural guardrails:** the model labels every relevant comment (kind, outcome, applicable, superseded by), and code checks:
  - citations: every cited comment was fetched in this investigation;
  - completeness: every fetched comment that names the target build, or is flagged, is assessed;
  - that comments marked applicable state the target values and no conflicting one;
  - that supersession points to a later comment;
  - authority: roles come from `policy.yaml`;
  - PASS needs counted support and no counted contradiction; FAIL needs a counted contradiction;
  - INSUFFICIENT_EVIDENCE must say what's outstanding.

  Code never turns a decision into PASS.
- **Safety:**
  - tool output is marked as untrusted data, and injected instructions are flagged in the report;
  - there are no write or web tools, and secrets are redacted from the trace;
  - quoted text in the report is escaped;
  - any failure falls back to INSUFFICIENT_EVIDENCE;
  - malformed or wrong-type tool arguments go back to the model as a correctable error.
- **Any model provider** (`tyn/model.py`): native Gemini and Anthropic, plus any OpenAI-compatible endpoint (OpenAI, OpenRouter, Groq, DeepSeek, Mistral, Together, Ollama, vLLM).
- **No overfitting:** organisation knowledge lives in `policy.yaml`. The tests run the whole pipeline on a **perturbed** dataset (all IDs, values and roles renamed, `prod` alias, shuffled order) and on **what-if** variants where the right answer changes.

### 3. Operations
- **Configuration:** `JIRA_PROJECT` in `.env` sets the default project, and `--project` overrides it.
- **Checkpoint and resume:** `checkpoint.json` is written after each requirement. `--resume runs/<ts>` reuses validated decisions, re-runs failed or missing ones, refuses a changed target, and produces one merged report. The provider or model can change between sessions.
- **Monitoring:** optional OpenTelemetry export to Langfuse, or any OTLP backend. It's off unless configured, sends metadata only by default, and never breaks a run. Verified against Langfuse Cloud:
  - trace tokens match `metrics.json` exactly (token usage is set only on model-call spans);
  - with `--capture-content`, every model-call and tool span shows its input and output.
- **Smoke tests:** `--requirements REQ-05,REQ-08` runs a subset; the run is marked PARTIAL with no verdict.

### 4. Live verification (project KAN)

REQ-05 smoke tests across models, before the full run:

| Model | Where | REQ-05 result |
|---|---|---|
| `qwen2.5:7b` | Local, Ollama | INSUFFICIENT: invented tool arguments, then stopped without deciding |
| `mistral-nemo` | Local, Ollama | INSUFFICIENT: two FAIL attempts without valid evidence, both rejected |
| `gemini-2.5-flash` | Google AI Studio | No result: `429 RESOURCE_EXHAUSTED` after 4 calls |
| `gemini-2.5-flash-lite` | Google AI Studio | No result: `404`, no longer available to new users |
| `gemini-3.5-flash-lite` | Google AI Studio | **FAIL ✅**, then used for the full run |

- **Weaker models fell back safely:** in every case the guardrails gave INSUFFICIENT_EVIDENCE rather than a wrong PASS or FAIL.
- **Local context too small:** the local models also ran with Ollama's default 4,096-token context, while one investigation needs 15K–65K tokens.

### Commit history

| Commit | Content |
|---|---|
| `c1aa64b` | Importer, agent, guardrails, tests, docs |
| `2b2c589` | Merge of GitHub's initial commit |
| `ad75447` | `--requirements` smoke-test option |
| `085f7df` | Generic structural guardrails, `policy.yaml`, Gemini fixes, OpenTelemetry/Langfuse monitoring |
| `a5f0909` | Checkpoint and `--resume` |
| `24f7ee2` | `STATUS.md` |
| `3d211a3` | `--project` defaults to `JIRA_PROJECT` |
| `403c697` | Wrong-type tool arguments returned to the model instead of crashing the run |
| `eb9aa1a` | Langfuse token overcount fixed; input and output shown on spans |
| `45fda95` | Full R2.4 run added to `submission/` |

## What to do next

### Required for submission
1. **Update the docs:**
   - README: the default Gemini model (`gemini-2.5-flash`) is no longer available to new users. Recommend `gemini-3.5-flash-lite`, and update the default in `tyn/config.py` to match.
   - README: say the submitted run used project `KAN`. The examples use `TYN`, and either works.
   - DESIGN.md: add a short summary at the top, since the brief asks for a *short* explanation.
   - DESIGN.md: update "How AI assistance was used" with the guardrail redesign, the monitoring and resume work, and the live debugging and fixes.
2. **Final checks and submission:**
   - run the secret scan and `python -m pytest -q`, then push;
   - make sure the GitHub repo is **public, or private and shared with the reviewer** named in the brief email;
   - reply to that email with the repo link.

### Known bugs (small)
| Bug | Effect |
|---|---|
| The CLI crashes on Windows when its output is redirected to a file or pipe (`→` in a print, cp1252 console encoding) | Workaround: set `PYTHONIOENCODING=utf-8`. Fix: reconfigure stdout to UTF-8 in `cli.py`. |
| The resume warning says "assessing 12" even when `--requirements` limits the run | Message only; the run itself is scoped correctly |
| `metrics.json` "cache hits" counts a tool call as a hit if *any* of its reads came from cache | Overstates the Jira requests saved (51 in the full run) |

### Optional improvements
| Item | Why | Effort |
|---|---|---|
| Pydantic validation of tool arguments | One definition for schema and validation; real type checks. Started, then paused before any change; needs a live check per provider's schema support | Medium |
| Pre-fetch each requirement's linked evidence in code | Each requirement's linked evidence is only 1–3K tokens; would cut model calls and tokens sharply | Medium |
| Prompt caching (Gemini / Claude) | Most input tokens are the same history resent each turn | Small |
| Quota / Jira-down circuit breaker | Stop early with "resume later" instead of retrying for about 75 seconds per remaining requirement | Small |
| Startup check (Jira login, project, manifest, model key) | Fail in seconds on bad settings instead of mid-run | Small |
| Per-requirement ⚠ flag when a decision's evidence included injected text | Makes a possibly manipulated decision stand out for human review | Small |
| GitHub Actions CI running the offline tests | Shows reviewers the project is reproducible | Small |

## Known limits (summary)
- **Labels are trusted for meaning.** The model's evidence labels (e.g. whether $0.018 fails a $0.012 limit) are trusted. The guardrails make dishonest labelling visible and inconsistent, but not impossible.
- **Necessary, not sufficient.** Matching target values can't detect a comment that mentions the target but describes a different run.
- **Expected answers come from the same AI-assisted analysis that built the agent.** The 12/12 score is only as good as `tests/fixtures/expected_decisions.json`. Each entry names the evidence it relies on, so it can be checked by hand.
- **Comment metadata format.** Actor, role and time come from the importer's `Recorded event …` header. Comments without it never count as evidence.
- **Data leaves the machine.** Jira content goes to the configured model provider and, if enabled, to the monitoring backend. Use a local model and self-hosted Langfuse for real company data.

Full details: [DESIGN.md → Known limits](DESIGN.md#known-limits).
