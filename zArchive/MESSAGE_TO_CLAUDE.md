# Message to Claude — BORG_ROUTE Onboarding
> Last updated: 2026-04-18 (Session 2 — Architectural Reassessment)
> Purpose: AI agent onboarding. Read this FIRST, then follow the reading order below.

---

## Mission

BORG_ROUTE is an **Adaptive Recursive Agent Router (ARAR)** — a cost-aware
orchestration system where the smartest model acts as a **project manager**:
it sees the shape of a task, builds a work breakdown structure (WBS), and
routes each step to the cheapest capable model. Intelligence is spent on
**structuring**, not **doing**.

The product exists because most systems today hand everything to the biggest
model available. That is wasteful when the task can be decomposed and each
piece handled by a cheaper specialist. BORG_ROUTE makes that decomposition a
first-class, portable architecture — not a hack.

---

## Document Map

| Document | Purpose | When to read |
|----------|---------|--------------|
| `MESSAGE_TO_CLAUDE.md` (this file) | Onboarding, architecture at a glance | Always first |
| `DESIGN_PRINCIPLES.md` | The laws, patterns, and why of the architecture | Second — essential depth |
| `CHECKPOINT.md` | Current state, where we left off | Third |
| `BUILD_PLAN.md` | Slice-based plan with task-level detail | When planning work |
| `HANDOVER_TO_BUILDER.md` | Builder agent operating procedure | If you are the builder |
| `CODING_STANDARD_CORE.md` | Commit rules, error codes, testing | Before any code |
| `docs/errors/error-registry.md` | BRT-* error codes | When debugging |
| `docs/tests/test-plan.md` | Test cases | When writing tests |
| `.env.example` | Environment variables | When configuring |

---

## Reading Order (every session)

1. `MESSAGE_TO_CLAUDE.md` (this file) — architectural overview
2. `DESIGN_PRINCIPLES.md` — laws and patterns (non-negotiable context)
3. `CHECKPOINT.md` — current slice status and "where I left off"
4. `CODING_STANDARD_CORE.md` — commit and testing rules
5. `BUILD_PLAN.md` — only the current slice section

---

## Current State

- **Position:** Slice 0 (Scaffolding) complete. Slice 1 begins next.
- **Docs:** architecture fully reassessed in Session 2; written to disk.
- **Code:** none yet.
- **Tests:** none yet.
- **Known gaps:** listed in `CHECKPOINT.md` and `DESIGN_PRINCIPLES.md §9`.

---

## Architecture at a Glance

### The Three Core Patterns
(see `DESIGN_PRINCIPLES.md` for full detail)

1. **Wrapper-as-Compression Contract.** The inbound envelope schema is a
   declarative compression specification. The orchestrator compresses by
   choosing what shape of data it wants back.

2. **Progressive Distillation.** Compression is a by-product of work. Every
   answerer's structured output IS the compressed form of whatever raw
   material it consumed. No separate compression step needed for current-run
   data.

3. **Cascading Distillation.** When one pass of compression isn't enough,
   cascade another pass. Each fold upward compresses information further.

### The Two Laws
(see `DESIGN_PRINCIPLES.md §2` for full detail)

1. **Law of Uncompacted Governance.** Global Rules, user instructions, and
   admin-UI-added system instructions pass through every cascade verbatim.
   Governance is additive across levels, never diluted.

2. **Law of No Forgotten Scratch.** Children clean up their own SQLite
   scratch before returning. Parents verify at fold-in; residue triggers a
   `child_residue` diagnostic event and is swept anyway. Data never lies
   around to be misused by later agents.

### Unified Orchestrator/Composer

The orchestrator and composer are **the same brain wearing different hats**.
One unified system prompt; a `current_mode` flag distinguishes turns:
- `plan` — build the initial WBS
- `dispatch` — emit an envelope or a batch of envelopes
- `re_plan` — after a stage completes, decide next moves
- `compose` — final synthesis turn

No separate Composer agent. The agent that dispatched the work is the one
that writes the final answer — it has the full picture.

### Task Types (DAG Node Kinds)

| Type | Role | Returns |
|------|------|---------|
| `orchestrate` | Recursive sub-plan | Folded into parent's context |
| `answerer` | Single atomic leaf task | MUST return `resolved` or `failed` |
| `apply_to_each` | Batch of identical tasks over a list | Aggregated array of results |
| `state_op` | Deterministic (no-model) operation: filter, count, group_by, sample | Computed result |

The executor is uniform — `execute()` handles all four by type.

### Shared-State Orchestration (Four Mechanisms)

