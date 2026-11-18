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
2. **Calling `POST /models/research` before `POST /models/discover`.** Scorer runs against the matrix, which ships with 12 curated cloud rows; local rows appear only after Discover. Order is: (1) start local model servers, (2) discover, (3) research.
3. **Forgetting `OPENROUTER_API_KEY`.** Without it, OpenRouter discovery AND the scorer (default `perplexity/sonar-pro-search`, routed via OpenRouter) both fail. Set the key in `.env` before discover/research.
4. **Using `copy` on Linux or `cp` in raw PowerShell.** See "Platform detection" above; prefer the Python one-liner.
5. **Assuming every row in the matrix has real scores.** A model that Discover has just found has no scores and is not chosen by score-based routing until `POST /models/research` (Get Model Info) runs; the 12 curated rows ship with scores, and Discover keeps existing scores. If no ticked model meets the planner's quality bar, the planner falls back with a `BRT-ORC-NO-FRONTIER` trace event. This is normal; running research is what fills the scores in.
6. **Skipping the checkbox step.** On a fresh install only the two cold-start OpenRouter rows are checked (see "Cold-start defaults"); every other row is `checked: false`. If the operator unticks them all, then until the operator (or you, via `PATCH /models/{id}/checked`) ticks at least one model, the run fails with `BRT-ORC-007 no orchestrator model available`.
7. **Trying to benchmark.** There is no `POST /models/benchmark` any more. It was replaced by `POST /models/research`. If you see stale docs mentioning benchmark, they are wrong.

### Skip spaCy for now
Slice 5 (NER worker) is parked by default. The previous install step
`python -m spacy download en_core_web_sm` is **not required** for a
functioning system today. Skip it. Only run it if the operator
specifically asks for NER. (The `spacy` library itself is still a
dependency because tests import it, but the language model download
is optional.)

**Test counts:** on a fresh install without `en_core_web_sm`, the three NER worker tests skip automatically (TC-S5-010 / 011 / 012); installing the language model turns those skips into passes. Run `python -m pytest` and expect no failures; the total grows with the project, so do not rely on a fixed number.

### Success criteria
The install is done when all of these pass:
- `python -m pytest` → exits 0; all tests pass (the count grows over time)
- `GET http://localhost:8000/health` → `{"status":"ok"}`
- `GET http://localhost:8000/models/matrix` → returns the 12 curated OpenRouter rows (plus one row per local model, after Discover); only the two cold-start rows have `checked: true`
- At least one row has been ticked with `PATCH /models/<id>/checked` (any one you plan to use, for example a model id as LM Studio shows it, like `your-org/your-model`)

---

## Prerequisites
- Python **3.11 or newer** (the CI workflow runs 3.11 and 3.13).
- `pip` (bundled with Python).
- Optional: **LM Studio** (Windows / macOS / Linux builds — all use ports 1234-1237).
- Optional: **Ollama** (Linux-native, macOS + Windows builds exist — port 11434).
- Optional: **Ollama in Docker** (`docker run -d -p 11434:11434 ollama/ollama`, then pull a model) — an easy way to get a local model without a native install; it answers on port 11434, the default address.
- Optional: **OpenRouter API key** for external models AND for the capability scorer.

## Quick Start (Local Development)

### Windows (PowerShell)
```powershell
cd BORG_ROUTE                              # the folder you cloned or extracted

copy .env.example .env
# Edit .env — add OPENROUTER_API_KEY (and any LM Studio paths)

python -m pip install -e ".[dev]"
python -m pytest                            # expect all tests to pass

uvicorn main:app --reload
# Server at http://localhost:8000
```

