# Build Plan — BORG_ROUTE (ARAR)

> Each phase produces a working, tested, committed increment.
> Read only the phase you are currently working on.
> Check off tasks as you complete them. Update CHECKPOINT.md after every session.

---

## Phase 0: Scaffolding ✅ Complete
Directory, template docs, git repo, architecture planning — done.

---

## Phase 1: Model Registry + Discovery
**Goal:** System can discover, validate, and score available models. UI triggers discovery.
**Verify:** `POST /models/discover` returns local models. API key form saves to `.env`.

### Files
```
pyproject.toml                   — dependencies: fastapi, uvicorn, httpx, aiosqlite,
                                   python-dotenv, pydantic, spacy, uv
main.py                          — FastAPI app, static UI mount, startup event, health GET /
src/registry/discovery.py        — ping LM Studio ports 1234–1237 (OpenAI-compat /models),
                                   query Ollama GET /api/tags,
                                   test external API keys (GET /models on each),
                                   return List[ModelInfo] — BRT-REG-001..010
src/registry/capability_matrix.py — load/save data/capability_matrix.yaml,
                                   query_by_tier(tier, task_type) → List[ModelInfo],
                                   update_score(model_id, task_type, score)
src/registry/benchmark.py        — 5 fixed prompts per task_type (reasoning, coding, summarize,
                                   agentic, tool_use); call model, score 0–10 via rubric;
                                   update capability_matrix — BRT-REG-011..020
src/registry/router.py           — build_routing_table(strategy) → Dict[tier, ModelInfo];
                                   select_model(tier, task_type, strategy) → ModelInfo
src/api/models.py                — GET /models, POST /models/discover,
                                   POST /models/benchmark/{model_id}, GET /models/matrix,
                                   POST /keys (write up to 5 external providers to .env)
data/capability_matrix.yaml      — seed: Qwen3-35B, Gemma-4-26B, GPT-4o, Claude-Sonnet-4,
                                   Llama-3-70B — each with tier + task scores + endpoint hint
ui/models.html                   — model list table, Discover button (POST /models/discover),
                                   capability matrix view, per-model Benchmark button
ui/keys.html                     — form: name, base_url, api_key × 5 → POST /keys
```

### Tasks
- [ ] P1-01: `pyproject.toml` + `main.py` — FastAPI skeleton, static mount, `GET /`
- [ ] P1-02: `data/capability_matrix.yaml` — seed file with known models + scores
- [ ] P1-03: `src/registry/discovery.py` — LM Studio + Ollama + external key discovery
- [ ] P1-04: `src/registry/capability_matrix.py` — load/save/query YAML
- [ ] P1-05: `src/registry/benchmark.py` — 5-prompt test suite per task type
- [ ] P1-06: `src/registry/router.py` — routing table + `select_model()`
- [ ] P1-07: `src/api/models.py` — all model endpoints + `/keys`
- [ ] P1-08: `ui/models.html` — model list, discover, benchmark UI
- [ ] P1-09: `ui/keys.html` — API keys form

---

## Phase 2: Envelope Contract + State Schema + Logging Foundation
**Goal:** Wrapped Task schema defined and validated. Full SQLite schema live.
Admin UI with Global Rules. No pipeline logic yet.
**Verify:** DB initialises cleanly. `POST /config/global-rules` persists. Pydantic models validate.

### Files
```
src/envelopes/schema.py          — WrappedTask (outbound Pydantic model):
                                   envelope_id, run_id, parent_run_id, subtask_id,
                                   type (enum: answerer|orchestrate|composer),
                                   current_depth, max_depth, model_endpoint, model_name,
                                   agent_tier, instruction, context, dependency_input,
                                   output_schema, max_output_tokens;
                                   InboundEnvelope (inbound): envelope_id, subtask_id,
                                   status (enum: resolved|failed|unresolved), confidence,
                                   output, tokens_used
src/envelopes/validator.py       — validate_outbound(task: WrappedTask) → raises BRT-ENV-001..005;
                                   validate_inbound(env: InboundEnvelope, task_type) → raises
                                   BRT-ENV-010 if answerer returns unresolved
src/prompts/base.py              — build_system_prompt(stage_prompt, global_rules) → str;
                                   load_global_rules() → reads from config table;
                                   ALL model calls must use this — no exceptions
src/db/schema.py                 — create_tables(): threads, messages, runs, config,
                                   envelopes, subtasks, evidence,
                                   logs, log_payloads, log_entities, log_relations
src/db/state.py                  — async: create_thread(), create_run(), update_run_status(),
                                   save_envelope(), update_envelope_status(),
                                   append_evidence(), resume_run(),
                                   get_config(key), set_config(key, value),
                                   write_log(event), write_log_payload(log_id, direction, text)
src/logging/writer.py            — write_event(event_type, run_id, envelope_id, model,
                                   tokens_prompt, tokens_completion, cost_usd,
                                   latency_ms, decision_json, text_in, text_out):
                                   writes logs row + log_payloads rows (fire-and-forget asyncio task)
src/api/admin.py                 — GET /config, POST /config,
                                   GET /config/global-rules, POST /config/global-rules
ui/admin.html                    — Global Rules textarea + save; depth slider 1–5;
                                   routing strategy toggle; context window size input
```

