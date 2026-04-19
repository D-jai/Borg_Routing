# Design Principles — BORG_ROUTE
> Draft output of Reassessment Session 2 (2026-04-18)
> Status: Decisions captured in real-time during architectural reassessment.
> To be merged into MESSAGE_TO_CLAUDE.md / BUILD_PLAN.md after review.
> This document describes WHY and HOW; MESSAGE_TO_CLAUDE.md describes WHAT.

---

## 1. Product Thesis

> The orchestrator is a project manager, not a worker. Its job is to see the
> shape of the work and build a good WBS (Work Breakdown Structure), matching
> the intelligence required at each step to the cheapest model that can deliver
> it. Big-model intelligence is spent **structuring**, not **doing**.

Every design decision below serves this thesis. If a decision does not serve it,
it should be challenged.

### Corollaries
- **Direct mode is the default.** The orchestrator answers directly unless
  decomposition produces materially better output or materially lower cost.
- **Quality-by-floor.** The curated model lineup ensures every available
  option is capable enough. There is no "sub-standard" model in the system.
- **Self-regulating.** Because the orchestrator itself is smart enough to
  recognise when routing isn't worth it, we do not need a meta-system
  (critic / coach) policing its decisions.

---

## 2. The Two Laws

These are architectural invariants. Violating either breaks the system.

### Law 1 — Uncompacted Governance
> Compression applies to content, never to governance.
>
> Global Rules, the original user instruction, task-specific invariants, and
> **any system-level instructions added via the Admin UI** pass through every
> cascade and every agent verbatim. System-level instructions are **additive**
> to whatever is already in context cascading down from previous models —
> never replacing, never diluting.

**Enforcement:** `src/prompts/base.py::build_system_prompt()` is the single
enforcement point. The governance bundle belongs in the system-prompt layer,
not the compressible context layer.

**What counts as governance (uncompacted):**
- Global Rules from Admin UI config
- Original user instruction for the current turn
- Admin-UI-added system instructions (additive, cascading)
- Task-specific invariants explicitly marked as such

**What counts as content (compressible):**
- Conversation history beyond the current turn
- Intermediate agent outputs
- Raw data being processed

### Law 2 — No Forgotten Scratch (Fold-in Pattern)
> Every write to SQLite has an owner. The child is responsible for cleaning
> up after itself. The parent verifies at fold-in and backstops if the child
> failed to comply. **Residue is a diagnostic signal that something went wrong.**

**The primary–verifier model:**
- **Primary responsibility — the child.** Before returning, the child reads
  its own scratch, digests what it needs into its structured output, and
  deletes its own writes. Well-behaved children leave no trace.
- **Verifier responsibility — the parent.** At fold-in, the parent inspects
  the child's level for any residual scratch. If residue exists, that is a
  **signal the child went off the rails** — log it, surface it, adjust trust
  for that agent/model, then clean up the residue anyway (defense in depth).

**The fold-in sequence:**
Levels exist concurrently — e.g. at a point in time you may have
`1a, 1b, 1c, 2a, 2b, 3a, 4a, 4b, 4c, 4d` all in flight.
Each sub-level folds into its parent in sequence:

```
4d does its work
  → 4d writes scratch during work
  → 4d reads its own scratch at end, digests into structured output
  → 4d deletes its own scratch
  → 4d returns output to 4c

4c receives 4d's output
  → 4c inspects 4d's level in SQLite
  → If residue found:
      - Log diagnostic event (child failed cleanup)
      - Increment off-rails counter for that agent
      - Digest the residue into 4c's context (don't lose the information)
      - Clean up the residue
  → 4c continues its own work using 4d's output + any residual insight

4c later folds into 4b — same pattern applies
```

**Why this matters beyond cleanup:**
Residue is free observability. The parent's verification sweep is how the
system detects misbehaving agents without any separate health-check infrastructure.
If a specific model consistently leaves residue, that is evidence to drop it in
the capability matrix or write a better inbound schema that constrains it more
tightly. The cleanup law is simultaneously a correctness guarantee and a
diagnostic instrument.

