# Install Guide — BORG_ROUTE
> Audience: Operators / IT staff / LLM installer agents
> Covers: installation, configuration, deployment, maintenance
> Cross-platform: Windows, Linux, macOS

---

## Note to an LLM Installer Agent — READ FIRST

If you are an LLM agent tasked with installing BORG_ROUTE, read this
section before executing anything. Following it will save you 15-30
minutes of debugging what would otherwise look like mysterious failures.

### Platform detection
Detect the platform before running any shell command:
- **Windows (PowerShell / cmd):** use `copy`, `;` to chain, backslash paths optional but relative paths preferred.
- **Linux / macOS / Git Bash on Windows:** use `cp`, `&&` to chain, forward slashes.
- A safe cross-platform alternative to `copy` / `cp` is `python -c "import shutil; shutil.copy('.env.example', '.env')"`.

### Known pitfalls (in order of frequency)
1. **Running tests before starting LM Studio/Ollama.** Tests pass, but then `POST /models/discover` returns 0 local models. This is expected — tests do not probe live providers. Start your local model server *before* calling discover.
2. **Calling `POST /models/research` before `POST /models/discover`.** Scorer runs against the matrix, which is empty on a fresh install. Order is: (1) start local model servers, (2) discover, (3) research.
3. **Forgetting `OPENROUTER_API_KEY`.** Without it, OpenRouter discovery AND the scorer (default `perplexity/sonar-pro-search`, routed via OpenRouter) both fail. Set the key in `.env` before discover/research.
4. **Using `copy` on Linux or `cp` in raw PowerShell.** See "Platform detection" above; prefer the Python one-liner.
5. **Assuming every row in the matrix has real scores.** After discover, all 19 rows have `task_scores` at a flat 5.0 until `POST /models/research` is run. The router will either use that flat score or fall back with `BRT-ORC-007-DEGRADED`. This is normal; running research is what fills it in.
6. **Skipping the checkbox step.** Every row defaults to `checked: false`. Until the operator (or you, via `PATCH /models/{id}/checked`) ticks at least one model, the router returns `BRT-REG-020 no model found`.
7. **Trying to benchmark.** There is no `POST /models/benchmark` any more. It was replaced by `POST /models/research`. If you see stale docs mentioning benchmark, they are wrong.

### Skip spaCy for now
Slice 5 (NER worker) is parked by default. The previous install step
`python -m spacy download en_core_web_sm` is **not required** for a
functioning system today. Skip it. Only run it if the operator
specifically asks for NER. (The `spacy` library itself is still a
dependency because tests import it, but the language model download
is optional.)

**Test-count math (BRT-QA-01):** on a fresh install without
`en_core_web_sm`, the three NER worker tests skip automatically
(TC-S5-010 / 011 / 012). You should see `293 passed, 3 skipped,
0 failed` (i.e. 296 tests collected, 3 skipped). Installing the
language model flips those three from skip to pass, giving the
full `296 passed` result.

### Success criteria
The install is done when all of these pass:
- `python -m pytest` → exits 0 with 296 passing
- `GET http://localhost:8000/health` → `{"status":"ok"}`
- `GET http://localhost:8000/models/matrix` → returns 19 rows (7 LM Studio + 12 OpenRouter) with `checked: false` defaults
- At least one row has been ticked with `PATCH /models/<id>/checked` (use `d-jai/test-model` from LM Studio, or any one you plan to use)

---

## Prerequisites
- Python **3.11 or newer** (3.11 / 3.12 / 3.13 all tested).
- `pip` (bundled with Python).
- Optional: **LM Studio** (Windows / macOS / Linux builds — all use ports 1234-1237).
- Optional: **Ollama** (Linux-native, macOS + Windows builds exist — port 11434).
- Optional: **Ollama in Docker** (`docker run -d -p 11434:11434 ollama/ollama`, then pull a model) — an easy way to get a local model without a native install; it answers on port 11434, the default address.
- Optional: **OpenRouter API key** for external models AND for the capability scorer.

## Quick Start (Local Development)

### Windows (PowerShell)
```powershell
cd C:\Stage\BORG_ROUTE                      # or wherever you extracted it

copy .env.example .env
# Edit .env — add OPENROUTER_API_KEY (and any LM Studio paths)

python -m pip install -e ".[dev]"
python -m pytest                            # expect 296 passing

uvicorn main:app --reload
# Server at http://localhost:8000
```

### Linux / macOS / Git Bash
```bash
cd /path/to/BORG_ROUTE                      # wherever you cloned it

cp .env.example .env
# Edit .env — add OPENROUTER_API_KEY (and any LM Studio paths)

python3 -m pip install -e ".[dev]"
python3 -m pytest                           # expect 296 passing

uvicorn main:app --reload
# Server at http://localhost:8000
```

