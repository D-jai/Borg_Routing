# Session Handoff — 2026-04-18T17:00 (Builder Session 2 → Builder Session 3)

## Context At Write Time
- Usage: ~65%
- Model: Claude Sonnet 4.6
- Reasoning: High

## Where I Left Off

Slice 2 (Core Pattern E2E — THE architectural milestone) is **complete and committed**
(17 tasks, 138 new tests, 191 total passing). The next session opens
`src/orchestrator/engine.py` and extends it to handle `type=orchestrate` nodes:
spawn a child run with linked `parent_run_id`, enforce `MAX_RECURSION_DEPTH=5`,
and demote at the cap (BRT-ORC-020..029). That is the exact first concrete action.

## Just Completed (this session)

- S2-01: `src/envelopes/schema.py` — WrappedTask + InboundEnvelope (BRT-ENV-001..005)
- S2-02: `src/envelopes/validator.py` — validate_outbound + validate_inbound (BRT-ENV-010..012)
- S2-03: `src/db/schema.py` — 8 tables + create_all() (BRT-DB-001..003)
- S2-04: `src/db/state.py` + `src/db/scratch.py` — async DB helpers (BRT-DB-010..017)
- S2-05: `src/logging/writer.py` — fire-and-forget event logger (BRT-LOG-001..002)
- S2-06: `src/agents/executor.py` — stateless answerer dispatch (BRT-ANS-001..008)
- S2-07: `src/orchestrator/state_ops.py` — filter/count/group_by/sample (BRT-SOP-001..005)
- S2-08: `src/skills/library.py` — skill loader + render + log_usage (BRT-SKL-001..007)
- S2-09: `src/orchestrator/apply_to_each.py` — runtime expansion, best-effort (BRT-APE-001..005)
- S2-10: `src/orchestrator/fold_in.py` — Law 2 enforcement, child_residue logging (BRT-ORC-010..012)
- S2-11: `src/orchestrator/engine.py` — plan/dispatch/re_plan/compose loop (BRT-ORC-001..009)
- S2-12: `src/pipeline/runner.py` — run_pipeline top-level entry (BRT-PIP-001..005)
- S2-13: `src/api/chat_completions.py` — POST /v1/chat/completions (BRT-API-010..012)
- S2-14: `src/api/tasks.py` — POST /v1/task + GET /runs/{id} (BRT-API-020..023)
- S2-15: `src/mcp/server.py` — MCP JSON-RPC 2.0 at POST /mcp (BRT-API-030..034)
- S2-16: `ui/chat.html` + `ui/run.html` — reference chat UI + run trace view
- S2-17: `tests/e2e/test_slice_2.py` + `tests/fixtures/issues.json` — 8 E2E tests

## In Progress
None — clean handoff.

## Next Task (specific)

**BUILD_PLAN.md Slice 3, task S3-01.**

First concrete actions:
1. Read AGENTS.md and this file (you're doing it).
2. Skim DESIGN_PRINCIPLES.md §2 (Two Laws) and §4 (Execution Model) — quick refresh.
3. Read BUILD_PLAN.md §Slice 3, tasks S3-01 through S3-06 (recursion + resume + admin).
4. Open `src/orchestrator/engine.py` — extend `_dispatch_one` to handle
   `type=orchestrate` by calling `orchestrate_run(run_id=child_run_id, ...)` recursively
   with `current_depth + 1`. Enforce MAX_RECURSION_DEPTH: if depth >= max, demote to answerer.
5. Add `src/db/state.py::resume_run()` — re-dispatch failed/in-flight envelopes only.
6. Build `POST /runs/{id}/resume` endpoint in `src/api/tasks.py`.
7. Build `src/api/admin.py` — GET/POST /config, POST /config/rules.
8. Build `ui/admin.html`.

## Files To Read In New Session (minimal set)

1. `AGENTS.md`
2. This `SESSION_HANDOFF.md`
3. `DESIGN_PRINCIPLES.md` §2 + §4 (skim only)
4. `BUILD_PLAN.md` §Slice 3 (S3-01 through S3-11 only)
5. `src/orchestrator/engine.py` — understand `_dispatch_one` and `orchestrate_run`
6. `src/db/state.py` — understand existing helpers before adding `resume_run()`
7. `src/api/tasks.py` — where to add the resume endpoint

Do NOT re-read all of DESIGN_PRINCIPLES.md, CHECKPOINT.md history, or all of BUILD_PLAN.md.

## Open Issues / Gotchas Discovered This Session

- `ANSWERER_SYSTEM_PROMPT.format()` fails because the prompt contains JSON `{...}` examples.
  Fix: use safe string replacement (replace `{placeholder}` by literal key, not `.format()`).
  The same fix applies to `ORCHESTRATOR_SYSTEM_PROMPT` in engine.py — already done.
- Python 3.14.3 is running (not 3.11); `>=3.11` constraint is met, no issue.
- `httpx.AsyncClient(app=app)` is NOT supported in this httpx version — use `TestClient` (sync)
  for all FastAPI integration tests.
- PowerShell: use `;` not `&&` to chain commands. `tail` doesn't exist — use `Select-Object -Last N`.
- `git push` has no remote configured; use zip backups per HANDOVER_TO_BUILDER.md.
- The `orchestrate_run()` in engine.py currently ignores `type=orchestrate` plan nodes
  (they fall through to the default answerer path). S3-01 must fix this.
- `_db_initialised` flag in chat_completions.py uses the module-level DB_PATH — in tests this
  means the production DB may get touched if tests don't set `db_path` explicitly.
  Consider passing `db_path` through the API layer in Slice 3.

## Tests Status
- Total: 191 passing
- Failing: none
- New in Slice 2: 138 tests
- Test files: tests/envelopes/, tests/db/, tests/logging/, tests/agents/,
  tests/orchestrator/, tests/skills/, tests/pipeline/, tests/api/, tests/mcp/,
  tests/e2e/test_slice_2.py

## Instruction To Future Builder

Read files above in order. Begin at S3-01. Every `feat:` commit must ship all
Parallel Commit Law artefacts (code + error codes registered + user-guide section
+ presentation bullet + tests). See CODING_STANDARD_CORE.md for the checklist.

**When YOUR context reaches ~65%, or you complete a slice:**
1. Run full test suite — confirm all passing.
2. Commit remaining changes.
3. Create zip backup:
   `Set-Location C:\stage; Compress-Archive -Path borg_route -DestinationPath "borg_route_slice3_complete_$(Get-Date -Format 'yyyyMMdd_HHmmss').zip"`
4. Update CHECKPOINT.md (one precise paragraph about where you stopped).
5. OVERWRITE this file (`SESSION_HANDOFF.md`) using the template in AGENTS.md.
6. Tell the user: "Context at ~65%. Start a new Droid session and type:
   `Read AGENTS.md and SESSION_HANDOFF.md, then continue.`"
7. Stop and wait.