**Enforcement:**
- Agent prompts explicitly instruct self-cleanup as part of the protocol.
- The executor, at each fold-in moment, performs the verification sweep:
  `SELECT COUNT(*) FROM scratch WHERE level = {child_level}`. If non-zero,
  emit a `child_residue` event to logs with the agent's model_id.
- Residue is then read into the parent's context (for digestion) and deleted.
- Scratch is **ephemeral**. Structured envelope outputs are what **persist**.

---

## 3. Core Patterns

### 3.1 Wrapper-as-Compression Contract (core pattern)
> The inbound envelope's schema is a declarative compression specification.
> The orchestrator does not say "please compress" — it says "return me this
> shape." Compression happens automatically because the answerer must fit
> its work into the shape.

Example shapes and their implicit compressions:

| Task | Inbound schema | Implicit compression |
|------|----------------|----------------------|
| Classify 1 email | `{bucket: "A|B|C|D|E|F"}` | Email body → 1 character |
| Summarise 1 email | `{topic: str<=20w, people: str[], actions: str[]}` | Body → ~100 tokens |
| Summarise 50 classifications | `{themes: [{label, count, sample_ids}]}` | 50 rows → 1 overview |
| Find anomalies | `{anomaly_ids: uuid[], reason: str<=30}` | Full scan → pointers only |

### 3.2 Progressive Distillation (the by-product principle)
> Compression is not a separate step; it is a by-product of work you were
> going to do anyway.

At every stage, answerers simultaneously:
1. Get work done (classify, extract, summarise)
2. Produce compressed context (their structured output IS the condensed form)

The Composer never sees raw inputs for work already done — it sees the
sub-agents' conclusions. A run with 2000 sub-envelopes still produces a
Composer context on the order of "user question + 2000 small structured
results + recent turns." No separate compression call needed.

### 3.3 Cascading Distillation
> If one pass of compression isn't enough, cascade another pass.

```
Stage 1: apply_to_each over 2000 emails
  schema: {bucket, topic_phrase_5w}
  result: 2000 rows in DB + 2000 small records → still too much

Stage 2: compact_summary over Stage 1 results
  reads DB, returns: {theme_clusters: [...], outliers: [...]}
  result: ~20 structured clusters → fits in orchestrator context

Stage 3: orchestrator plans next step using the 20 clusters
  (raw 2000 rows live in DB, queryable on demand)
```

Each cascade is just another envelope dispatch — no new primitive required.
Bounded by `MAX_RECURSION_DEPTH = 5`.

### 3.4 The Context Bucket (multi-resolution context)
Conceptual structure assembled progressively during a run:

```
context_bucket = {
  "high_res":        [user_question, current_turn_context, governance],
  "structured_evidence": [envelope_outputs indexed by subtask_id],
  "compact_history": rolling_summary_of_older_turns,
  "state_snapshots": relevant_db_rows_at_composer_time
}
```

Not stored as-is — assembled at Composer invocation from existing tables.
Naming it makes the prompt and executor both clear about what they are
building toward.

### 3.5 Orchestrator = Composer (same brain, different hat)
> The Composer is not a separate agent. It is the orchestrator on its final
> turn, told it is time to synthesise.

**Prompt structure:** one unified system prompt with a `current_mode` flag:
`plan | dispatch | re_plan | compose`. The model has the full picture; it just
knows which action it is taking this turn.

**Consequence:** since the orchestrator curates its own context throughout the
run, and is also the eventual Composer, it is **building its own cognitive
environment** for its final synthesis turn. This is coherent; no handover
between agents means no information loss.

---

## 4. Execution Model

### 4.1 Shared-State Orchestration (four mechanisms)
The system is not only a DAG executor. It is a **shared-state collaborative
environment** where agents read and write to SQLite between steps. The DAG is
one execution mode; shared-state collaboration enables emergent patterns.

Four minimal mechanisms enable every emergent pattern:

