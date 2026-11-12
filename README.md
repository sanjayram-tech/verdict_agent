# TYN Jira Release Evidence Agent

A tool-using AI agent that decides whether a software release is ready, using **only evidence recorded in Jira**. It reads the release manifest and requirements, follows linked verification records, checks that each result applies to the exact target build, and writes an evidence-backed release-readiness report.

- **Submitted result:** release R2.4 is **BLOCKED** (4 PASS · 4 FAIL · 4 INSUFFICIENT_EVIDENCE), scoring 12/12 against hand-derived answers. See [`submission/`](submission/).
- **Further reading:**
  - [DESIGN.md](DESIGN.md): design choices, known limits and how AI assistance was used.
  - [STATUS.md](STATUS.md): project status and next steps.

## Contents

1. [How it works](#how-it-works)
2. [Quick start](#quick-start)
3. [Prerequisites](#1-prerequisites)
4. [Install](#2-install)
5. [Create the Jira site, project and API token](#3-create-the-jira-site-project-and-api-token)
6. [Configure `.env`](#4-configure-env)
7. [Import the dataset into Jira](#5-import-the-dataset-into-jira)
8. [Run the assessment](#6-run-the-assessment)
9. [Read and score the results](#7-read-and-score-the-results)
10. [Resume an interrupted run](#8-resume-an-interrupted-run)
11. [Choose a model provider](#9-choose-a-model-provider)
12. [Monitoring with Langfuse (optional)](#10-monitoring-with-langfuse-optional)
13. [Policy and how decisions are checked](#11-policy-and-how-decisions-are-checked)
14. [Tests](#12-tests)
15. [Troubleshooting](#13-troubleshooting)
16. [Command reference](#14-command-reference)
17. [Using a different dataset](#15-using-a-different-dataset)
18. [Repository layout](#repository-layout)

---

## How it works

```
inputs/dataset.json ──(1) importer──▶ Jira Cloud project ◀──(2) read-only tools── AI agent ──▶ runs/<timestamp>/
   400 records                        issues + comments +        get_issue, get_comments,          report.md
   2,059 comments                     links, all labelled        search_issues, check_scope,       decisions.json
   76 links                           tyn-release-assessment-v2  record_decision                   trace.jsonl
                                                                                                   metrics.json
```

1. **The importer** copies the supplied dataset into a Jira project. It only needs to run once, and re-running it creates nothing new.
2. **The agent** never sees the JSON file. It investigates Jira through tools:
   - it reads the manifest and every requirement linked from it (the supplied dataset has 12; nothing in the code assumes a count);
   - then, **one requirement at a time**, it follows linked records and reads comments;
   - it records a decision (PASS, FAIL or INSUFFICIENT_EVIDENCE) with a structured evidence assessment.
3. **Code** checks every decision against what the agent actually fetched, then computes the overall verdict: any FAIL → **BLOCKED**; otherwise any INSUFFICIENT → **NEEDS_REVIEW**; only all PASS → **READY**.

---

## Quick start

For readers who already have a Jira Cloud project, a Jira API token and a model API key. Every step is explained below.

```bash
git clone git@github.com:sanjayram-tech/verdict_agent.git
cd verdict_agent
python -m venv .venv
.venv\Scripts\activate                     # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env                     # macOS/Linux: cp .env.example .env
# edit .env: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT, LLM_PROVIDER, LLM_MODEL, LLM_API_KEY

python -m tyn.importer import --dataset inputs/dataset.json          # one-time import (~7 min for the supplied dataset)
python -m tyn.importer verify --dataset inputs/dataset.json          # expect: VERIFY OK
python -m tyn.cli assess --label tyn-release-assessment-v2 --release R2.4
python tests/eval_report.py runs/<timestamp>/decisions.json         # score the run
```

---

## 1. Prerequisites

| You need | Why | Where to get it |
|---|---|---|
| **Python 3.10+** | Runs the importer and agent | https://www.python.org/downloads/ (`python --version` to check) |
| **Git** | To clone the repository | https://git-scm.com/downloads |
| **A Jira Cloud site** (the free plan works) | Holds the imported evidence | https://www.atlassian.com/software/jira/free |
| **A Jira API token** | Lets the importer and agent use the Jira REST API | https://id.atlassian.com/manage-profile/security/api-tokens |
| **A model API key** (one provider) | The agent's reasoning | Gemini: https://aistudio.google.com/apikey · Claude: https://console.anthropic.com · OpenAI, OpenRouter, Groq, … · or a **local model via Ollama** (no key) |
| *(optional)* A Langfuse project | Monitoring dashboards | https://cloud.langfuse.com |

The model must support **tool (function) calling**. All the providers listed in [section 9](#9-choose-a-model-provider) do.

---

## 2. Install

**Windows (PowerShell)**
```powershell
git clone git@github.com:sanjayram-tech/verdict_agent.git
cd verdict_agent
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS / Linux**
```bash
git clone git@github.com:sanjayram-tech/verdict_agent.git
cd verdict_agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

- **Pinned versions:** `requirements.txt` pins exact versions for reproducible installs.
- **Activate the environment** in every new terminal before running commands. Git Bash on Windows uses `source .venv/Scripts/activate`.
- **Check the install** by running `python -m pytest -q`. It should report all tests passing, with no credentials needed.

---

## 3. Create the Jira site, project and API token

1. **Create a site:** sign up at https://www.atlassian.com/software/jira/free and choose a site name, e.g. `my-team`. Your site URL becomes `https://my-team.atlassian.net`. To check the site is running, open `https://my-team.atlassian.net/status`; it should show `{"state":"RUNNING"}`.
2. **Create a project:** in Jira, go to **Projects → Create project → Kanban → Team-managed**.
   - Give it a key, for example `TYN`; the submitted run used `KAN`.
   - It needs the **Task** issue type, which Kanban projects have by default.
   - Decline sample data if offered. Other tickets in the project are fine anyway, because the agent only reads issues carrying the dataset label.
3. **Create an API token:** at https://id.atlassian.com/manage-profile/security/api-tokens, click **Create API token**.
   - Choose the plain token, not "with scopes".
   - Copy it immediately; it's shown only once.
   - Put it **only** in `.env`. Never paste it into chat, code or screenshots.

---

## 4. Configure `.env`

Copy the template, then edit `.env`. It's gitignored, so it's never committed.

```bash
copy .env.example .env        # macOS/Linux: cp .env.example .env
```

**Minimal working example** (Gemini). Replace the placeholders:

```dotenv
# ---- Jira
JIRA_BASE_URL=https://my-team.atlassian.net      # site root only: no trailing slash, no /jira/... path
JIRA_EMAIL=you@example.com                       # the email you log in to Atlassian with
JIRA_API_TOKEN=<your-api-token>
JIRA_PROJECT=TYN                                 # project key; lets you omit --project

# ---- Model
LLM_PROVIDER=gemini
LLM_MODEL=gemini-2.5-flash
LLM_API_KEY=<your-gemini-key>
LLM_MIN_INTERVAL_S=6                             # free tier: ~6-12 s between calls avoids rate limits
```

**All settings:**

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `JIRA_BASE_URL` | ✅ | | Jira Cloud site root, e.g. `https://my-team.atlassian.net` |
| `JIRA_EMAIL` | ✅ | | Atlassian account email |
| `JIRA_API_TOKEN` | ✅ | | Jira API token |
| `JIRA_PROJECT` | | | Project key used when `--project` is omitted. An explicit flag wins. |
| `JIRA_ISSUE_TYPE` | | `Task` | Issue type the importer creates |
| `JIRA_LINK_TYPE` | | `Relates` | Link type the importer creates |
| `LLM_PROVIDER` | | `gemini` | `gemini`, `anthropic`, `openai`, `openrouter`, `groq`, `deepseek`, `mistral`, `together` or `ollama` |
| `LLM_MODEL` | for OpenAI-compatible providers | `gemini-2.5-flash` / `claude-opus-5-5` | Model ID |
| `LLM_API_KEY` | ✅ (not for `ollama`) | | API key. Provider-specific variables such as `GEMINI_API_KEY` also work. |
| `LLM_BASE_URL` | | | Custom OpenAI-compatible endpoint (Azure, vLLM, LM Studio, a proxy) |
| `LLM_MIN_INTERVAL_S` | | `0` | Seconds between model calls, for rate-limited free tiers |
| `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, `LLM_EFFORT` | | | Optional tuning (see [section 9](#9-choose-a-model-provider)) |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | | | Optional monitoring (see [section 10](#10-monitoring-with-langfuse-optional)) |

Credentials are only read from the environment or `.env`, never from command-line flags, so they don't end up in shell history, traces or reports.

---

## 5. Import the dataset into Jira

The example outputs below are for the supplied `inputs/dataset.json`. With another dataset, all counts come from that file (see [Using a different dataset](#15-using-a-different-dataset)).

`--project` can be left out when `JIRA_PROJECT` is set in `.env`. The examples show it explicitly.

### 5.1 Preview (no network calls)

```bash
python -m tyn.importer import --dataset inputs/dataset.json --project TYN --dry-run
```
```
Dataset: 400 records, 2059 comments, 76 links, label 'tyn-release-assessment-v2'
Dry run - no network calls. Sample issue payload:
{ "project": {"key": "TYN"}, "issuetype": {"name": "Task"},
  "summary": "[REC-4666] Answer quality: atlas-assistant R2.2", ... }
Would create up to 400 issues, 2059 comments, 76 links.
```

### 5.2 Import

```bash
python -m tyn.importer import --dataset inputs/dataset.json --project TYN
```

Example output (about 7 minutes; posting the comments is the slow part):
```
Dataset: 400 records, 2059 comments, 76 links, label 'tyn-release-assessment-v2'
Connected as account 7120…  to project TYN (TYN Release Evidence)
Pass 1: 0 issues already exist, creating 400
  created 25/400
  ...
  created 400/400
Pass 2: syncing comments
  50/400 issues checked, 250 comments posted
  ...
  posted 2059 comments
Pass 3: creating 76 links (0 already present)
Import finished in 445.4s. Created 400 issues, 2059 comments, 76 links. Run 'verify' next.
```

**What gets created:**
- **One issue per record** (400 for the supplied dataset).
  - Each carries the dataset label `tyn-release-assessment-v2` plus `src-<source_id>`, `kind-*`, `release-*` and `recorded-status-*`. Requirement issues also get `rr-requirement`.
  - Summaries start with `[<source_id>]`.
  - Descriptions start with a header block (source ID, kind, release, recorded status, directed links), followed by the original text verbatim.
- **Every comment** (2,059 here), verbatim, in recorded-time order. Each keeps its `Recorded event <time> | actor <name> | role <role>` first line, because Jira's own author and timestamp belong to the importer, not to the scenario.
- **One "Relates" link per entry in a record's `links`** (76 here).

### 5.3 Verify

```bash
python -m tyn.importer verify --dataset inputs/dataset.json --project TYN
```
```
Issues: 400 / expected 400
Comments: 2059 / expected 2059
Links: 76 / expected 76 (unexpected extra: 0)
Text-search probe for 'BETA-017': ['REC-1001'] (expected REC-1001)
VERIFY OK
```

`verify` compares Jira with the dataset file, so the expected counts always come from your dataset. It also checks that comment text is searchable, using an identifier it picks from the data (one that occurs in exactly one record). It waits for Jira's search index to catch up after a fresh import. Run the agent only after `VERIFY OK`.

### 5.4 Re-running is safe

The importer checks what already exists before creating anything, so a re-run after a partial failure only fills the gaps:
```
Pass 1: 400 issues already exist, creating 0
  posted 0 comments
Pass 3: creating 0 links (76 already present)
Import finished in 34.6s. Created 0 issues, 0 comments, 0 links.
```

If a network failure ever leaves **duplicate** issues, the importer refuses to continue. To clean up:
1. Review what would be deleted:
   ```bash
   python -m tyn.importer dedupe --dataset inputs/dataset.json --project TYN
   ```
2. Then permanently delete the listed extra copies:
   ```bash
   python -m tyn.importer dedupe --dataset inputs/dataset.json --project TYN --apply
   ```

---

## 6. Run the assessment

### 6.1 Smoke test (2 requirements, about 2 minutes)

Check that Jira, the model and your keys work together before spending a full run:

```bash
python -m tyn.cli assess --project TYN --label tyn-release-assessment-v2 --release R2.4 --requirements REQ-05,REQ-08
```
```
Assessing R2.4 in TYN (label tyn-release-assessment-v2) with gemini:gemini-2.5-flash → runs\20261111-235221
  REQ-05  FAIL
  REQ-08  FAIL
Overall: PARTIAL   wall 106.46s   tokens 26474   tool calls 17
Report: runs\20261111-235221\report.md
```

A subset run is marked **PARTIAL** and has no overall verdict.

### 6.2 Full run (all requirements; about 10 minutes for the supplied 12)

```bash
python -m tyn.cli assess --project TYN --label tyn-release-assessment-v2 --release R2.4
```

The submitted run looked like this (`gemini-3.5-flash-lite`):
```
Assessing R2.4 in KAN (label tyn-release-assessment-v2) with gemini:gemini-3.5-flash-lite → runs\20261112-121528
  REQ-01  PASS
  REQ-02  FAIL
  REQ-03  INSUFFICIENT_EVIDENCE
  REQ-04  PASS
  REQ-05  FAIL
  REQ-06  INSUFFICIENT_EVIDENCE  (agent failure: guardrail_fired: 2 decisions rejected by code checks; defaulted to INSUFFICIENT_EVIDENCE)
  REQ-07  PASS
  REQ-08  FAIL
  REQ-09  INSUFFICIENT_EVIDENCE
  REQ-10  PASS
  REQ-11  INSUFFICIENT_EVIDENCE  (agent failure: guardrail_fired: 2 decisions rejected by code checks; defaulted to INSUFFICIENT_EVIDENCE)
  REQ-12  FAIL
Overall: BLOCKED   wall 551.35s   tokens 375102   tool calls 126
Report: runs\20261112-121528\report.md
```

`agent failure` means the requirement **failed safe**. Here the model's decisions were rejected by code, so it defaulted to INSUFFICIENT_EVIDENCE rather than risk a wrong PASS. The reasons are in the report.

**More examples:**
```bash
# Use another provider or model for one run without editing .env
python -m tyn.cli assess --label tyn-release-assessment-v2 --release R2.4 --provider anthropic --model claude-opus-5-5

# Write runs somewhere else
python -m tyn.cli assess --label tyn-release-assessment-v2 --release R2.4 --out results

# Tighter budgets: 12 tool calls per requirement, 15 minutes for the whole run
python -m tyn.cli assess --label tyn-release-assessment-v2 --release R2.4 --max-tool-calls-per-req 12 --max-wall-s 900
```

### 6.3 Output files (`runs/<timestamp>/`)

| File | Contents |
|---|---|
| `report.md` | The release-readiness report: verdict, summary table, and for each requirement the explanation, an evidence assessment table, cited Jira comments (with links, actor, role and recorded time), outstanding items, guardrail rejections; plus suspicious content, failures and run metrics |
| `decisions.json` | Machine-readable decisions and evidence (used for scoring) |
| `trace.jsonl` | Every model turn and tool call in order: arguments, result summaries, latency, retries, guardrail rejections. Secrets are redacted. |
| `metrics.json` | Wall time, provider and model, token usage, model and tool call counts, per-requirement figures, sessions |
| `checkpoint.json` | Progress saved after each requirement, used by `--resume` |

**Exit codes:**
- `0`: the run completed (or a `--requirements` subset finished).
- `1`: the run was incomplete or aborted.
- `2`: a configuration error, such as a missing setting or a bad `--resume` folder.

---

## 7. Read and score the results

**The report:** open `runs/<timestamp>/report.md`. It renders as a formatted page on GitHub or in VS Code's Markdown preview. Each requirement looks like this (excerpt from [`submission/report.md`](submission/report.md)):

```markdown
### REQ-08 — Human handoff delivery: FAIL

- **Jira:** KAN-52
- **Acceptance criterion:** When an answer is escalated, a handoff must reach the support queue,
  including its linked delivery dependency.

**Explanation.** Investigation of the linked delivery dependency verification record (KAN-229 / REC-9635)
shows a verified result from QA Lead Priya Nair on the exact target build (R2.4, commit b7a91f2,
service atlas-assistant, production) stating that 0 of 12 escalated conversations reached the support
queue ... leaving defect HND-DEF-4 open. ... Therefore, the decision is FAIL.

| Ref | Kind | Outcome | Applicable | Counted | Superseded by |
|---|---|---|---|---|---|
| `KAN-52#c10241`  | implementation_claim | supports    | no  | no      | |
| `KAN-229#c10378` | verified_result      | contradicts | yes | **yes** | |
```

- **Kind, Outcome, Applicable, Superseded by** are the agent's labels.
- **Counted** is computed by code: applicable, not superseded, and written by a role the policy authorises.

**Score a run** against the hand-derived expected answers in `tests/fixtures/expected_decisions.json`:

```bash
python tests/eval_report.py runs/<timestamp>/decisions.json
```
```
REQ     expected               got                    ok
REQ-01  PASS                   PASS                   ✓
REQ-02  FAIL                   FAIL                   ✓
...
REQ-12  FAIL                   FAIL                   ✓

Requirements correct: 12/12
Verdict: expected BLOCKED, got BLOCKED ✓
```

---

## 8. Resume an interrupted run

`checkpoint.json` is saved after every requirement. If a run stops partway (Ctrl+C, a crash, the daily model quota running out), finish the **same** run later:

```bash
python -m tyn.cli assess --resume runs/20261112-121528
```
```
Resuming runs\20261112-121528: 4 requirement(s) already decided (REQ-01, REQ-02, REQ-03, REQ-04); the rest will be assessed
Assessing R2.4 in TYN (label tyn-release-assessment-v2) with gemini:gemini-2.5-flash → runs\20261112-121528
  REQ-01  PASS                   (from session 1)
  ...
```

- **Kept from earlier sessions:** validated decisions are reused. Failed (quota, network, loop, budget) or missing requirements are assessed again.
- **No model calls on start:** the manifest is re-read from Jira directly. **Resume refuses if the target build changed.**
- **One run folder:** the trace is appended, metrics are summed across sessions, and the report is regenerated as one report with a "Sessions" table.
- **Scope can't change:** project, label, release and `--requirements` come from the checkpoint. You may switch provider or model, and the report notes it.
- **Hint:** whenever anything is unfinished, the command prints the exact `--resume` line to run.

---

## 9. Choose a model provider

No code changes are needed. Set it in `.env`, or per run with `--provider` / `--model`.

| `LLM_PROVIDER` | Uses | `LLM_MODEL` | Key variable |
|---|---|---|---|
| `gemini` | Google GenAI SDK | default `gemini-2.5-flash` | `GEMINI_API_KEY` or `LLM_API_KEY` |
| `anthropic` | Anthropic SDK | default `claude-opus-5-5` | `ANTHROPIC_API_KEY` or `LLM_API_KEY` |
| `openai` | Any OpenAI-compatible Chat Completions API (set `LLM_BASE_URL` for Azure, vLLM, LM Studio or a proxy) | required | `OPENAI_API_KEY` or `LLM_API_KEY` |
| `openrouter`, `groq`, `deepseek`, `mistral`, `together` | OpenAI-compatible, base URL preset | required | `<NAME>_API_KEY` or `LLM_API_KEY` |
| `ollama` | Local models at `http://localhost:11434/v1` | required (tool-capable) | none |

**Examples (`.env`):**

```dotenv
# Claude
LLM_PROVIDER=anthropic
LLM_MODEL=claude-opus-5-5
LLM_API_KEY=<anthropic-key>

# OpenRouter: any model it hosts
LLM_PROVIDER=openrouter
LLM_MODEL=<vendor/model-id>
LLM_API_KEY=<openrouter-key>

# Fully local: data never leaves your machine (install Ollama, then `ollama pull <model>`)
LLM_PROVIDER=ollama
LLM_MODEL=<tool-capable-model, e.g. qwen3>

# Self-hosted or Azure OpenAI-compatible endpoint
LLM_PROVIDER=openai
LLM_BASE_URL=https://my-endpoint.example.com/v1
LLM_MODEL=<model-id>
LLM_API_KEY=<key>
```

**Optional tuning:**
- `LLM_TEMPERATURE`: Gemini defaults to 0. It's ignored for Claude, which rejects sampling parameters.
- `LLM_MAX_TOKENS`: per-response output cap.
- `LLM_EFFORT`: reasoning effort (`low` to `max`), Claude only.
- `ANTHROPIC_FALLBACKS`: server-side refusal fallback on supported Claude models. On by default; set to `0` to turn it off.

**Rate limits:** free tiers limit requests per minute and per day. Set `LLM_MIN_INTERVAL_S=6` (or `12`), and use [resume](#8-resume-an-interrupted-run) if a daily quota runs out. A full run makes roughly 60–150 model calls.

---

## 10. Monitoring with Langfuse (optional)

Each run can be exported as an **OpenTelemetry** trace: run → requirement → model calls (`generation`), tool calls (`tool`) and guardrail rejections (`guardrail`), with tokens, latency, retries and errors.

1. Create a project at https://cloud.langfuse.com (or self-host) and copy its API keys.
2. Add them to `.env`:
   ```dotenv
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_HOST=https://cloud.langfuse.com     # US: https://us.cloud.langfuse.com · self-hosted: http://localhost:3000
   ```
3. Run `assess` as usual. The console shows `telemetry: exporting OpenTelemetry traces …`. In Langfuse, open the trace **`release-assessment R2.4`**; its session ID `<release>-<timestamp>` matches the `runs/` folder.

**Behaviour:**
- **Off unless configured.** `--no-telemetry` or `TYN_TELEMETRY=off` disables it.
- **Other backends** (Phoenix, Grafana, Jaeger, Honeycomb) work through the standard `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS` variables.
- **Metadata only by default:** IDs, decisions, counts, timings and tokens. `--capture-content` also sends tool arguments, results and model text, after secret redaction.
- **Never breaks a run.** If the backend is unreachable, the run and its files are unaffected.

---

## 11. Policy and how decisions are checked

**`policy.yaml`** holds organisation knowledge, so the code holds none:
- which roles count as verifiers and approvers;
- role requirements triggered by criterion text;
- which target fields evidence must state;
- value aliases (`production` ↔ `prod`) and conflicting values (`staging`).

Use another file with `--policy path/to/policy.yaml`.

**How decisions are checked.** For every relevant comment it fetched, the agent states its **kind** (verified result, approval, implementation claim, proposed fix, other), its **outcome** (supports, contradicts, neutral), whether it is **applicable** to the target build, and what **supersedes** it. Code then rejects, with reasons:
- a decision that leaves relevant evidence out;
- a wrong-scope comment marked applicable;
- a fake supersession;
- a PASS with no counted support, or with counted contradicting evidence;
- a FAIL with no counted failure;
- an INSUFFICIENT_EVIDENCE with no outstanding items.

After two rejections the requirement falls back to INSUFFICIENT_EVIDENCE. Code never turns a decision into PASS. Details are in [DESIGN.md](DESIGN.md).

---

## 12. Tests

```bash
python -m pytest -q
```

Offline: no Jira, model or keys needed. The tests use an in-memory Jira, populated by the real importer, and a scripted model to drive the real tools, guardrails and agent loop. They cover:
- the importer: counts, idempotency, resume after partial import, verbatim text;
- every guardrail, and fail-safe behaviour on model errors, loops and budgets;
- **generalisation**: the whole pipeline on a perturbed copy of the dataset (IDs, values, roles, wording and order all changed) and on what-if variants where the right answer changes;
- the provider adapters;
- checkpoint and resume;
- telemetry;
- secret redaction;
- the agent's Jira client having no write methods.

---

## 13. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Jira HTTP 404: Site temporarily unavailable` | `JIRA_BASE_URL` doesn't point to an active site | Open `<url>/status` in a browser; it must show `{"state":"RUNNING"}`. Use the site **root** (`https://name.atlassian.net`), copied from the address bar while in Jira, not `home.atlassian.com`. |
| `Failed to resolve 'https'` | URL written twice, e.g. `https://https://…`, or a stray space after `=` | Fix the `JIRA_BASE_URL` line in `.env` |
| `Jira HTTP 401` | Wrong email or token, or the token expired | Create a new token; check `JIRA_EMAIL` |
| `No project could be found with key …` | Wrong project key, or no access | Check the key in **Project settings → Details**, or set `JIRA_PROJECT` |
| `Field 'labels' cannot be set` | The Labels field isn't on the Task layout | **Project settings → Issue types → Task**: add **Labels**, then re-run the import |
| Import stops with a timeout or `network error on POST` | Jira was slow; the request may have been applied | Wait a minute, then re-run the import (it skips what exists). If it reports duplicates, run `dedupe`, then `dedupe --apply`. |
| `Import stopped: … more than one Jira issue` | Duplicates from an earlier failure | `python -m tyn.importer dedupe --dataset inputs/dataset.json --project <KEY>`, then add `--apply` |
| Agent says `No issue with source id` right after an import | Jira's search index is still catching up | Run `verify` until `VERIFY OK` |
| `HTTP 429 RESOURCE_EXHAUSTED` / `exceeded your current quota` | Model rate limit or daily quota | Raise `LLM_MIN_INTERVAL_S`; wait for the quota reset and run `--resume`; or switch provider |
| `Missing API key: set LLM_API_KEY …` | No model key for that provider | Add `LLM_API_KEY` (or the provider's own variable) to `.env` |
| `Set LLM_MODEL …` | OpenAI-compatible providers have no default model | Set `LLM_MODEL` |
| Many INSUFFICIENT_EVIDENCE with `agent failure` | Quota, network or budget problems during the run | Read "Failures and warnings" in the report, fix the cause, then `--resume` |

---

## 14. Command reference

```text
python -m tyn.importer {import,verify,dedupe} --dataset FILE [--project KEY] [--dry-run] [--apply] [--workers N]

python -m tyn.cli assess [--project KEY] --label LABEL --release RELEASE
                         [--requirements REQ-01,REQ-02]   subset (smoke test, PARTIAL)
                         [--resume RUN_DIR]               continue an interrupted run
                         [--provider NAME] [--model ID]   override .env for this run
                         [--policy FILE] [--out DIR]
                         [--no-telemetry] [--capture-content]
                         [--max-tool-calls-per-req N]
                         [--max-wall-s SECONDS] [--max-total-tool-calls N]   default: scale with the number of requirements

python tests/eval_report.py runs/<timestamp>/decisions.json [--expected answer_key.json]
python -m pytest -q
```

Run `python -m tyn.cli assess --help` or `python -m tyn.importer --help` for the full help.

## 15. Using a different dataset

Nothing in the code is tied to this dataset's size, IDs or wording:
- **Requirements** are discovered from the manifest's links.
- **Run-wide budgets** scale with the number of requirements.
- **Long tickets:** comments are paged, never silently truncated.
- **Domain knowledge** (roles, aliases, required fields) lives in `policy.yaml`.

Tests check this on a synthetic manifest with 130 requirements and on a fully renamed copy of the data.

**What a dataset must provide** (the same shape as `inputs/dataset.json`):
- `dataset_label`: becomes the Jira scope label;
- `target`: release, commit, service, environment and any other fields;
- `records`: each with `source_id`, `kind` (`manifest`, `requirement`, `evidence` or `work`), `summary`, `release`, `recorded_status`, `description`, `comments` and `links`;
- one `manifest` record whose description has a `Target: {...}` JSON line and whose `links` list the requirements to assess;
- requirement descriptions with `Requirement ID:`, `Mandatory:` and `Acceptance criterion:` lines;
- comments starting with `Recorded event <ISO time> | actor <name> | role <role>`. Without this header a comment is still shown, but never counts as evidence.

**Steps:**
```bash
python -m tyn.importer import --dataset path/to/new.json --project NEW --dry-run
python -m tyn.importer import --dataset path/to/new.json --project NEW
python -m tyn.importer verify --dataset path/to/new.json --project NEW
python -m tyn.cli assess --project NEW --label <dataset_label from the file> --release <target release>
```

**Adjust if needed:**
- **`policy.yaml`:** if the organisation uses different role names (e.g. "Test Engineer", "Head of Security"), required scope fields, or environment aliases.
- **Budgets:** for very large manifests, `--max-wall-s` and `--max-total-tool-calls` set fixed limits. By default they scale with the plan.
- **Scoring:** `tests/eval_report.py` scores against the supplied dataset's answer key by default. For another dataset, write a key in the same format and pass it:
  ```bash
  python tests/eval_report.py runs/<timestamp>/decisions.json --expected my_expected.json
  ```

---

## Repository layout

```
tyn/
  config.py       env-based configuration (no credentials on the command line)
  jira_client.py  JiraReader (GET only, used by the agent) / JiraWriter (importer); retries, timeouts
  adf.py          plain text <-> Atlassian Document Format, so text round-trips verbatim
  importer.py     import / verify / dedupe
  policy.py       loads policy.yaml (roles, scope fields, aliases)
  text.py         comment header parsing, repeat collapsing, target-value matching, injection signals, escaping
  tools.py        the agent tools and the structural record_decision guardrails
  model.py        provider adapters (Gemini, Anthropic, OpenAI-compatible) behind one small interface
  agent.py        discovery + one bounded chat per requirement; tracing; verdict
  checkpoint.py   per-requirement checkpoint and --resume
  telemetry.py    optional OpenTelemetry export (Langfuse or any OTLP backend)
  report.py       report.md / decisions.json / metrics.json
  cli.py          the assess command
policy.yaml       assessment policy (organisation configuration)
tests/            offline pytest suite, perturb.py (dataset perturbation), eval_report.py
inputs/           supplied dataset.json (the import input, never shown to the model)
submission/       the generated report, trace and metrics from a real run
```

## Security notes

- **Credentials** live only in `.env`, which is gitignored. Keep them out of chat, code and screenshots, and revoke any token that leaks.
- **The agent can only read Jira**: its client only issues GET requests, and a test enforces this. Only the importer writes.
- **Where data goes:** Jira content is sent to the configured model provider, and to Langfuse if monitoring is enabled. For real company data, use a local model (`ollama`) and self-hosted Langfuse.