### Linux / macOS / Git Bash
```bash
cd /path/to/BORG_ROUTE                      # wherever you cloned it

cp .env.example .env
# Edit .env — add OPENROUTER_API_KEY (and any LM Studio paths)

python3 -m pip install -e ".[dev]"
python3 -m pytest                           # expect all tests to pass

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
3. **Click Discover.** Expect one row per model your local server lists (if it is running) + 12 curated OpenRouter rows.
4. **Click Get Model Info.** The scorer runs in batches and can take a minute or more; it fills relative 0-10 task scores per row. Requires `OPENROUTER_API_KEY`.
5. **Tick the checkbox** on any row you want in today's routing pool (typical: 3-6 total).
6. **Kick a test run** from `http://localhost:8000/ui/` (it opens the Ask page) — the orchestrator will route to one of the checked models.

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
an in-process run registry and SQLite DB. Scale vertically (a faster CPU) instead.

## Environment Variables
See `.env.example` — every variable is documented inline. The ones you
are most likely to touch during install and operation are grouped below.

### Provider credentials
| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | (unset) | Required for OpenRouter discovery, the capability scorer, and the Perplexity web-search fallback. |
| `TAVILY_API_KEY` | (unset) | Optional. When set, Tavily becomes the primary `web_search` provider (cheaper than Perplexity). Without it, `web_search` silently falls back to Perplexity via OpenRouter and logs `BRT-TOOL-DEGRADED` once per process. |

### Admin token
| Variable | Default | Purpose |
|---|---|---|
| `BORG_ADMIN_TOKEN` | (unset) | Locks every route that changes settings, keys, rules, models or skills, plus `GET /logs`, `GET /logs/entities`, `GET /logs/export` and, for the request bodies and event text in `GET /runs/{id}`, anyone without it. Clients send `Authorization: Bearer <token>`. ASCII only: letters, digits and punctuation, no spaces or accents (anything else is reported as misconfigured, `BRT-API-047`). |

How to set it: put a long random value in `.env` (`BORG_ADMIN_TOKEN=...`) and restart the server. The Admin pages ask for it the first time a call is refused and keep it for that browser tab only.

The loopback rule: with the variable unset or empty, admin routes answer only to requests from this computer (`127.0.0.0/8` and `::1`). Any other address gets 403 (`BRT-API-046`). The server (uvicorn) trusts `X-Forwarded-For` from `127.0.0.1` by default (`proxy_headers` is on), so a reverse proxy on the same machine that sets that header to the visitor's address is handled: the visitor counts as remote and gets 403. The real hole is a proxy that sends no `X-Forwarded-For`, or passes the client's own header through unchanged, and a container whose proxy is not on `127.0.0.1`: then every visitor looks local, or a visitor can claim to be. **Set `BORG_ADMIN_TOKEN` whenever the app is reachable from another machine** (a proxy, Docker port mapping, a LAN, a VPS); do not rely on the loopback rule there. Asking questions (`/v1/chat/completions`, `/v1/task`, the MCP tools) and reading a run stay open. A wrong or missing token gets 401 (`BRT-API-045`).

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
| `LEAF_QUALITY_LIGHT` / `LEAF_QUALITY_MID` / `LEAF_QUALITY_HEAVY` | `6.0` / `7.5` / `9.0` | Quality floor of each step tier. |
| `BASELINE_MODEL_ID` | (unset) | Reference model the router prices each step on, kept in the record; the pages show no comparison. |
| `BORG_ANSWERER_CALL_TIMEOUT` / `BORG_ORCHESTRATOR_CALL_TIMEOUT` | `120` / `180` | Seconds one step call or one planner/compose call may take (`BRT-ANS-006` / `BRT-ORC-001`). |

### Safety caps
| Variable | Default | Purpose |
|---|---|---|
| `BORG_MAX_RUN_SECONDS` | `300` in code (`.env.example` sets `500`) | Hard wall-clock budget per run. Runs that exceed this are cancelled and marked `failed` with `BRT-PIP-008`. Protects against runaway / stuck pipelines burning real money. Raise for long research tasks. |