1. **`state_query` field** on envelope inputs — parameterised SQL executed by
   the runtime before dispatch; result injected as context.
2. **`side_effects` field** on envelope outputs — declared, structured writes
   the agent is allowed/expected to make to named tables.
3. **Orchestrator re-invocation** mid-run — not just once at the start; it is
   invoked with current state summary whenever a re-plan is needed.
4. **State operators** — deterministic (no-model) nodes: `filter`, `group_by`,
   `count`, `sample`. They manipulate state cheaply between model stages.

With these four, patterns emerge:
- Triage + deferral (light model flags uncertain items; orchestrator escalates)
- Confidence escalation (`WHERE confidence < threshold` → re-route)
- Cross-agent breadcrumbs (agents write structured notes for siblings)
- Hypothesis-and-check (light guess + verification sample)
- Self-triggered re-routing (agent flags "needs help"; orchestrator matches)

No new DAG node types required. Patterns emerge from composition.

### 4.2 `apply_to_each` Primitive
> First-class primitive for "do this same thing to N items."

Schema sketch:
```json
{
  "type": "apply_to_each",
  "instruction": "Classify this email into one of 6 buckets: A B C D E F",
  "inputs_ref": "email_list",
  "model_tier_hint": "light",
  "output_schema": { "bucket": "A|B|C|D|E|F", "reason": "string<=30" },
  "concurrency_limit": 20
}
```

- Orchestrator writes ONE node. Executor expands to N envelopes at runtime.
- One row in `subtasks`; N rows in `envelopes`; one aggregated result available
  to downstream nodes.
- Naming uses natural language (`apply_to_each`) not code (`map`) — the
  orchestrator model should reason about WBS in English, not think it is
  writing code.

### 4.3 Router Signature (revised)
**Old plan:** `select_model(tier, task_type, strategy)` — tier-primary.

**Revised:** `select_model(task_type, estimated_input_tokens, quality_requirement, strategy)` —
specialty-primary.

```python
def select_model(task_type, est_input_tokens, quality_req, strategy):
    candidates = capability_matrix.query(task_type)
    candidates = [m for m in candidates
                  if m.score_for_task(task_type) >= quality_req
                  and m.context_length >= est_input_tokens]
    candidates.sort(key=lambda m: effective_cost(m, est_input_tokens))
    return candidates[0]
```

Tier becomes a **derived** concept — "heavy" just means "high
quality_requirement." Specialty is represented by per-task-type scores,
not by naming a tier. This handles lineups like {120B generalist, 12B instruct,
4B coder, 4B instruct} correctly — a 4B coder beats a 12B generalist at code.

---

## 5. Cost Model

### 5.1 Unified Effective Cost
One formula works for local, external, and mixed setups:

```
effective_cost =
    (tokens_in  × input_rate  + tokens_out × output_rate)     # dollars
  + (tokens_total / throughput_tps) × time_cost_per_sec        # time→dollars
```

- **External:** first term dominates; rates from OpenRouter API.
- **Local:** first term is zero; second term dominates (tokens/sec is inverse cost).
- `time_cost_per_sec` is a user-configurable Admin UI knob — defaults to a
  sensible value (e.g. $30/hr worth of waiting time).

### 5.2 Data Sources
- **External pricing:** `GET https://openrouter.ai/api/v1/models` returns every
  model with `pricing.prompt`, `pricing.completion` (strings, dollars per token)
  and `context_length`. No auth needed for the list. Applies to any
  OpenAI-compat provider routing through OpenRouter.
- **Local throughput:** measured during Phase 1 benchmarks (we already call
  each model N times per task type — just time it and record `throughput_tps`).

### 5.3 Capability Matrix Schema (additions)
```yaml
- model_id: "openrouter/anthropic/claude-sonnet-4"
  source: "openrouter"
  context_length: 200000
  pricing:
    input_per_token: 0.000003
    output_per_token: 0.000015
  task_scores:
    reasoning: 9.0
    coding: 9.5
    summarize: 9.0
    agentic: 8.5
    tool_use: 9.0
  throughput_tps: 80   # measured or provider-reported

- model_id: "lmstudio/qwen3-35b"
  source: "lmstudio"
  endpoint: "http://localhost:1234"
  context_length: 32000
  pricing: null        # local = free in dollars
  task_scores: { ... }
  throughput_tps: 18   # measured during benchmark
```

