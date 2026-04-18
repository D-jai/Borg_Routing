# Message to Claude — BORG_ROUTE Handover
> Last updated: 2026-04-17 (Session 1 — Architecture & Planning)
> Purpose: AI agent onboarding. Read this FIRST, then follow the reading order below.

---

## Mission
BORG_ROUTE is an **Adaptive Recursive Agent Router (ARAR)** — a cost-aware orchestration system
that routes tasks to the cheapest capable model, distills conversation context into minimal
agent packets, and assembles coherent final responses that fit naturally into the conversation
stream.

**Core design principle:** Input tokens are a sunk cost once the orchestrator reads the thread.
The only variable is who generates the output. The system chooses the cheapest path that meets
the quality requirement.

---

## Document Map

| Document | Purpose | When to read |
|----------|---------|--------------|
| `MESSAGE_TO_CLAUDE.md` (this file) | Onboarding, architecture, quick reference | Always first |
| `CHECKPOINT.md` | Current state, phase statuses, known gaps | After this file |
| `BUILD_PLAN.md` | Full 7-phase plan with task-level detail | When planning work |
| `HANDOVER_TO_BUILDER.md` | Builder agent instructions + housekeeping rules | If you are the builder agent |
| `CODING_STANDARD_CORE.md` | Rules for every commit — always apply | Before writing any code |
| `docs/errors/error-registry.md` | All error codes and hints | When debugging |
| `docs/tests/test-plan.md` | Test strategy and coverage | When writing tests |
| `.env.example` | All env vars with placeholder values | When deploying |

---

## Reading Order

**Phase 1 — Orientation (once per session)**
1. This file — read fully
2. `CHECKPOINT.md` — pay attention to "Where I Left Off"
3. `CODING_STANDARD_CORE.md`

**Phase 2 — Context for current task**
4. `BUILD_PLAN.md` — read only the phase you're working on
5. Source files — read on demand as you work

---

## Current State
- **Phase:** Phase 1 — Model Registry + Discovery (not yet started)
- **What works:** Project scaffolded, architecture fully planned
- **What's next:** `pyproject.toml`, `main.py`, `src/registry/discovery.py`
- **Known gaps:** No code written yet

---

## System Architecture

### The Cost Decision
```
cost_direct = orchestrator_output_tokens × big_model_rate
cost_route  = context_packet_tokens × input_rate
            + agent_output_tokens   × small_model_rate

if cost_route < cost_direct → route
else → answer directly
```

### Three Orchestrator Modes

| Mode | Trigger | What happens |
|------|---------|--------------|
| **Direct** | Small context, short answer | Orchestrator answers. Zero handoff. |
| **Router** | Clear task, minimal context | Thin packet: task + 1–2 relevant facts |
| **Distiller** | Long thread, specialist needed | Extracts + compresses context per agent |

### Task Type System
Every wrapped task has a `type` field. `run()` is uniform — it handles all three:

| Type | Role | Contract |
|------|------|----------|
| `answerer` | Leaf node — executes one atomic task | MUST return `resolved` or `failed`. Never `unresolved`. |
| `orchestrate` | Spawns child orchestrator + DAG | Demoted to `answerer` if depth cap reached |
| `composer` | Final synthesizer — assembles answer | Depends on all siblings. Heavy model. Gets conversation window. |

### Wrapped Task Schema
```json
// OUTBOUND
{
  "envelope_id": "uuid",
  "run_id": "uuid",
  "parent_run_id": "uuid|null",
  "subtask_id": "uuid",
  "type": "answerer|orchestrate|composer",
  "current_depth": 0,
  "max_depth": 1,
  "model_endpoint": "http://...",
  "model_name": "string",
  "agent_tier": "light|mid|heavy",
  "instruction": "precise, atomic",
  "context": "distilled slice only",
  "dependency_input": {},
  "output_schema": {},
  "max_output_tokens": 500
}
// INBOUND
{
  "envelope_id": "uuid",
  "subtask_id": "uuid",
  "status": "resolved|unresolved|failed",
  "confidence": 0.85,
  "output": {},
  "tokens_used": { "input": 312, "output": 148 }
}
```

