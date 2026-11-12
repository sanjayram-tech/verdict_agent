# TYN Jira Release Evidence Agent

A tool-using AI agent that decides whether release **R2.4** is ready, using only the evidence
recorded in Jira. It reads requirements, follows linked verification records, checks that each
result applies to the target build, and writes an evidence-backed report.

> **Project status:** see [STATUS.md](STATUS.md) for what's done, what's been verified live, and the remaining steps.

The repository contains:

- `tyn/importer.py`: a repeatable, idempotent importer that loads `inputs/dataset.json` into a Jira Cloud project.
- `tyn/cli.py`: the assessment agent, which works with **any tool-calling LLM** and uses read-only Jira tools. Supported providers: Gemini, Anthropic Claude, OpenAI, and any OpenAI-compatible endpoint such as OpenRouter, Groq, DeepSeek, Mistral, Together, Azure, or local models through Ollama, vLLM or LM Studio.
- `submission/`: one real run, with `report.md`, `trace.jsonl` (every tool call), `metrics.json` (time and tokens) and `decisions.json`.
- [DESIGN.md](DESIGN.md): design choices, safety guardrails, known limits and how AI assistance was used.

## 1. Prerequisites

- Python 3.10 or newer.
- A Jira Cloud site. A free site works: create one at atlassian.com, then create a **team-managed
  Kanban or Scrum project** (for example key `TYN`) that has a **Task** issue type.