### Cross-platform (works everywhere including raw PowerShell)
```bash
python -c "import shutil; shutil.copy('.env.example', '.env')"
python -m pip install -e ".[dev]"
python -m pytest
python -m uvicorn main:app --reload
```

### Verify
`GET http://localhost:8000/health` → `{"status":"ok"}`. The system is up.

---

## First-Run Checklist (after `uvicorn` is running)

1. **Start your local model server** (LM Studio or Ollama) if you want local models.
2. **Open the UI:** `http://localhost:8000/ui/models.html`
3. **Click Discover Models.** Expect 7 LM Studio rows (if LM Studio is running) + 12 curated OpenRouter rows.
4. **Click Get Model Info.** Scorer runs for 30-90 seconds; fills relative 0-10 task scores per row. Requires `OPENROUTER_API_KEY`.
5. **Tick the checkbox** on any row you want in today's routing pool (typical: 3-6 total).
6. **Kick a test run** from `http://localhost:8000/ui/` (it opens the Admin overview) — the orchestrator will route to one of the checked models.

## Production Deployment

### Linux (systemd + reverse proxy — recommended)
```bash
# Run as non-root user
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```
Put it behind nginx or caddy for TLS. The `aiosqlite` DB at
`data/borg_route.db` is a single file — back it up with any file
backup tool.

### Windows
Run under NSSM or a Windows Service wrapper of your choice. Same
`uvicorn` command, same single-DB backup story.

### Notes on workers
`--workers > 1` is **not supported** today because the orchestrator keeps
an in-process run registry and SQLite DB. For multi-worker scale, wait
for the Postgres migration slice. Use vertical scale (faster CPU) until
then.

## Environment Variables
See `.env.example` — every variable is documented inline. The ones you
are most likely to touch during install and operation are grouped below.

### Provider credentials
| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | (unset) | Required for OpenRouter discovery, the capability scorer, and the Perplexity web-search fallback. |
| `TAVILY_API_KEY` | (unset) | Optional. When set, Tavily becomes the primary `web_search` provider (cheaper than Perplexity). Without it, `web_search` silently falls back to Perplexity via OpenRouter and logs `BRT-TOOL-DEGRADED` once per process. |

### Local model servers
| Variable | Default | Purpose |
|---|---|---|
| `LMSTUDIO_BASE_URLS` | `http://localhost:1234`, `1235`, `1236`, `1237` | Comma-separated LM Studio addresses probed by Discover. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama address probed by Discover (Ollama in Docker works here). |
| `LOCAL_OPENAI_BASE_URLS` | (unset) | Comma-separated addresses of any other OpenAI-compatible server, found through `/v1/models`; rows get source `custom_key`. |

Addresses must start with `http://` or `https://`; anything else is ignored with a log warning. An unreachable address is skipped, never an error.

### Scorer
| Variable | Default | Purpose |
|---|---|---|
| `SCORING_MODEL` | `perplexity/sonar-pro-search` | Must support real web search. |
| `SCORING_MODEL_ENDPOINT` | OpenRouter | Override if you want to hit a provider directly. |

### Web-search tool (BRT-WEB-01)
| Variable | Default | Purpose |
|---|---|---|
| `BORG_WEB_SEARCH_PROVIDER_ORDER` | `tavily,perplexity` | Comma-separated override. Set to `perplexity,tavily` to skip Tavily. |
| `BORG_WEB_SEARCH_MAX_RESULTS` | `5` | Per-call result cap (1–20). |
| `BORG_WEB_SEARCH_TIMEOUT_SEC` | `8` | Per-provider HTTP timeout. |
| `BORG_PERPLEXITY_MODEL` | `perplexity/sonar` | Swap to `perplexity/sonar-pro` or `perplexity/sonar-reasoning` without redeploy. |

### Orchestrator / router knobs
| Variable | Default | Purpose |
|---|---|---|
| `ORCHESTRATOR_MAX_TOKENS` | `2000` | Ceiling on orchestrator JSON responses. Raise for large plans; lower for cost control. |
| `ANSWERER_MAX_TOKENS` | `2000` | Ceiling on answerer responses (both bypass and orchestrated paths). |
| `ORCHESTRATOR_QUALITY_REQUIREMENT` | `9.0` | Minimum task_score for a row to qualify as orchestrator. Forces a frontier-or-near-frontier brain. Degrades to 5.1 with a `BRT-ORC-NO-FRONTIER` trace event if no checked model meets the bar. Six curated OpenRouter rows already qualify out of the box. |
| `TIME_COST_PER_SEC` | `0.0001` | Dollar cost per wall-second, used by the router to penalise slow models. |