Instead of a fixed DAG, the system supports emergent patterns through:
1. `state_query` on envelope inputs — SQL read before dispatch
2. `side_effects` on envelope outputs — declared, structured DB writes
3. Orchestrator re-invocation mid-run with current state summary
4. State operators — deterministic nodes for cheap data manipulation

Examples of emergent patterns these enable: triage + deferral, confidence
escalation, cross-agent breadcrumbs, hypothesis-and-check, self-triggered
re-routing. (Full list: `DESIGN_PRINCIPLES.md §4.1`.)

### Wrapped Task Schemas

**Outbound (from orchestrator to executor):**
```json
{
  "envelope_id": "uuid",
  "run_id": "uuid",
  "parent_run_id": "uuid|null",
  "subtask_id": "uuid",
  "type": "orchestrate|answerer|apply_to_each|state_op",
  "current_depth": 0,
  "max_depth": 5,
  "level_path": "4d",
  "model_endpoint": "http://...",
  "model_name": "string",
  "instruction": "precise, atomic",
  "context": "distilled slice only",
  "state_query": "optional SQL to read before dispatch",
  "side_effects": ["deferrals", "cross_references"],
  "dependency_input": {},
  "output_schema": {},
  "max_output_tokens": 500,
  "trigger_reason": "initial_plan|escalation_from:id|..."
}
```

**Inbound (from agent back to executor):**
```json
{
  "envelope_id": "uuid",
  "subtask_id": "uuid",
  "status": "resolved|failed",
  "confidence": 0.85,
  "output": {},
  "tokens_used": { "input": 312, "output": 148 }
}
```

Answerer contract: `status` MUST be `resolved` or `failed`. `unresolved` is
invalid. Raise `BRT-ANS-001` if violated.

### Pipeline Flow

```
CONVERSATION THREAD (SQLite — full history, never truncated)
        │
        ▼
  ORCHESTRATOR (mode=plan)
  Reads thread → designs WBS → emits DAG nodes
        │
  ┌─────┼──────┬────────────────┐
  ▼     ▼      ▼                ▼
answerer apply_to_each state_op  orchestrate (recursive)
                                    ↓
                              [child DAG at depth+1]
        │
        ▼
  FOLD-IN: parent inspects SQLite scratch at child's level
           → digests into parent's context
           → verifies cleanup, logs child_residue if any
           → deletes child's scratch
        │
        ▼
  ORCHESTRATOR (mode=re_plan) — decide next move, or compose
        │
        ▼
  ORCHESTRATOR (mode=compose)
  Context bucket: high-res governance + distilled evidence + compact history
  Produces response that continues the conversation naturally
        │
        ▼
  THREAD (response written back)
```

Depth guard: `current_depth >= max_depth` → `orchestrate` demoted to `answerer`.
Hard limit: `MAX_RECURSION_DEPTH = 5` constant. Default `max_depth = 1` (config).

### The Context Bucket

The orchestrator curates its own working context throughout a run. At any
moment it conceptually holds:

```
context_bucket = {
  "governance":        [global_rules, user_instruction, admin_rules],  # verbatim
  "high_res":          [current_turn_context, pinned_facts],
  "structured_evidence": [envelope_outputs by subtask_id],             # compressed
  "compact_history":   rolling_summary_of_older_turns,
  "state_snapshots":   relevant_db_rows
}
```

Not stored as-is — assembled on the fly from existing tables.

### Conversation Memory

- Full thread stored in SQLite, never truncated.
- Orchestrator receives: last N turns (window) + rolling summary of overflow.
- Answerers receive: distilled task slice only — never the full thread.
- Current-run compression happens **for free** via progressive distillation.
- Only **inactive historical** turns need a dedicated compressor call.
- Window size configurable via Admin UI (`config` table).

### Global Rules + Admin-Added Instructions

- Admin UI text → stored in `config` table as `global_rules` and
  `admin_instructions` (ledger, append-only with timestamps).
- Prepended to EVERY model system prompt via `src/prompts/base.py::build_system_prompt()`.
- **Additive, never replacing.** New rules join existing governance; old
  rules remain until explicitly retired.
- No model call anywhere bypasses this.

### Deep Logging (for tuning, not audit)

Every model call writes a structured log event (tokens in/out, cost estimate,
latency, decision trace). Full text stored in `log_payloads`. spaCy NER runs
async, extracting entities + relationships into `log_entities` + `log_relations`.

New event types beyond basic calls:
- `orchestrator_decision` — one per orchestrator invocation; prompt-input
  digest + verbatim reasoning + plan
- `child_residue` — emitted when parent's fold-in sweep finds unclean scratch
- `escalation` / `deferral` / `reorchestration` — state transitions

Observability is for **analysis and tuning**, not audit. A Stats page
(simple charts, no AI critic) is the primary human surface.

---