### Tasks
- [ ] P2-01: `src/envelopes/schema.py` — WrappedTask + InboundEnvelope Pydantic models
- [ ] P2-02: `src/envelopes/validator.py` — outbound + inbound validation, answerer contract
- [ ] P2-03: `src/prompts/base.py` — `build_system_prompt()`, `load_global_rules()`
- [ ] P2-04: `src/db/schema.py` — all tables including full log schema
- [ ] P2-05: `src/db/state.py` — all async DB helpers
- [ ] P2-06: `src/logging/writer.py` — fire-and-forget log writer
- [ ] P2-07: `src/api/admin.py` — config endpoints
- [ ] P2-08: `ui/admin.html` — Global Rules + settings panel

---

## Phase 3: Orchestrator + Executor (non-recursive)
**Goal:** Full pipeline runs end-to-end. Orchestrator builds DAG, dispatches wrapped tasks,
Composer assembles final response. All calls logged. Global Rules applied everywhere.
**Verify:** Simple task → Direct mode, 1 call. Complex task → DAG built, answerers run,
Composer produces response. Every model call in `logs` table with tokens.

### Files
```
src/prompts/orchestrator.py      — versioned system prompt: decision heuristics
                                   (Direct/Router/Distiller), DAG JSON output schema,
                                   context distillation rules, type assignment rules
src/prompts/answerer.py          — versioned system prompt: you receive a WrappedTask,
                                   execute the instruction using only the provided context,
                                   return InboundEnvelope JSON, must resolve or fail
src/prompts/composer.py          — versioned system prompt: you receive an evidence bundle
                                   and conversation window, produce a final coherent response
                                   that continues the thread naturally
src/orchestrator/engine.py       — orchestrate(thread_id, message_id, config) → run_id:
                                   load thread, call orchestrator model via build_system_prompt(),
                                   parse DAG from response, assign model per task via router,
                                   persist subtasks, log decision event
src/orchestrator/dag.py          — DAGExecutor: build from subtask list + depends_on edges;
                                   state machine WAITING→READY→IN_FLIGHT→DONE|FAILED;
                                   get_ready_tasks() → tasks with all deps DONE;
                                   mark_done(subtask_id, output)
src/agents/executor.py           — execute(task: WrappedTask) → InboundEnvelope:
                                   build_system_prompt(stage_prompt, global_rules),
                                   call model via httpx, parse response,
                                   validate_inbound() (enforce answerer contract),
                                   log model_call_start + model_call_end with tokens + latency,
                                   return envelope
src/pipeline/runner.py           — run_pipeline(thread_id, message_id):
                                   log run_started; orchestrate() → DAG;
                                   execute DAG (asyncio.gather for parallel branches,
                                   sequential for chained); compose(); log run_completed
src/api/tasks.py                 — POST /v1/task → {run_id}, GET /runs/{id},
                                   GET /threads/{id}/messages
ui/chat.html                     — thread view, message input, response display
ui/run.html                      — subtask list, type badge, depth, status, tokens per call
```

### Tasks
- [ ] P3-01: `src/prompts/orchestrator.py` — orchestrator system prompt
- [ ] P3-02: `src/prompts/answerer.py` — answerer system prompt
- [ ] P3-03: `src/prompts/composer.py` — composer system prompt
- [ ] P3-04: `src/orchestrator/dag.py` — DAGExecutor state machine
- [ ] P3-05: `src/orchestrator/engine.py` — orchestration logic + decision logging
- [ ] P3-06: `src/agents/executor.py` — stateless model caller + contract enforcer
- [ ] P3-07: `src/pipeline/runner.py` — end-to-end pipeline orchestration
- [ ] P3-08: `src/api/tasks.py` — task + run endpoints
- [ ] P3-09: `ui/chat.html` + `ui/run.html` — chat input + run detail