### Safety caps
| Variable | Default | Purpose |
|---|---|---|
| `BORG_MAX_RUN_SECONDS` | `300` | Hard wall-clock budget per run. Runs that exceed this are cancelled and marked `failed` with `BRT-PIP-008`. Protects against runaway / stuck pipelines burning real money. Raise for long research tasks. |

### Behaviour toggles
| Variable | Default | Purpose |
|---|---|---|
| `BORG_RULE_ZERO_SHORTCUT` | `true` | Set to `false` to force the full plan path on every prompt (useful for debugging). |
| `BORG_STREAM_HEARTBEAT_SEC` | `5` | SSE heartbeat cadence so long-running plans don't trip browser idle timeouts. |

### The_Borg_DB compatibility — provenance stamps on every log row
Every log row, run row, and entity/relation extraction carries three
provenance stamps so the data can be ingested by The_Borg_DB (or any
downstream knowledge system) without losing track of where it came from:

| Field | Source | Purpose |
|---|---|---|
| `instance_id` | env `BORG_INSTANCE_ID` -> hash(hostname + uid) | Tells apart logs from different devices when aggregated |
| `source_version` | env `BORG_SOURCE_VERSION` -> `git rev-parse --short HEAD` | Weights confidence as ROUTE evolves |
| `domain_profile` | env `BORG_DOMAIN_PROFILE` -> `general` | Tags the active vertical (healthcare, legal, ...) |

NER-extracted entities and relations carry `confidence` (0.85 for spaCy
entities, 0.6 for dependency-parsed relations) and `provenance`
(`spacy:en_core_web_sm`, `dep_parse:en_core_web_sm`).

A read-only view `borg_db_interactions` denormalizes logs + runs +
payloads into a single ingestion stream. A `borg_db_export_cursor`
table tracks per-consumer ingest progress so downstream pulls are
incremental and idempotent. See BRT-LOG-DB-COMPAT for details.

### Where settings come from
BORG resolves each operator-facing setting in this order: **Admin DB (`config`
table, written via `POST /config`) → `.env` → hardcoded default**. The Admin UI
at `/ui/settings.html` shows the effective value for each setting with a tag
showing which source it came from, and saves a changed value through
`POST /config`; it applies from the next run, with no restart. Two settings are
read from `.env` only (`ORCHESTRATOR_MAX_TOKENS`, `ANSWERER_MAX_TOKENS`); the
page shows them read-only.

### Cold-start defaults — pre-checked OpenRouter models
On a fresh install the curated capability matrix ships with **two OpenRouter
rows pre-checked** so the system can route a prompt without manual setup:

| Model | reasoning | role |
|---|---|---|
| `openrouter/anthropic/claude-sonnet-4.6` | 9.0 | Strict-JSON discipline / instruction-following safety net |
| `openrouter/moonshotai/kimi-k2-thinking` | 9.2 | ~5-6× cheaper; the cost optimiser will prefer this for most calls |

Both meet the default `ORCHESTRATOR_QUALITY_REQUIREMENT=9.0` bar. The router's
cost optimiser picks the cheaper model per call (Kimi for most prompts),
falling back to Sonnet when Kimi is unreachable, when context > 256k is
needed, or when Kimi trips on strict JSON output. Operators can uncheck
either or both at `/ui/models.html` and check on different rows.

If every row gets unchecked, the Models page shows a red banner reminding
the operator that no model is in the pool, and the next prompt would fail
with `BRT-ORC-007`.

## Backup & Restore
Back up `data/borg_route.db` (the SQLite DB) and `data/capability_matrix.yaml`
(scores + checkboxes). That is the full state. No other files carry
user-specific configuration.

## When BORG_ROUTE Pays Off — Honest Expectations

> **TL;DR:** For single-turn conversational prompts under ~500 characters,
> expect BORG to cost roughly 10–20× more than a direct call to the
> orchestrator model. BORG's efficiency emerges at scale
> (N > ~130 bulk items), for oversized corpora, or for mixed-specialty
> tasks. Read this section before judging BORG on a single short prompt.

### Measured cost comparison (same prompt, two paths)

Prompt: *"Write a short technical deep-dive on three PostgreSQL features
(MVCC, WAL, Vacuum). For each: what it is, why it exists, one practical
gotcha."* — ~130 prompt tokens, ~370 response tokens.

| Path | Wall time | Input tokens | Output tokens | Cost (actual) |
|---|---|---|---|---|
| **BORG orchestrated** | 34.0s | 13,169 | 1,086 | **~$0.00370** |
| **Direct call** (deepseek-v3.2 via OpenRouter) | 11.3s | 133 | 371 | **$0.000176** |
| Ratio | ~3× slower | 99× | 2.9× | **~21× more expensive** |