---

## 6. Observability

### 6.1 Scope
Observability is for **tuning and analysis**, not audit. Signal over completeness.

### 6.2 What We Log (kept from original plan)
- Every model call as a log event (tokens, latency, cost, decision)
- Full payloads in `log_payloads` table
- spaCy NER extraction over payloads (Phase 6)

### 6.3 What We Add
- **`trigger_reason`** field on every envelope — one short string: why was this
  envelope dispatched? (`initial_plan`, `escalation_from:{id}`,
  `deferral_batch:{table}`, `re_orchestration:{reason}`, `retry:{id}`)
  → Turns the envelope table into a causal graph queryable with a single WHERE clause.

- **`orchestrator_decision`** event type — one row per orchestrator invocation:
  what state it saw (brief digest), its reasoning output (verbatim), its plan.
  → Primary artefact for manual drill-down when a run looks odd.

- **`child_residue`** event type — emitted when a parent's fold-in sweep finds
  scratch rows the child failed to clean up. Fields: `parent_level`,
  `child_level`, `child_agent_model`, `residue_row_count`, `residue_tables`.
  → Free diagnostic: accumulating residue events per model_id reveals which
  agents are misbehaving without any separate health check.

- **`run_digests`** table — computed once at run completion:
  `total_cost_usd`, `total_latency_ms`, `tokens_by_model (json)`,
  `orchestrator_invocations`, `escalation_count`, `deferral_count`,
  `retry_count`, `envelopes_by_tier (json)`, `composer_confidence`,
  `user_rating (nullable)`.
  → "Week at a glance" view without any AI in the loop.

- **`user_rating`** (optional) on `runs` — thumbs up/down from chat UI.
  → Real ground truth signal when user chooses to provide it.

### 6.4 What We Do NOT Add (previously considered, rejected)
- **The Coach** — a scheduled critic model that reads digests and suggests
  changes. Rejected: the system is self-regulating by construction
  (orchestrator bias toward Direct mode + quality-by-floor lineup). Over-policing
  a well-designed system is waste.
- Full state change-log with timestamps (too noisy, almost never useful).
- Scrubbable timeline UI (pretty, expensive, low ROI for tuning).
- Simulation/replay mode (large lift, low tuning ROI).

### 6.5 Stats Page (in place of Coach)
Small addition to the Dashboard in Phase 7:
- Weekly cost chart by model and day
- Outlier runs (top 5 by cost, top 5 by duration, runs > 2× median)
- User-flagged runs (thumbs-down)

Zero model calls. A couple of SQL queries and charts.

---

## 7. Budget Awareness (deferred)

**Decision:** Ship with **implicit** only — the orchestrator prompt says "keep
returns compact, cascade when stage outputs would overwhelm your next decision."
No numeric budget tracking in the executor initially.

Observe in Phases 3/4:
- If the orchestrator consistently overflows → add **hybrid** (executor reports
  rough % used as soft signal in the prompt).
- If it over-compresses and loses detail → no change.
- If different task types behave very differently → task-type-sensitive.

This is a cheap decision to defer and expensive to over-engineer.

---

## 8. What Changes in the Existing Plan

### 8.1 Phase 1 — Model Registry + Discovery
- Add **OpenRouter adapter** to `src/registry/discovery.py` —
  `GET /api/v1/models`, parse pricing + context_length for every listed model.
- Add **throughput measurement** to `src/registry/benchmark.py` — record
  `throughput_tps` alongside quality score.
- Revise **`src/registry/router.py`** to the new signature
  (`task_type, input_tokens, quality_req, strategy`) — specialty-primary, not
  tier-primary.
- Extend **`data/capability_matrix.yaml`** schema — `pricing`, `context_length`,
  `throughput_tps`, per-task `task_scores`.

