# Session Handoff — 2026-04-18T15:00 (Builder Session 1 → Builder Session 2)

## Context At Write Time
- Usage: ~65% (approaching limit)
- Model: Claude Sonnet 4.6
- Reasoning: High

## Where I Left Off

Slice 1 (Registry Foundation) is **complete and committed** (9 commits, 53 tests
passing). The next session opens `src/envelopes/schema.py` (does not exist yet)
and implements `WrappedTask` + `InboundEnvelope` Pydantic models per BUILD_PLAN.md
§S2-01. That is the exact first concrete action.

## Just Completed (this session)

- S1-00: `pyproject.toml`, `main.py`, `/health` endpoint, `tests/test_health.py`
- S1-01: Verified pre-written artefacts (`src/prompts/`, `data/skills.yaml`, `data/capability_matrix.yaml`)
- S1-02: `src/registry/models.py` — ModelInfo + Pricing (BRT-REG-001/002)
- S1-03: `src/registry/capability_matrix.py` — YAML load/save/query/upsert (BRT-REG-010..012)
- S1-04: `src/registry/cost.py` — unified effective_cost (BRT-REG-013)
- S1-05: `src/registry/router.py` — select_model specialty-primary (BRT-REG-020)
- S1-06: `src/registry/discovery.py` — LM Studio/Ollama/OpenRouter/custom (BRT-REG-001/003/004)
- S1-07: `src/registry/benchmark.py` + `data/benchmark_prompts.yaml` (BRT-REG-011/014/015)
- S1-08: `src/api/models.py` — GET /models, POST /discover, POST /benchmark/{id}, POST /keys (BRT-API-001/002); updated `main.py` to load matrix at startup
- S1-09: `ui/models.html`, `ui/keys.html`, `ui/chat.html`, `ui/_header.html`
- S1-10: `tests/e2e/test_slice_1.py` (7 E2E tests); CHECKPOINT.md + BUILD_PLAN.md updated

## In Progress
None — clean handoff.

## Next Task (specific)

**BUILD_PLAN.md Slice 2, task S2-01.**

First concrete actions:
1. Read AGENTS.md and this file (you're doing it).
2. Skim DESIGN_PRINCIPLES.md §2 (Two Laws) and §4 (Execution Model) — already read once; just refresh.
3. Read BUILD_PLAN.md §Slice 2, tasks S2-01 and S2-02 only (envelopes).
4. Create `src/envelopes/__init__.py` (empty) and `src/envelopes/schema.py`.
5. Implement `SideEffectAction`, `WrappedTask`, `InboundEnvelope` per S2-01 spec.
   Key invariant: `status: Literal["resolved", "failed"]` — NO "unresolved" — BRT-ENV-001.
6. Write `tests/envelopes/test_schema.py` — roundtrip, status=unresolved rejected, required fields.
7. Commit with Parallel Commit Law (docs/errors, docs/user-guide, docs/presentation, docs/tests/test-plan).
8. Proceed to S2-02 (`src/envelopes/validator.py`).

## Files To Read In New Session (minimal set)

1. `AGENTS.md`
2. This `SESSION_HANDOFF.md`
3. `DESIGN_PRINCIPLES.md` §2 (Two Laws) + §4 (Execution Model) — skim only
4. `BUILD_PLAN.md` §Slice 2, tasks S2-01 through S2-04 first (envelopes + DB schema)
5. `src/registry/models.py` — ModelInfo is already used; understand its structure
6. `src/prompts/base.py` — build_system_prompt() signature (used in S2-06)

Do NOT re-read all of DESIGN_PRINCIPLES.md or MESSAGE_TO_CLAUDE.md.

## Open Issues / Gotchas Discovered This Session

- Python 3.14.3 is installed (not 3.11). All packages installed fine; `>=3.11` constraint is met.
- `.python-version` says 3.11 but Python 3.14 runs — no issue in practice.
- `capability_matrix.yaml` is a flat YAML list (no top-level `models:` key) — already handled correctly in CapabilityMatrix.load().
- The router's cost-sort may select Claude Sonnet 4 (external) over qwen3-coder for "coding" at default token counts, because Claude has higher throughput (80 tps vs 60 tps). This is correct per the spec (cheapest by effective_cost). Tests verify the invariant, not a specific model name.
- PowerShell `&&` syntax not supported — use `;` as separator or `Set-Location` first.
- `git push` not configured (no remote); using zip backups per HANDOVER_TO_BUILDER.md.

## Tests Status
- Total: 53 passing
- Failing: none
- Test files: `tests/test_health.py`, `tests/registry/test_models.py`,
  `tests/registry/test_capability_matrix.py`, `tests/registry/test_cost.py`,
  `tests/registry/test_router.py`, `tests/registry/test_discovery.py`,
  `tests/registry/test_benchmark.py`, `tests/api/test_models_api.py`,
  `tests/e2e/test_slice_1.py`

## Instruction To Future Builder

Read the files listed above in order. Begin at S2-01. Every `feat:` commit must
ship all Parallel Commit Law artefacts (code + error codes registered + user-guide
section + presentation bullet + tests). See CODING_STANDARD_CORE.md for the checklist.

**When YOUR context reaches ~65%, or you complete a slice:**
1. Run full test suite — confirm all passing.
2. Commit remaining changes.
3. Create zip backup: `Set-Location C:\stage; Compress-Archive -Path borg_route -DestinationPath "borg_route_slice2_complete_$(Get-Date -Format 'yyyyMMdd_HHmmss').zip"`
4. Update CHECKPOINT.md (one precise paragraph about where you stopped).
5. OVERWRITE this file (`SESSION_HANDOFF.md`) using the template in AGENTS.md.
6. Tell the user: "Context at ~65%. Start a new Droid session and type: `Read AGENTS.md and SESSION_HANDOFF.md, then continue.`"
7. Stop and wait.
