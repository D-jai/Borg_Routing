# Session Changes Manifest — 2026-04-21 → 2026-04-22

> Flat inventory of every file added, modified, or deleted across the two-day
> session that shipped slices 7-01, 7-02, and 8-01. Use this together with a
> pre-session backup to reconstruct the original state exactly.
>
> **Source of truth:** the per-slice ledgers under `.dev/ledger/` — this file
> is a roll-up view, not a substitute. Links to each ledger are at the bottom.

---

## Files DELETED (cannot be recovered without your backup)

These need to be restored from a pre-session zip if you want to roll back:

| Path | Original purpose | Slice |
|---|---|---|
| `src/registry/benchmark.py` | Model benchmark module (judge-model quality + throughput) | 8-01 |
| `tests/registry/test_benchmark.py` | Tests for benchmark module (6 test cases) | 8-01 |
| `data/benchmark_prompts.yaml` | 5 prompts per task_type used by benchmark | 8-01 |

---

## Files ADDED (new — delete to roll back)

| Path | Purpose | Slice |
|---|---|---|
| `src/registry/scorer.py` | LLM web-search model scorer (replaces benchmark) | 8-01 |
| `tests/registry/test_scorer.py` | 5 scorer tests | 8-01 |
| `.dev/ledger/2026-04-21_s7-model-enable-toggle.yaml` | Slice 7-01 ledger | 7-01 |
| `.dev/ledger/2026-04-21_s7-02-discovery-hygiene.yaml` | Slice 7-02 ledger | 7-02 |
| `.dev/ledger/2026-04-22_s8-retire-benchmark-scorer-and-slots.yaml` | Slice 8-01 ledger | 8-01 |
| `CHANGES_MANIFEST.md` | This file | 8-01 |

---

## Files MODIFIED (per-file summary of every change)

| Path | What changed | Slices |
|---|---|---|
| `src/registry/models.py` | Added three fields to `ModelInfo`: `enabled: bool = True`, `created_at: Optional[int] = None`, `scored_at: Optional[int] = None` | 7-01, 7-02, 8-01 |
| `src/registry/capability_matrix.py` | `query_by_task` now filters out `enabled=False`; added `set_enabled()` method | 7-01 |
| `src/registry/router.py` | `BRT-REG-020` error message mentions disabled-models as a possible cause | 7-01 |
| `src/registry/discovery.py` | LM Studio prefers `/api/v0/models` with `state=="loaded"` filter (fallback `/v1/models`); OpenRouter records `created_at` and auto-disables models older than `OPENROUTER_MAX_AGE_DAYS` (default 360); `discover_all` preserves user's `enabled` override on re-discovery when `created_at` is already known | 7-02 |
| `src/api/models.py` | Full rewrite. Removed `POST /models/benchmark/{id}` endpoint. Added `PATCH /models/matrix/{model_id}`, `POST /models/info`, `GET /models/slot_count`, `PUT /models/slot_count`. Added dual-name OpenRouter key resolver (`OPENROUTER_API_KEY` OR `KEY_OPENROUTER`). | 7-01, 8-01 |
| `src/orchestrator/engine.py` | `_select_orchestrator_model` retries at `quality_requirement=0.0` if 8.0 finds nothing; logs `BRT-ORC-007-DEGRADED`. Added `import logging` + module-level logger. | 8-01 |
| `ui/models.html` | Full rewrite (twice). First rewrite added Enabled checkbox column (7-01). Second rewrite replaced the flat table with two sections: LM Studio auto-rows + OpenRouter configurable slot dropdowns; added "Get Models Info" button; removed Benchmark buttons. | 7-01, 8-01 |
| `tests/registry/test_capability_matrix.py` | +2 tests (TC-REG-017 disabled-filter, TC-REG-018 set_enabled unknown id) | 7-01 |
| `tests/registry/test_router.py` | +2 tests (TC-REG-036 router skips disabled, TC-REG-036b BRT-REG-020 when all disabled) | 7-01 |
| `tests/registry/test_discovery.py` | +4 tests (TC-REG-040 rewrite loaded-only, TC-REG-040b fallback, TC-REG-047 age filter, TC-REG-048 preserve toggle) | 7-02 |
| `tests/api/test_models_api.py` | Removed `test_benchmark_unknown_model_returns_404` (TC-API-013); added TC-API-016 PATCH tests (7-01); added TC-API-017/018/019 scorer + slot_count tests (8-01); rewrote TC-API-016a to not depend on disk state | 7-01, 8-01 |
| `tests/orchestrator/test_engine.py` | +1 test (TC-ORC-040 auto-lower orchestrator bar) | 8-01 |
| `.env.example` | Added `OPENROUTER_MAX_AGE_DAYS=360` and commented `SCORING_MODEL` option | 7-02, 8-01 |
| `docs/user-guide.md` | New sections: "Enabling and Disabling Models", "Getting Model Info" (replaces Benchmark reference). New troubleshooting bullets. | 7-01, 7-02, 8-01 |
| `docs/presentation-content.md` | Added two new bullets: "Human-in-the-Loop Model Curation" (7-01), "Operator-First Models Page" (8-01) | 7-01, 8-01 |
| `docs/errors/error-registry.md` | Added BRT-REG-030/031/032. Retired BRT-REG-014/015. Updated origins for BRT-REG-013 (message) and BRT-REG-020 (mention disabled). Updated BRT-API-002 origin (set_model_enabled now uses it). | 7-01, 8-01 |
| `docs/CHANGELOG.md` | Regenerated from the 9 ledger files via `.dev/render_changelog.py` — now includes slices 7 and 8 | rendered |
| `docs/user-guide.generated.md`, `docs/presentation.generated.md` | Regenerated from the 9 ledger files via the sibling render scripts | rendered |