### On-premise lane
| Variable | Default | Purpose |
|---|---|---|
| `BORG_LANE_MODE` | `off` | `off`, `raw_local`, `all_local` or `by_reference`. |
| `BORG_DOCS_ROOT` | (unset) | The one folder documents may be read from. Unset: any request with documents is refused (`BRT-LANE-001`). |
| `BORG_LOCAL_HOSTS` | (unset) | Extra hosts that count as this machine. |
| `BORG_LANE_MAX_OUTPUT_CHARS`, `BORG_LANE_MAX_VERBATIM_CHARS`, `BORG_LANE_MAX_NGRAM_SHARE`, `BORG_LANE_MANIFEST`, `BORG_LANE_LOCAL_ALLOWLIST`, `BORG_LANE_PART_CHARS` | `1500`, `120`, `0.10`, `names`, empty, `8000` | Lane limits; see the user guide, "On-premise lane". |

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
| `openrouter/moonshotai/kimi-k2-thinking` | 9.2 | cheaper per token than Sonnet, so the cost-based router prefers it when both qualify |

Both meet the default `ORCHESTRATOR_QUALITY_REQUIREMENT=9.0` bar. The router's
cost optimiser picks the cheaper model per call (Kimi for most prompts)
and Sonnet is the other ticked row when Kimi cannot serve a step (for
example when a step needs more context than Kimi has). Operators can uncheck
either or both at `/ui/models.html` and check on different rows.

If every row gets unchecked, the Models page shows a red banner reminding
the operator that no model is in the pool, and the next prompt would fail
with `BRT-ORC-007`.

## Backup & Restore
Back up `data/borg_route.db` (the SQLite DB) and `data/capability_matrix.yaml`
(scores + checkboxes). That is the full state. No other files carry
user-specific configuration.

## When BORG_ROUTE Pays Off: Honest Expectations

BORG_ROUTE adds planning and per-step overhead: a planner call, a final
write-up call, and a prompt wrapper on every step. A short single-shot
prompt therefore costs more, and takes longer, than one direct call to a
model. Do not judge BORG_ROUTE on a single short prompt.

Where it is meant to help is work that one call handles badly: long lists
of items that can be split into cheap per-item calls, material too large
for one model's context, and jobs that mix kinds of work. Whether it
actually saves money on those is a measured question, not a promise.

The first bench round (17-Nov-2026) did not show a saving. Its results,
including the failures, are published in
`bench/results/2026_11_17/summary.md`. Later results will be linked there.
Until then, treat any saving as unproven and test your own workload with
your own pool.

### Pool recipes

| Recipe | When | Ticked rows |
|---|---|---|
| **Local-only dev** | iteration, no external spend | 1 mid-tier local model (as shown on the Models page) as both planner and step model |
| **OpenRouter mixed** | you have a key and want to try the router | 1 premium row for planning (e.g. `openrouter/anthropic/claude-sonnet-4.6`) + 2-3 cheaper rows for steps (e.g. `openrouter/deepseek/deepseek-v3.2`, `openrouter/x-ai/grok-4.1-fast`) |
| **Premium planner, cheap steps** | a bulk workload | one frontier planner + one or two cheaper step models; pin the planner on the Admin Settings page (`orchestrator_endpoint` and `orchestrator_model_name`) for determinism |

### Rule Zero shortcut — what it fixes (BRT-ZERO-01)

A cheap,
deterministic pre-classifier now bypasses plan → re_plan entirely for
unambiguously conversational short prompts (`< 500 chars`, no list
markers, no "for each"/"classify these" phrases, no live-data words such as "latest" or "news"). One compose call
instead of three — a greeting costs a single compose-mode call.

The shortcut is on by default. To force the full plan path (useful for
debugging), set `BORG_RULE_ZERO_SHORTCUT=false` in `.env` or write
`rule_zero_shortcut=false` via the `/config` API.

---

## Troubleshooting
See `docs/errors/error-registry.md` for the full error-code reference.
Most common on a fresh install:
- `BRT-ORC-007` — no row checked. Tick at least one on the Models UI.
- `BRT-REG-030` — scorer endpoint unreachable. Check `OPENROUTER_API_KEY` and network.
- `BRT-REG-032` — `data/curated_models.yaml` missing. Restore from source tree.
- `BRT-ORC-NO-FRONTIER` — warning only (trace event); no ticked model met the quality bar, so the planner fell back to quality 5.1. Run Get Model Info to refresh scores.
