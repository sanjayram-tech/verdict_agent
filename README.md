# TYN Jira Release Evidence Agent

A tool-using AI agent that decides whether release **R2.4** is ready, using only the evidence
recorded in Jira. It reads requirements, follows linked verification records, checks that each
result applies to the target build, and writes an evidence-backed report.

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

## 5. Tests and evaluation (offline, no credentials needed)

```bash
python -m pytest -q
python tests/eval_report.py runs/<timestamp>/decisions.json
```

The tests use an in-memory Jira, populated by the real importer, and a scripted model to exercise the real tools, guardrails and agent loop. They cover:

- the importer: counts, idempotency, resuming after a partial import, verbatim round-trips;
- filler removal and scope checks;
- injection flagging and the decision guardrails;
- fail-safe behaviour on model errors, loops and budget exhaustion;
- trace redaction;
- the agent's Jira client having no write methods.

`eval_report.py` compares a run with the hand-derived expected outcome in `tests/fixtures/expected_decisions.json`. That file is never shown to the agent.

## Repository layout

```
tyn/
  config.py       env-based configuration (no credentials on the command line)
  jira_client.py  JiraReader (GET only, used by the agent) / JiraWriter (importer); retries, timeouts
  adf.py          plain text <-> Atlassian Document Format, so text round-trips verbatim
  importer.py     import / verify
  text.py         comment header parsing, filler, scope extraction, injection detection, escaping
  tools.py        the agent tools and the record_decision guardrails
  model.py        provider adapters (Gemini, Anthropic, OpenAI-compatible) behind one small interface
  agent.py        discovery + one bounded chat per requirement; tracing; verdict
  report.py       report.md / decisions.json / metrics.json
  cli.py          the assess command
tests/            offline pytest suite + eval_report.py
inputs/           supplied dataset.json (the import input, never shown to the model)
submission/       the generated report, trace and metrics from a real run
```