Quality of the two responses was indistinguishable. All BORG's useful
work happened in compose mode; the fan-out phase added no content.

### Cost model

```
BORG_cost  ≈  fixed_overhead  +  N × leaf_cost_per_item
           ≈  ~$0.0035 (3 orchestrator calls on a mid-premium model)
              +  per-item work
Direct     ≈  (prompt_in + response_out) × orchestrator_rate
```

Savings require **both** conditions:
1. **Price gradient in the pool.** Orchestrator must be meaningfully more
   expensive than the leaf model. A deepseek-only pool has no gradient.
2. **Enough leaf work to amortise overhead.** Leaf savings on a small task
   (~400 output tokens) are ~$0.00035, well below the $0.0035 overhead.

### Break-even (apply_to_each / bulk item tasks)

Orchestrator = claude-opus-4.7 ($0.005 in / $0.025 out per 1K),
leaves = deepseek-v3.2 ($0.00025 in / $0.00038 out per 1K), per-item
workload ~100 input + ~10 output tokens:

```
Direct_cost = 0.0005 × N + 0.025  (opus bundle)
BORG_cost   = 0.085 + 0.0000288 × N

Break-even at N ≈ 130 items.
```

- **N < 100 items** — direct to opus is cheaper.
- **N ≈ 130** — roughly even.
- **N = 500** — BORG ~2.7× cheaper.
- **N = 2000** — BORG ~6× cheaper.

For tasks that do not fit in a single model's context, "direct" is not an
option at all. BORG's cascading distillation becomes mandatory and the
cost comparison is N/A for direct.

### Three workloads where BORG wins

1. **Large-scale classification / extraction (apply_to_each-shaped).**
   *"Classify 200 emails, extract product name, score urgency 1-5.
   Summarise per-topic counts and top-3 urgent overall."* Fan out to
   cheap leaves; summary in compose. Cheaper than one bundled call to a
   premium model at N ≳ 130.
2. **Multi-source aggregation over oversized corpora.** *"15 articles
   (~25K tokens each, 375K total). Extract claims, evidence, numeric
   data. Find contradictions. Produce 500-word synthesis."* 375K exceeds
   most single-model contexts. Direct is infeasible. BORG cost ≈
   $0.15-0.25 depending on leaf choice.
3. **Specialist-mixed generation.** *"URL-shortener spec: Postgres schema
   + indexes, Go handlers, OpenAPI 3.0 YAML, docker-compose, Python
   integration tests."* Router picks `coding` vs. `reasoning` vs.
   `writing` per sub-part. Direct-to-opus ≈ $0.15-0.20; BORG
   ≈ $0.04-0.08. Roughly 2-4× cheaper and potentially higher per-part
   quality.

### Pool recipes for cost efficiency

| Recipe | When | Ticked rows |
|---|---|---|
| **Local-only dev** | iteration, no external spend | 1 mid-tier local (e.g. `lmstudio/google/gemma-4-26b-a4b`) as both orchestrator + answerer |
| **OpenRouter mixed** | you have a key but want to test the router | 1 premium OR row as orchestrator (e.g. `openrouter/anthropic/claude-sonnet-4`) + 2-3 cheap OR rows for leaves (`openrouter/deepseek/deepseek-v3.2`, `openrouter/x-ai/grok-4`) |
| **Premium-orchestrator + cheap-leaves** | real bulk workload | one frontier orchestrator (opus/GPT-5) + one or two bulk leaves. This is where the break-even math pays off; pin via `orchestrator_endpoint` + `orchestrator_model_name` for determinism. |

### Rule Zero shortcut — what it fixes (BRT-ZERO-01)

The orchestrator prompt already contains Rule Zero ("Direct mode is the
default; 70-80% of conversation turns should be Direct"). A cheap,
deterministic pre-classifier now bypasses plan → re_plan entirely for
unambiguously conversational short prompts (`< 500 chars`, no list
markers, no "for each"/"classify these" phrases). One compose call
instead of three — the ~$0.0035 overhead on "hi" drops to a single
compose-mode call.

The shortcut is on by default. To force the full plan path (useful for
debugging), set `BORG_RULE_ZERO_SHORTCUT=false` in `.env` or write
`rule_zero_shortcut=false` via the `/config` API.

---

## Troubleshooting
See `docs/errors/error-registry.md` for the full error-code reference.
Most common on a fresh install:
- `BRT-REG-020` — no row checked. Tick at least one on the Models UI.
- `BRT-REG-030` — scorer endpoint unreachable. Check `OPENROUTER_API_KEY` and network.
- `BRT-REG-032` — `data/curated_models.yaml` missing. Restore from source tree.
- `BRT-ORC-007-DEGRADED` — warning only; quality bar fell back to 5.1. Run Get Model Info to refresh scores.