### 8.2 Phase 2 — Envelope Contract + State + Logging
- Add `trigger_reason` to `envelopes` table.
- Add `orchestrator_decision` event type to `logs.event_type` enum.
- Add `run_digests` table (computed at run completion).
- Add `user_rating` (nullable int) to `runs` table.
- Envelope outputs gain optional `side_effects` field (declared DB writes).
- Envelope inputs gain optional `state_query` field (SQL read before dispatch).
- Scratch table lifecycle management — `level` column so executor can run
  per-level cleanup on fold-in.

### 8.3 Phase 3 — Orchestrator + Executor
- **Unify** `src/prompts/orchestrator.py` and `src/prompts/composer.py` into a
  single prompt with `current_mode` flag.
- Add `apply_to_each` to the DAG node types.
- Add state operators (`filter`, `count`, `group_by`, `sample`) as no-model
  nodes in the DAG.
- Executor implements the fold-in cleanup law: parent reads child scratch,
  digest passed as context, scratch deleted post-parent-return.
- Orchestrator prompt teaches **wrapper-as-compression-contract** — the core skill.

### 8.4 Phase 5 — Memory
- Shrinks significantly: sub-agent outputs handle current-run compression as a
  by-product. Phase 5's dedicated compressor only handles **inactive historical
  turns** (from earlier runs that are no longer hot).

### 8.5 Phase 7 — UI
- Add **Stats** tab to Dashboard (no AI critic).
- Add **thumbs up/down** buttons on chat responses → `user_rating`.
- Remove the Coach from scope (it is not on any phase).

### 8.6 No Phase 8
The Coach is cancelled. The simple Stats page covers the need at a fraction of
the complexity.

---

## 9. Failure Semantics in `apply_to_each`

**Decision (Session 2):** **Best-effort + orchestrator decides.**

If the orchestrator dispatches `apply_to_each` over N items and some fail:
- Successful results are kept in the DB
- Failures are returned as a structured `failures` list alongside the
  aggregated results: `[{index, item_ref, reason, model_id}]`
- `apply_to_each` as a whole always succeeds — failures are data, not catastrophe
- The orchestrator reads the aggregate, sees the failure list, and decides
  what to do (retry on stronger model, ignore, flag for user, etc.)

This matches progressive distillation: failures are just another kind of
output the orchestrator digests and reacts to. No batch-level all-or-nothing.

## 10. Skill Library + Continuous Improvement Loop

The orchestrator's core skill is **designing good inbound schemas**. Rather
than relying on each model's innate ability to design schemas from scratch,
the system maintains a **Skill Library** — a versioned catalogue of
known-good task templates that the orchestrator can invoke.

### What a Skill Is

A skill is a reusable template for a common orchestrator move. Each skill has:

```yaml
skill_id: "classify_into_buckets"
version: 1
description: "Assign each item to exactly one of N predefined buckets."
when_to_use:
  - "You have a list of items"
  - "There is a known, closed set of categories"
  - "Uncertainty should be expressed via a 'defer' bucket, not refusal"
recommended_tier: "light"
outbound_template:
  type: "apply_to_each"
  instruction: "Classify each item into one of: {buckets}. If uncertain, return 'defer' with a short reason."
  output_schema:
    bucket: "enum:{buckets}|defer"
    confidence: "float 0-1"
    defer_reason: "string<=50 optional"
  side_effects:
    - "deferrals"   # for items classified as 'defer'
example_invocation: |
  {
    "buckets": ["billing", "tech_support", "sales", "feedback", "other"]
  }
```

### Initial Skills to Ship (Slice 2)

Hand-crafted library of ~10 skills covering the common WBS moves:

1. **classify_into_buckets** — closed-set classification with defer escape hatch
2. **extract_fields** — structured field extraction from unstructured text
3. **summarize** — compress long content to key points
4. **compare_and_rank** — score N items against criteria
5. **filter_by_criterion** — keep only items matching a rule
6. **transform_style** — convert between formats/tones/languages
7. **decompose_task** — break a complex task into sub-tasks (spawns child orchestrate)
8. **verify_claim** — check consistency of a claim against evidence
9. **retry_escalate** — re-route failed items to a higher tier
10. **defer_and_collect** — mark uncertain items; batch for later stronger pass