- A Jira API token, created at https://id.atlassian.com/manage-profile/security/api-tokens.
- An API key for the model provider you choose (see [Choosing a model](#choosing-a-model)). A local model through Ollama needs no key.

## 2. Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt   # exact pinned versions
cp .env.example .env              # then fill in the values (never commit .env)
```

## 3. Import the dataset into Jira

`--project` can be left out of every command below if `JIRA_PROJECT` is set in `.env`; an explicit `--project` wins.

```bash
python -m tyn.importer import --dataset inputs/dataset.json --project TYN --dry-run
python -m tyn.importer import --dataset inputs/dataset.json --project TYN
python -m tyn.importer verify --dataset inputs/dataset.json --project TYN
```

**What the import creates**

- **Issues:** one per record, 400 in total.
  - Every issue gets the label `tyn-release-assessment-v2` (the dataset scope).
  - It also gets `src-<source_id>`, `kind-*`, `release-*`, `recorded-status-*`, and `rr-requirement` for the requirement issues.
  - The summary starts with `[<source_id>]`.
  - The description begins with a header block: source ID, kind, release, recorded status and directed links.
- **Comments:** all 2,059 are posted verbatim. The scenario actor, role and timestamp are kept in each comment's first line.
- **Links:** 76 "Relates" links.

**Re-running** the import only creates what is missing, so it is safe after a partial failure.

**`verify`** checks the counts (400 / 2,059 / 76) and confirms that Jira's text search returns the comment evidence.

## 4. Run the assessment

```bash
python -m tyn.cli assess --project TYN --label tyn-release-assessment-v2 --release R2.4
```

Outputs are written to `runs/<timestamp>/`: `report.md`, `decisions.json`, `trace.jsonl` and `metrics.json`.

- **Exit code:** `0` when the run completed, `1` if it was incomplete, `2` for configuration errors.
- **Free tiers:** set `LLM_MIN_INTERVAL_S=6` in `.env` to stay under the rate limit.
- **Switching model:** `--provider` and `--model` override `.env` for a single run, for example
  `--provider anthropic --model claude-opus-5-5`.

### Choosing a model

Everything is configured in `.env`; no code changes are needed. The model must support tool (function) calling.

| `LLM_PROVIDER` | What it uses | `LLM_MODEL` | Key |
|---|---|---|---|
| `gemini` | Google GenAI SDK (native) | default `gemini-2.5-flash` | `GEMINI_API_KEY` |
| `anthropic` | Anthropic SDK (native) | default `claude-opus-5-5` | `ANTHROPIC_API_KEY` |
| `openai` | Any OpenAI-compatible Chat Completions API. Set `LLM_BASE_URL` for Azure, vLLM, LM Studio or a proxy. | required | `OPENAI_API_KEY` |
| `openrouter`, `groq`, `deepseek`, `mistral`, `together` | Presets of `openai` with that service's base URL filled in | required | `<NAME>_API_KEY` |
| `ollama` | Local models at `http://localhost:11434/v1` | required, a tool-capable model | none |

`LLM_API_KEY` works for every provider. Optional settings:

- `LLM_TEMPERATURE`: Gemini uses 0 by default. It is ignored for Claude, which rejects sampling parameters.
- `LLM_MAX_TOKENS`: per-response output cap.
- `LLM_EFFORT`: reasoning effort, Claude only.
- `ANTHROPIC_FALLBACKS`: server-side refusal fallback on supported Claude models. On by default; set to `0` to turn it off.

The agent loop, tools, guardrails and report are identical whichever provider you choose. Only `tyn/model.py` differs per provider.
- **Budgets:** there are per-requirement and run-wide budgets. See `--help` and `Budgets` in `tyn/agent.py`.
- **Smoke test:** `--requirements REQ-05,REQ-08` assesses only those requirements. The run is marked PARTIAL and has no overall verdict.

### Resuming an interrupted run

After every requirement, the run folder's `checkpoint.json` is rewritten atomically. Whatever stops a run (Ctrl+C, a crash, an exhausted model quota), nothing already decided is lost. To continue:

```bash
python -m tyn.cli assess --resume runs/<timestamp>
```

- **Reused, not re-assessed:** requirements that already have a validated decision.
- **Assessed again:** requirements that ended in an agent failure (quota, network, loop, budget) or were never reached.
- **No model calls on start:** the manifest and requirements are re-read from Jira directly. The run **refuses to resume if the target build changed**.
- **Same run folder:** the trace is appended (with a `resume` event), and metrics are summed across sessions. The report is regenerated as one report with a "Sessions" table, and each requirement shows when, and in which session, it was assessed.
- **Same scope:** project, label, release and `--requirements` come from the checkpoint. You can switch provider or model for the resumed session; the report notes it.
- **Hint:** when anything is left unfinished, the command prints the exact `--resume` line to run.

Typical use on a free tier: run until the daily quota runs out, then resume after it resets.

### Assessment policy (`policy.yaml`)

Organisation-specific knowledge is configuration, not code:
- which roles count as **verifiers** (test results) and **approvers**;
- role requirements triggered by the criterion text (e.g. a criterion mentioning the security owner needs that role's approval);
- which target fields evidence must state, and when (prompt only if the criterion mentions it);
- value aliases (`production` ↔ `prod`) and conflicting values (`staging`);
- optional boilerplate patterns.

Pass `--policy path/to/policy.yaml` to use another file.

### How decisions are checked

The agent records each decision with a **structured evidence assessment**. For every relevant comment it fetched, it gives:
- the **kind**: verified result, approval, implementation claim, proposed fix or other;
- the **outcome**: supports, contradicts or neutral;
- whether it is **applicable** to the target build;
- what **supersedes** it, if anything.

Code then decides which items **count**: applicable, not superseded, and written by a role the policy authorises. It rejects decisions that leave relevant evidence out, mark wrong-scope comments as applicable, give a fake supersession, or that the counted evidence doesn't support. See [DESIGN.md](DESIGN.md#safety-and-guardrails).

### Monitoring with Langfuse (optional)

Each run can be exported as an **OpenTelemetry** trace. The trace has one root span for the run, and under it:
- one span per requirement;
- a `generation` span for every model call, with model, provider and token usage (`gen_ai.*`);
- a `tool` span for every tool call, with latency, cache hits and errors;
- a `guardrail` span (warning level) for every rejected decision.

Retries, malformed calls and failures are attached as events.

1. Create a Langfuse project at https://cloud.langfuse.com, or self-host it, and copy its API keys.
2. Add them to `.env`:
   ```
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_HOST=https://cloud.langfuse.com    # or https://us.cloud.langfuse.com, or your self-hosted URL
   ```
3. Run `assess` as usual. Each run appears as a trace named `release-assessment R2.4`, with a session ID of the form `<release>-<timestamp>` that matches the `runs/` folder.

**Behaviour:**
- **Off unless configured.** `--no-telemetry` or `TYN_TELEMETRY=off` disables it. Other backends (Phoenix, Grafana, Jaeger, Honeycomb) work through the standard `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` variables.
- **Metadata only by default:** IDs, decisions, counts, timings and tokens. `--capture-content` (or `TYN_TELEMETRY_CAPTURE_CONTENT=true`) also sends tool arguments, results and model text, after the same secret redaction as `trace.jsonl`.
- **Never breaks a run.** Exporter or handler errors are reported once and ignored. `trace.jsonl` is still written either way.

## 5. Tests and evaluation (offline, no credentials needed)

```bash
python -m pytest -q
python tests/eval_report.py runs/<timestamp>/decisions.json
```

The tests use an in-memory Jira, populated by the real importer, and a scripted model to exercise the real tools, guardrails and agent loop. They cover:

- the importer: counts, idempotency, resuming after a partial import, verbatim round-trips;
- repeat collapsing and format-independent scope checks;
- injection flagging and every structural guardrail;
- **generalisation**: the same pipeline on a perturbed copy of the dataset (IDs, values, roles, wording and order all changed) and on what-if variants where the right answer changes. Correct decisions are accepted, and wrong PASS or FAIL decisions are rejected;
- fail-safe behaviour on model errors, loops and budget exhaustion;
- trace redaction;
- checkpoint and resume: an interrupted run continues without redoing finished requirements; quota-failed requirements are re-assessed; a changed target is refused; citations and injection flags survive the resume;
- telemetry: the span tree, `gen_ai` usage attributes and guardrail spans, no content by default, redaction when content is captured, Langfuse endpoint and auth, and a broken exporter not affecting the run (all with OpenTelemetry's in-memory exporter);
- the agent's Jira client having no write methods.

`eval_report.py` compares a run with the hand-derived expected outcome in `tests/fixtures/expected_decisions.json`. That file and `tests/fixtures/expected_evidence.json` (honest evidence labels) are used only by tests and are never shown to the agent.

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
