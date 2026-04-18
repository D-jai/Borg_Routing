# Checkpoint — BORG_ROUTE
> **Where I Left Off:** Slice 1 (Registry Foundation) is **complete**.
> All S1-00 through S1-10 tasks shipped and committed. 53 tests passing.
> Next session starts at **BUILD_PLAN.md Slice 2, task S2-01**
> (`src/envelopes/schema.py` — WrappedTask + InboundEnvelope Pydantic models).
>
> Open `src/envelopes/schema.py` (does not exist yet) and implement the
> schema per the S2-01 spec in BUILD_PLAN.md.
>
> Do NOT re-read all of DESIGN_PRINCIPLES.md — skim §2 (Two Laws) and §4
> (Execution Model) as a quick refresher. The key invariants are:
> - `status` MUST be `resolved` or `failed` (never `unresolved`) — BRT-ENV-001
> - Every model call goes through `build_system_prompt()` from `src/prompts/base.py`
> - Governance is uncompactable (additive, verbatim through every cascade)

---

## Slice Status

| Slice | Status | Notes |
|-------|--------|-------|
| Slice 0: Scaffolding | ✅ Complete | Docs, git, reassessment done |
| Slice 1: Registry Foundation | ✅ Complete | S1-00..S1-10, 53 tests, all committed |
| Slice 2: Core Pattern E2E | 🔲 Not started | **Start here** — THE architectural milestone |
| Slice 3: Recursion + Admin + Resume | 🔲 Not started | |
| Slice 4: Conversation Memory | 🔲 Not started | |
| Slice 5: Observability + NER | 🔲 Not started | |
| Slice 6: Live UI + Polish | 🔲 Not started | |

---

## Architectural Decisions Locked In (Session 2)

The following are now non-negotiable without a further reassessment session.
Full detail: `DESIGN_PRINCIPLES.md`.

- **Product thesis:** orchestrator as project manager; smart WBS;
  intelligence spent on structuring, not doing
- **Law 1:** Uncompacted Governance (additive, cascading, never diluted)
- **Law 2:** No Forgotten Scratch (child self-cleans, parent verifies, residue
  is diagnostic signal)
- **Core patterns:** Wrapper-as-Compression Contract, Progressive Distillation,
  Cascading Distillation
- **Execution:** Shared-state orchestration via four mechanisms
  (state_query, side_effects, orchestrator re-invocation, state_ops)
- **Unified brain:** orchestrator and composer are the same prompt with a
  `current_mode` flag (plan|dispatch|re_plan|compose)
- **DAG types:** `orchestrate`, `answerer`, `apply_to_each`, `state_op`
- **Cost:** unified `effective_cost = dollars + time×rate`;
  OpenRouter for external pricing; measured throughput_tps for local
- **Router:** specialty-primary `select_model(task_type, input_tokens,
  quality_req, strategy)`; tier is derived
- **Observability:** Stats page (no Coach), run_digests, trigger_reason on
  envelopes, orchestrator_decision + child_residue event types
- **Budget awareness:** start implicit; add explicit only if empirically needed
- **Builder model:** Claude Sonnet 4.6, reasoning High (Max for Heavy tasks).
  One model for the whole build — no switching. Set via `/model` or
  Shift+Tab → Settings → Model.
- **Context regime:** 65% rule. When context hits ~65% (or slice completes),
  the builder overwrites `SESSION_HANDOFF.md`, commits, zips, and tells
  user to open a new session. New session reads `AGENTS.md` +
  `SESSION_HANDOFF.md` to bootstrap. See `AGENTS.md` for template.

---

## What Works (verified)

- [x] Project directory + template docs
- [x] Git repo initialised, initial commit made
- [x] Full architecture reassessed and written to disk (Session 2)
- [x] Orchestrator + answerer + base system prompts written
      (`src/prompts/base.py`, `orchestrator.py`, `answerer.py`)
- [x] Initial 10 skills hand-crafted (`data/skills.yaml`)
- [x] Capability matrix seeded (8 local + 5 OpenRouter) in
      `data/capability_matrix.yaml`