These are the orchestrator's toolbox. The orchestrator's system prompt
includes the full library, and it is told: "First, check if an existing skill
matches. If yes, invoke it with the right arguments. If no, craft a custom
envelope."

### Continuous Improvement Loop

The library evolves based on usage data. Every time a skill is used, outcome
is logged:

```sql
-- skill_usage_log table
skill_id TEXT, skill_version INT, run_id UUID, envelope_id UUID,
outcome TEXT, -- 'success' | 'partial' | 'failure'
tokens_used INT, latency_ms INT,
composer_accepted BOOLEAN, -- did the composer use this output cleanly?
child_residue_count INT,   -- did the agent misbehave?
created_at TIMESTAMP
```

Aggregated queries answer:
- Which skills have high success rate? Low?
- Which skills produce residue (misbehaving agents)?
- Which skills get edited/overridden most often by the composer?
- Are certain skills failing on specific model tiers?

**Surfacing:** an Admin UI panel ("Skills Library") shows each skill with its
usage stats, failure examples, and a plain-English recommendation
("Consider tuning the output_schema — 22% of invocations produced residue").
Admin can edit skills. Edits bump the version; old stats are preserved.

**Versioning + A/B:** new skill versions can be deployed alongside old ones.
When a task matches, the library can alternate between versions to compare
outcomes. This is a light-touch A/B, no elaborate experiment framework.

This is NOT the rejected "Coach" — the Coach was about policing decisions.
The Skill Library is about providing the orchestrator with better tools, and
letting usage data guide which tools to keep sharp.

### Where It Lives

| File | Role |
|------|------|
| `data/skills.yaml` | The versioned skill catalogue |
| `src/skills/library.py` | Load + query + track skills |
| `src/prompts/orchestrator.py` | Includes the skill catalogue in its system prompt |
| `src/db/schema.py` | `skill_usage_log` table |
| `ui/skills.html` | (Slice 5+) Skill management UI |

### Initial Slice Allocation

- **Slice 2:** ship 10 hand-crafted skills as `data/skills.yaml`; orchestrator
  prompt includes them; `skill_usage_log` populated on every dispatch.
- **Slice 5 (extended):** Skill library UI — view, edit, version, see stats.
- **Post-v1:** automated tuning suggestions (a light-weight critic specifically
  for skills — NOT general decision policing), A/B experiment harness.

---

## 11. Product Surfaces — How BORG_ROUTE Meets The World

BORG_ROUTE is **API-first middleware**, not a standalone chat app. It exposes
three surfaces, in priority order:

### Primary — OpenAI-Compatible Gateway (`POST /v1/chat/completions`)

The product is invisible infrastructure. Cursor, Continue, OpenClaw, Droid,
custom scripts — all of them speak the OpenAI chat-completions protocol.
BORG_ROUTE speaks it back. The user points their existing tool at
`http://localhost:8000/v1` and specifies `model: "borg_route"` (or a variant
like `borg_route:light`, `borg_route:heavy`, `borg_route:coding`).

Internally, every request triggers the full orchestration pipeline:
- orchestrator in plan mode (Direct or Route decision)
- dispatch of whatever nodes the plan produces
- re_plan / cascade / compose as needed
- return as a standard OpenAI response (with token usage, finish_reason, etc.)

Streaming support via SSE (server-sent events) for token-level output — this
is standard in the OpenAI spec.

Model aliases the gateway accepts:
- `borg_route` — full orchestration, system decides
- `borg_route:light|mid|heavy` — tier hint biases model selection
- `borg_route:coding|creative|reasoning|extraction` — specialty hint
- `borg_route:local` — force LOCAL_FIRST strategy for this request
- `{any specific model_id from capability_matrix}` — bypass orchestration,
  route directly to that model (useful for debugging / benchmarking)