---

## Files changed by the test suite (not by me directly)

During normal test runs, `matrix.save()` is called by `POST /models/discover`
(the happy-path endpoint test) and this writes the current in-memory matrix
state to disk. The following file is regularly rewritten:

- `data/capability_matrix.yaml` — the seed matrix. Row count on disk is
  currently 208 (pre-session discovery pollution from before this session) and
  every row now carries the new `enabled`, `created_at`, and `scored_at` keys
  because the ModelInfo schema changed. If you restore from backup, the
  schema mismatch is fine — Pydantic defaults fill in missing fields.

---

## Environment / runtime side-effects

| Thing | Where it lives | How to roll back |
|---|---|---|
| `.venv/` virtualenv | `.venv/` (project root) | `rm -rf .venv` |
| `en_core_web_sm` spaCy model | Inside `.venv/` | Dies with the venv |
| `data/borg_route.db` | SQLite runtime state | Stop server; `rm data/borg_route.db` (schema recreates on next boot) |
| `/tmp/borg_route_uvicorn.log` | Dev server log | Delete if present |

None of these leak outside the project tree.

---

## How to roll back to the pre-session state

The project is **not** under git (confirmed at session start). Options in
order of cleanliness:

1. **If you have a pre-session zip backup** (per the "zip backup procedure in
   HANDOVER_TO_BUILDER.md" mentioned in `docs/install-guide.md`): extract it
   over the project tree, then `rm -rf .venv data/borg_route.db`. Done.

2. **If you don't have a backup:** the ledger YAMLs describe what changed but
   do not contain reverse diffs. Deleted files (`src/registry/benchmark.py`,
   `tests/registry/test_benchmark.py`, `data/benchmark_prompts.yaml`) are
   irrecoverable without a backup.

3. **Cherry-pick rollback:** for any single slice, open its ledger under
   `.dev/ledger/`, read the `files_touched` / `files_deleted` lists, and
   reverse each change manually using the `design_notes` and
   `user_guide`/`presentation` blocks as guidance. Tedious but possible.

---

## Links to per-slice detail

- Slice 7-01 — enable/disable toggle: [.dev/ledger/2026-04-21_s7-model-enable-toggle.yaml](.dev/ledger/2026-04-21_s7-model-enable-toggle.yaml)
- Slice 7-02 — discovery hygiene: [.dev/ledger/2026-04-21_s7-02-discovery-hygiene.yaml](.dev/ledger/2026-04-21_s7-02-discovery-hygiene.yaml)
- Slice 8-01 — retire benchmark + scorer + Models UI: [.dev/ledger/2026-04-22_s8-retire-benchmark-scorer-and-slots.yaml](.dev/ledger/2026-04-22_s8-retire-benchmark-scorer-and-slots.yaml)
- Rolled-up human-readable view: [docs/CHANGELOG.md](docs/CHANGELOG.md)
- Slice 8 implementation plan (rationale + decisions): `~/.claude/plans/was-task-score-was-spicy-falcon.md`