---

## Phase 4: Recursive Orchestration + Fault Tolerance
**Goal:** `type=orchestrate` tasks spawn child runs. Depth guard enforced.
Failed runs resume from last good envelope. Admin UI depth control wired.
**Verify:** Nested task spawns child run at depth+1. depth=max → demoted to answerer.
Kill mid-run → resume → continues correctly.

### Tasks
- [ ] P4-01: `engine.py` — handle `type=orchestrate`: spawn child run, link `parent_run_id`,
             enforce `MAX_RECURSION_DEPTH=5`, demote when depth >= max_depth
- [ ] P4-02: `runner.py` — child runner: fold child composer output into parent `dependency_input`
- [ ] P4-03: `state.py` — `resume_run()`: reload DAG, identify incomplete subtasks,
             re-dispatch failed/in-flight envelopes only
- [ ] P4-04: `src/api/tasks.py` — `POST /runs/{id}/resume`
- [ ] P4-05: `ui/admin.html` — wire depth slider to `POST /config`
- [ ] P4-06: `ui/dashboard.html` — run history table, Resume button, depth tree indicator
- [ ] P4-07: `ui/run.html` — child run drill-down (click orchestrate task → child DAG)

---

## Phase 5: Conversation Memory + Stream Continuity
**Goal:** Long threads handled without context explosion.
Composer produces responses that fit the conversation stream.
**Verify:** 15-message thread → Composer receives window + summary, not full history.
Response reads naturally as continuation of conversation.

### Tasks
- [ ] P5-01: `src/memory/window.py` — extract last N turns (N from config `window_size`)
- [ ] P5-02: `src/memory/compressor.py` — compress overflow turns into one paragraph via
             model call (logged with tokens); store compressed summary in `threads` table
- [ ] P5-03: `src/prompts/composer.py` — update: add window + rolling summary to input contract

---

## Phase 6: spaCy NER Background Worker
**Goal:** Entity + relationship extraction from all log payloads. Zero pipeline latency.
**Verify:** After a run, `log_entities` + `log_relations` populated.
`GET /logs/export` returns analyst-ready JSONL.

### Tasks
- [ ] P6-01: `src/logging/ner_worker.py` — asyncio background task started at app startup;
             polls `log_payloads` for `ner_processed=false`; loads spacy `en_core_web_sm`;
             extracts entities → `log_entities`; dep-parse relationships → `log_relations`;
             marks `ner_processed=true`
- [ ] P6-02: `src/api/logs.py` — `GET /logs`, `GET /logs/entities`,
             `GET /logs/export` (JSONL), `GET /logs/cost-summary`
- [ ] P6-03: `ui/logs.html` — event table, token cost chart, entity browser, export button

---

## Phase 7: Live UI + Full Observability
**Goal:** Real-time pipeline visibility via SSE. Cost dashboard. Analyst export polished.
**Verify:** Watching Run Detail during a live run — stages update in real time without refresh.

### Tasks
- [ ] P7-01: `src/api/tasks.py` — `GET /runs/{id}/stream` (SSE: emit envelope status changes)
- [ ] P7-02: `ui/run.html` — replace polling with SSE listener; live type badge + status updates
- [ ] P7-03: `ui/dashboard.html` — cost summary cards, token leaderboard per model,
             run depth tree

---

## Error Code Reference
| Module | Prefix | Scope |
|--------|--------|-------|
| Registry | BRT-REG | Discovery, matrix, benchmark |
| Envelope | BRT-ENV | Schema validation, answerer contract |
| Orchestrator | BRT-ORC | DAG, distillation, recursion |
| Answerer | BRT-ANS | Leaf execution, contract enforcement |
| Composer | BRT-CMP | Synthesis, evidence assimilation |
| Database | BRT-DB | SQLite operations |
| Logging | BRT-LOG | Log writer, NER worker |
| API | BRT-API | FastAPI endpoints |
| Memory | BRT-MEM | Window, compressor |
| Admin | BRT-ADM | Config, Global Rules |