### Secondary — Batch / Task API (`POST /v1/task`)

For long-running jobs that don't fit the sync chat-completions pattern:
- Request returns `run_id` immediately (202 Accepted)
- Client polls `GET /runs/{id}` or subscribes to `GET /runs/{id}/stream` (SSE)
- Use cases: bulk classification, batch extraction, multi-stage research
- Same orchestration engine underneath; just async surface

### Tertiary — MCP Server (`/mcp` endpoint)

BORG_ROUTE exposes MCP tools for consumption by Claude Desktop, Continue, or
any MCP-compatible client. Tool set:
- `borg_route.chat` — sync, thin wrapper over `/v1/chat/completions`
- `borg_route.submit_task` — async job submission, returns run_id
- `borg_route.get_run` — fetch status + result of a run
- `borg_route.list_models` — matrix view
- `borg_route.get_stats` — recent cost/usage summary

Allows workflows like: user in Claude Desktop says "Please process this CSV
through BORG_ROUTE with these 6 buckets." Claude calls `borg_route.submit_task`
and the pipeline runs locally; result returned to the Claude conversation.

### UI As Reference Client

The browser UI (`/`, `/chat`, `/models`, `/admin`, `/dashboard`, `/run/{id}`,
`/logs`, `/skills`) is a reference implementation that calls the same public
APIs any third party would call. No privileged backdoor. Dogfooding forces the
API to be good.

---

## 12. Builder-Model Strategy (The Meta-WBS)

Building BORG_ROUTE is itself a WBS exercise. The planning agent (who wrote
this document) spent peak context designing architecture and prompts. The
builder agent can be a cheaper model for most execution work.

Per-task model recommendations live in `BUILD_PLAN.md` under each task. General
tiers:

| Work class | Recommended builder model |
|-----------|---------------------------|
| Architecturally subtle: orchestrator engine, executor, fold-in, validators, skill loader, prompt wiring | Claude Sonnet 4 (or equivalent flagship) |
| Structured CRUD: discovery, router, cost formula, DB schema, logging writer, registry endpoints | DeepSeek V3.1 or Qwen3-Coder |
| Wiring + UI: API endpoints, HTML/JS, tests skeletons, docs updates, error registry entries | Claude Haiku 4.5 or DeepSeek V3.1 or a local 27B |

The prompts (`src/prompts/base.py`, `orchestrator.py`, `answerer.py`) and
the skills library (`data/skills.yaml`) are **already written** during
Session 2 and do NOT need to be regenerated by the builder. The builder
imports and uses them as-is. This is the most important constraint — these
artefacts are high-leverage and prompt-engineering-dense; changing them
without intent would break the system.

---

## 13. Resolved Questions

All Session 2 open questions are now closed. No blocking architectural
decisions remain before Slice 1 can begin.

- Tool use for code-gen answerers — DECISION: pure envelope I/O for Slice 2
- First testable slice — RESOLVED: Slice 2 is the milestone
- apply_to_each failure semantics — RESOLVED (§9): best-effort
- Orchestrator skill teaching — RESOLVED (§10): Skill Library, 10 skills in Slice 2
- Product surfaces — RESOLVED (§11): API-first (B) + MCP (D) from day one
- Builder-model strategy — RESOLVED (§12): tiered assignment in BUILD_PLAN
- Prompts + skills — WRITTEN: live in `src/prompts/` and `data/skills.yaml`
- Backup strategy — DEFERRED GitHub; zip snapshots per HANDOVER_TO_BUILDER.md

---

## 10. Reading Order (for future sessions)
1. This file (`DESIGN_PRINCIPLES.md`) — the WHY/HOW
2. `MESSAGE_TO_CLAUDE.md` — the WHAT (to be updated after review of this doc)
3. `BUILD_PLAN.md` — the TASKS (to be updated after review of this doc)
4. `CHECKPOINT.md` — where we are now
5. `CODING_STANDARD_CORE.md` — always applies
