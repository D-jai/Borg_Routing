# Checkpoint — BORG_ROUTE
> **Where I Left Off:** Slice 6 (Live UI + Streaming Polish) is **complete**.
> All S6-01 through S6-06 tasks shipped and committed. 245 tests passing (9 new).
> All planned slices (0-6) are now complete. No further slices defined in BUILD_PLAN.md.
> System is feature-complete per the current BUILD_PLAN.

---

## Slice Status

| Slice | Status | Notes |
|-------|--------|-------|
| Slice 0: Scaffolding | ✅ Complete | Docs, git, reassessment done |
| Slice 1: Registry Foundation | ✅ Complete | S1-00..S1-10, 53 tests, all committed |
| Slice 2: Core Pattern E2E | ✅ Complete | S2-01..S2-17, 191 tests (138 new in Slice 2) |
| Slice 3: Recursion + Admin + Resume | ✅ Complete | S3-01..S3-11, 210 tests |
| Slice 4: Historical Memory | ✅ Complete | S4-01..S4-04, 9 new tests, 219 total |
| Slice 5: Observability + NER + Skills UI | ✅ Complete | S5-01..S5-10, 17 new tests, 236 total |
| Slice 6: Live UI + Polish | ✅ Complete | S6-01..S6-06, 9 new tests, 245 total |

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
| 2026-04-18 | 4 (Builder Session 2) | Slice 2 complete. S2-01..S2-17: envelope schema+validator, 8 DB tables, async state/scratch helpers, fire-and-forget log writer, answerer executor, state_ops, skill library, apply_to_each, fold_in, orchestrator engine (plan/re_plan/compose loop), pipeline runner, chat_completions gateway, task API, MCP server, chat+run UI, E2E test. 138 new tests. | 191 |
| 2026-04-18 | 5 (Builder Session 3) | Slice 3 complete. S3-01..S3-11: recursive orchestration (child run + parent_run_id linkage, depth cap demotion), run_pipeline parent/depth params, resume_run(), admin API (config + rules ledger), POST /runs/{id}/resume + GET /runs, threads+messages tables + window extractor, admin.html, dashboard.html, run.html drill-down, 19 new tests. | 210 |
| 2026-04-18 | 6 (Builder Session 4) | Slice 4 complete. S4-01..S4-04: compress_inactive_turns() in src/memory/compressor.py (model call, config store, model_call log event, BRT-MEM-003..005), runner.py auto-loads rolling_summary from config when thread_id provided, 9 new tests. | 219 |
| 2026-04-18 | 7 (Builder Session 5) | Slice 5 complete. S5-01..S5-10: run_digests + log_entities + log_relations tables, compute_and_save_digest + set_run_rating helpers, POST /runs/{id}/rating, thumbs in chat UI, NER worker (spaCy), GET /logs/* API, ui/logs.html, dashboard Stats tab, GET+POST /skills/* API, ui/skills.html, 17 new tests. | 236 |