## Cost Model

One unified formula across local, external, and mixed setups:

```
effective_cost =
    (tokens_in × input_rate + tokens_out × output_rate)     # dollars
  + (tokens_total / throughput_tps) × time_cost_per_sec      # time → dollars
```

- **External:** rates come from `GET https://openrouter.ai/api/v1/models`.
  OpenRouter is the pricing authority; it gives real numbers per token +
  context length per model.
- **Local:** `pricing = null`; `throughput_tps` measured during benchmark.
  Tokens/sec is an inverse cost — slow local models are expensive in time.
- `time_cost_per_sec` is an Admin UI knob (default ~$30/hr worth of waiting).

### Router Signature

```python
select_model(task_type, est_input_tokens, quality_requirement, strategy):
    candidates = capability_matrix.query(task_type)
    candidates = filter(lambda m:
        m.score_for_task(task_type) >= quality_requirement
        and m.context_length >= est_input_tokens)
    candidates = sort_by(effective_cost)
    return candidates[0]
```

Specialty-primary, not tier-primary. A 4B coder can beat a 12B generalist on
code tasks. "Tier" (light/mid/heavy) is a derived concept from quality_requirement.

---

## Deployment

Local-first. FastAPI serves API and static HTML UI on localhost. Models via
LM Studio (ports 1234–1237) or Ollama. External models through OpenRouter
(primary) or direct API keys (up to 5 providers via Admin UI).

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11+, FastAPI, asyncio |
| State + Logs | SQLite via `aiosqlite` |
| Model calls | `httpx` async, OpenAI-compat `/v1/chat/completions` |
| Pricing | OpenRouter API |
| Config | `.env` via `python-dotenv` |
| Frontend | HTML/CSS/Vanilla JS (no framework) |
| Discovery | `httpx` ping + Ollama REST + OpenRouter API |
| NER | `spacy` + `en_core_web_sm` (async background worker) |
| Deps | `pyproject.toml` + `uv` |

---

## Key Conventions (never violate)

- **Error codes:** `BRT-[MODULE]-[NNN]`. Register every new code in
  `docs/errors/error-registry.md`.
  Modules: `REG, ENV, ORC, ANS, APE (apply_to_each), SOP (state_op), CMP, DB,
  LOG, API, MEM, ADM`.
- **Answerer contract:** MUST return `resolved` or `failed`. Never `unresolved`.
- **Governance is uncompactable.** Global rules and user instructions pass
  verbatim through every cascade. Enforce at `build_system_prompt()`.
- **Children self-clean.** Parents verify. Residue = diagnostic signal.
- **Every model call goes through `build_system_prompt()`.** No bypasses.
- **Every model call writes a log event.** No silent calls.
- **Secrets in `.env` only.** Never in code.
- **Follow the Parallel Commit Law** — every `feat:` commit ships code,
  error handling, user-guide entry, presentation bullet, and tests together.

---

## Routing Strategies

| Strategy | Behaviour |
|----------|-----------|
| `LOCAL_FIRST` | Prefer local (LM Studio/Ollama); escalate to external only when tier not met locally |
| `SYSTEM_CHOOSE` | Capability matrix picks best model per task — whichever wins on effective_cost |

---

## Web UI Pages

| Page | Purpose |
|------|---------|
| Chat | Conversation input, streaming response, thumbs up/down feedback |
| Models | Discover, capability matrix, benchmark, specialty view |
| API Keys | Up to 5 external providers (incl. OpenRouter) → `.env` |
| Admin | Global Rules (append), admin instructions ledger, depth 1–5, routing strategy, window size, time_cost_per_sec |
| Dashboard | Run history, active runs, Stats tab (cost/outlier/feedback charts) |
| Run Detail | Per-envelope trace, type badge, level path, token costs, apply_to_each expansion, child DAG drill-down |
| Logs | Event log, entity browser, JSONL export |

---

## Session Checklists

### Session Start
1. Read `MESSAGE_TO_CLAUDE.md` → `DESIGN_PRINCIPLES.md` → `CHECKPOINT.md`
2. Confirm current slice (from `CHECKPOINT.md`)
3. Start model services (LM Studio / Ollama) if needed
4. Run tests — note count before touching anything

### Session End
1. All tests pass
2. `BUILD_PLAN.md` — check off completed tasks in current slice
3. `CHECKPOINT.md` — update "Where I Left Off" + slice status
4. `docs/errors/error-registry.md` — register any new error codes
5. `docs/tests/test-plan.md` — register any new test cases
6. `.env.example` — add any new variables
7. Commit + push with meaningful message (per `CODING_STANDARD_CORE.md`)
8. Remind Deej to say **"save to memory"** if anything worth keeping was learned