### Pipeline Flow
```
CONVERSATION THREAD (SQLite — full history, never truncated)
          │
          ▼
  ORCHESTRATOR (medium model)
  Reads full thread → decides mode → builds DAG
          │
  ┌───────┼────────────────┐
  ▼       ▼                ▼
ANSWERER ANSWERER     type=orchestrate
(parallel)             → CHILD ORCHESTRATOR (depth+1)
                         ├─ Child Answerers
                         └─ Child Composer
                              └─► folds back as parent evidence
          │
          ▼
  COMPOSER (heavy model)
  depends_on: all siblings
  Receives: evidence bundle + conversation window
  Produces: response that fits the thread
          │
          ▼
  CONVERSATION THREAD (response written back)
```

Depth guard: `current_depth >= max_depth` → `orchestrate` demoted to `answerer`
Hard limit: `MAX_RECURSION_DEPTH = 5` (constant in code)
Default: `max_depth = 1` (admin-configurable, stored in `config` table)

### Conversation Memory
- Full thread stored in SQLite, never truncated
- Orchestrator + Composer receive: last N turns (window) + rolling summary of overflow
- Answerers receive: distilled task slice only — never the full thread
- Window size configurable via Admin UI (stored in `config` table)

### Global Rules
- Admin UI text field → stored in `config` table as `key="global_rules"`
- Prepended to EVERY model system prompt via `src/prompts/base.py::build_system_prompt()`
- No model call anywhere bypasses this — enforced at prompt-build layer
- Examples: "Do not fabricate facts.", "Cite sources.", "Respond in user's language."

### Deep Logging
Every model call is a structured log event with tokens in/out, cost estimate, latency, and
decision trace. Full text stored in `log_payloads`. spaCy NER runs async after the fact,
extracting entities + relationships into `log_entities` + `log_relations`.
Logs are designed to be consumed by an analyst model for fine-tuning and optimisation.

---

## Deployment Architecture
Local-first. FastAPI serves the API and static HTML UI on localhost.
Models served via LM Studio (ports 1234–1237) or Ollama.
External models via `.env` API keys (up to 5 providers, OpenAI-compat).

---

## Tech Stack
| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11+, FastAPI, asyncio |
| State + Logs | SQLite via `aiosqlite` |
| Model calls | `httpx` async, OpenAI-compat `/v1/chat/completions` |
| Config | `.env` via `python-dotenv` |
| Frontend | HTML/CSS/Vanilla JS (no framework) |
| Discovery | `httpx` ping + Ollama REST API |
| NER | `spacy` + `en_core_web_sm` (async background worker) |
| Deps | `pyproject.toml` + `uv` |

---

## Key Conventions (never violate)
- Error codes: `BRT-[MODULE]-[NNN]` — register every code in `docs/errors/error-registry.md`
  Modules: `REG, ENV, ORC, ANS, CMP, DB, LOG, API, MEM, ADM`
- Answerer contract: leaf nodes MUST return `resolved` or `failed` — `unresolved` is invalid
- Every model call goes through `build_system_prompt()` — never bypass global rules
- Every model call writes a log event — no silent calls
- Secrets in `.env` only — never in code
- Follow `CODING_STANDARD_CORE.md` Parallel Commit Law on every `feat:` commit

---

## Routing Strategies
| Strategy | Behaviour |
|----------|-----------|
| `LOCAL_FIRST` | Prefer local (LM Studio/Ollama); escalate to external only if tier not met |
| `SYSTEM_CHOOSE` | Capability matrix selects best model per task type, local or external |

---

## Web UI Pages
| Page | Purpose |
|------|---------|
| Chat | Conversation input, streaming response |
| Models | Discover models, capability matrix, benchmark |
| API Keys | Up to 5 external providers → `.env` |
| Admin | Global Rules, depth (1–5), routing strategy, window size |
| Dashboard | Run history, active runs, cost summary, resume |
| Run Detail | Per-envelope trace, type badge, depth, token costs, child DAG |
| Logs | Event log, entity browser, JSONL export |

---

## Session Start Checklist
```
1. Read CHECKPOINT.md — especially "Where I Left Off"
2. Start model services (LM Studio / Ollama) if needed
3. Run tests — check expected count in CHECKPOINT.md
4. Pick up from CHECKPOINT.md
```

## Session End Checklist
```
1. All tests pass
2. BUILD_PLAN.md — check off completed tasks
3. CHECKPOINT.md — update "Where I Left Off" + phase status
4. Commit and push (meaningful commit message per CODING_STANDARD_CORE.md)
5. Remind Deej to say "save to memory" if anything significant was learned
```