- [x] Build plan detailed to extreme-spec for Slices 1 + 2
- [x] **Slice 1 complete (53 tests passing):**
  - `pyproject.toml` + `main.py` + `/health` endpoint
  - `src/registry/models.py` — ModelInfo + Pricing (BRT-REG-001/002)
  - `src/registry/capability_matrix.py` — YAML load/save/query/upsert (BRT-REG-010..012)
  - `src/registry/cost.py` — unified effective_cost (BRT-REG-013)
  - `src/registry/router.py` — select_model specialty-primary (BRT-REG-020)
  - `src/registry/discovery.py` — LM Studio/Ollama/OpenRouter/custom (BRT-REG-001/003/004)
  - `src/registry/benchmark.py` + `data/benchmark_prompts.yaml` (BRT-REG-011/014/015)
  - `src/api/models.py` — GET /models, POST /discover, POST /benchmark/{id}, POST /keys (BRT-API-001/002)
  - `ui/models.html`, `ui/keys.html`, `ui/chat.html`, `ui/_header.html`

## What Has Never Been Tested End-to-End

- [ ] Envelope contract (S2-01 onwards)
- [ ] Orchestrator engine + executor
- [ ] MCP server
- [ ] OpenAI-compat gateway

## Known Gaps

- [ ] GitHub deferred — using zip snapshots (HANDOVER Step 4)
- [ ] No `pyproject.toml` yet — S1-00 creates it
- [ ] Only prompts, skills, and capability matrix exist as actual artefacts
      so far. Everything else is spec.

## Open Architectural Questions (mostly resolved in Session 2)

All previously-open questions were resolved on 2026-04-18:

- **apply_to_each failure semantics** — RESOLVED: best-effort + orchestrator
  decides (DESIGN_PRINCIPLES.md §9)
- **Tool use for code-gen answerers** — RESOLVED: pure envelope I/O for
  Slice 2; tools deferred to post-Slice 6
- **Orchestrator skill teaching** — RESOLVED: Skill Library with continuous
  improvement loop, 10 initial skills in Slice 2 (DESIGN_PRINCIPLES.md §10)
- **User's model lineup** — RESOLVED: capability_matrix.yaml seeded with
  8 local (LM Studio) + 5 OpenRouter externals
- **GitHub** — DEFERRED: zip snapshots instead (HANDOVER_TO_BUILDER.md Step 4)

No blocking architectural questions remain. Builder can start Slice 1 cleanly.

## Already Fixed

- N/A

---

## Session History

| Date | Session | Work Done | Tests |
|------|---------|-----------|-------|
| 2026-04-17 | 1 | Full architecture designed (v1); all project docs written; HANDOVER_TO_BUILDER.md created | 0 |
| 2026-04-18 | 2 (reassessment) | Architecture reassessed end-to-end with Droid. Significant simplifications and additions. DESIGN_PRINCIPLES.md created. MESSAGE_TO_CLAUDE.md + BUILD_PLAN.md rewritten to reflect slice-based plan. CHECKPOINT.md + HANDOVER_TO_BUILDER.md updated. Skill Library concept added. capability_matrix.yaml seeded (8 local + 5 OpenRouter). Failure semantics + tool-use + GitHub strategy resolved. | 0 |
| 2026-04-18 | 2b (peak-context writing) | Actual orchestrator + answerer + base system prompts written into `src/prompts/`. 10 skills fully specified in `data/skills.yaml`. Product surfaces decided (B OpenAI-compat + D MCP from Slice 2). BUILD_PLAN rewritten to extreme-spec detail for Slices 1 + 2 with per-task builder-model tier. DESIGN_PRINCIPLES §11-13 added (surfaces, builder strategy, resolved questions). | 0 |
| 2026-04-18 | 3 (Builder Session 1) | Slice 1 complete. S1-00..S1-10 all shipped: FastAPI scaffold, ModelInfo, CapabilityMatrix, cost formula, specialty-primary router, discovery (LM Studio/Ollama/OpenRouter/custom), 35-prompt benchmark suite, registry API endpoints, Models + Keys + Chat UI pages, Slice 1 E2E test. All Parallel Commit Law artefacts shipped with each commit. | 53 |
