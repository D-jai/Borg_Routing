# Test Plan — BORG_ROUTE
> Naming: `TC-[MODULE]-[NNN] — Plain English description`
> Three levels: Unit (mocked) → Integration (external mocked) → E2E (full system)

---

## S2-01 — Envelope Schema

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-ENV-001 | WrappedTask roundtrip serialisation with all fields | Unit | ✅ Pass |
| TC-ENV-002 | InboundEnvelope (resolved) roundtrip serialisation | Unit | ✅ Pass |
| TC-ENV-003 | BRT-ENV-001: status='unresolved' rejected | Unit | ✅ Pass |
| TC-ENV-004 | BRT-ENV-002: invalid envelope type rejected | Unit | ✅ Pass |
| TC-ENV-005 | BRT-ENV-003: current_depth > max_depth rejected | Unit | ✅ Pass |
| TC-ENV-006 | BRT-ENV-004: empty instruction rejected | Unit | ✅ Pass |
| TC-ENV-007 | BRT-ENV-005: invalid SideEffectAction action rejected | Unit | ✅ Pass |
| TC-ENV-008 | WrappedTask default fields are correct | Unit | ✅ Pass |
| TC-ENV-009 | InboundEnvelope (failed) roundtrip serialisation | Unit | ✅ Pass |
| TC-ENV-010 | SideEffectAction accepts write and delete | Unit | ✅ Pass |
| TC-ENV-011 | All four valid envelope types accepted | Unit | ✅ Pass |
| TC-ENV-012 | InboundEnvelope with scratch_writes and scratch_cleanup | Unit | ✅ Pass |

## S2-12 — Pipeline Runner

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-PIP-001 | run_pipeline returns completed dict with run_id + response | Unit (mocked) | ✅ Pass |
| TC-PIP-002 | BRT-PIP-004 for empty instruction | Unit | ✅ Pass |
| TC-PIP-003 | run status=completed in DB after success | Unit (mocked) | ✅ Pass |
| TC-PIP-004 | BRT-PIP-002 in error field when orchestration fails | Unit (mocked) | ✅ Pass |
| TC-PIP-005 | run status=failed in DB after orchestration error | Unit (mocked) | ✅ Pass |

## S2-11 — Orchestration Engine

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-ORC-020 | direct_answer returns immediately (no further calls) | Unit (mocked) | ✅ Pass |
| TC-ORC-021 | 1-stage plan: 3 orchestrator calls (plan, re_plan, compose) | Unit (mocked) | ✅ Pass |
| TC-ORC-022 | re_plan action=fail raises BRT-ORC-006 | Unit (mocked) | ✅ Pass |
| TC-ORC-023 | re_plan action=continue dispatches next_nodes | Unit (mocked) | ✅ Pass |
| TC-ORC-024 | orchestrator_decision events logged for each call | Unit (mocked) | ✅ Pass |

## S2-10 — Fold-In Verifier

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-ORC-010 | No residue → empty dict returned | Unit | ✅ Pass |
| TC-ORC-011 | Residue found → event logged + scratch cleaned | Unit | ✅ Pass |
| TC-ORC-012 | Digest contains sample_tables and residue_count | Unit | ✅ Pass |
| TC-ORC-013 | No scratch rows remain after fold_in | Unit | ✅ Pass |
| TC-ORC-014 | child_residue event logged with correct model_id | Unit | ✅ Pass |

## S2-09 — apply_to_each

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-APE-001 | 10 items, all succeed → results has 10 entries | Unit (mocked) | ✅ Pass |
| TC-APE-002 | 2/5 failures → failures[], not raised | Unit (mocked) | ✅ Pass |
| TC-APE-003 | BRT-APE-001 raised when items not resolvable | Unit | ✅ Pass |
| TC-APE-004 | BRT-APE-003 raised for invalid concurrency_limit | Unit | ✅ Pass |
| TC-APE-005 | BRT-APE-004 raised when depth limit exceeded | Unit | ✅ Pass |
| TC-APE-006 | Results are ordered by index | Unit (mocked) | ✅ Pass |
| TC-APE-007 | 100% failure still returns resolved envelope | Unit (mocked) | ✅ Pass |
| TC-APE-008 | _level_suffix produces correct alphabetic suffixes | Unit | ✅ Pass |

## S2-08 — Skill Library

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-SKL-001 | load() populates skills from data/skills.yaml | Unit | ✅ Pass |
| TC-SKL-002 | get() returns skill by id | Unit | ✅ Pass |
| TC-SKL-003 | get() returns highest version when version=None | Unit | ✅ Pass |
| TC-SKL-004 | BRT-SKL-003 raised for unknown skill_id | Unit | ✅ Pass |
| TC-SKL-005 | BRT-SKL-004 raised for wrong version | Unit | ✅ Pass |
| TC-SKL-006 | BRT-SKL-001 raised for missing file | Unit | ✅ Pass |
| TC-SKL-007 | catalogue_for_prompt() includes all skill ids | Unit | ✅ Pass |
| TC-SKL-008 | render() returns valid WrappedTask for classify_into_buckets | Unit | ✅ Pass |
| TC-SKL-009 | Rendered WrappedTask passes validate_outbound | Unit | ✅ Pass |
| TC-SKL-010 | BRT-SKL-005 raised when required arg missing | Unit | ✅ Pass |
| TC-SKL-011 | log_usage() writes to skill_usage_log | Unit | ✅ Pass |

## S2-07 — State Operators

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-SOP-001 | op_filter keeps matching items | Unit | ✅ Pass |
| TC-SOP-002 | op_filter negate keeps non-matching | Unit | ✅ Pass |
| TC-SOP-003 | op_count returns total count | Unit | ✅ Pass |
| TC-SOP-004 | op_count group_by returns grouped counts | Unit | ✅ Pass |
| TC-SOP-005 | op_group_by groups items by key | Unit | ✅ Pass |
| TC-SOP-006 | op_sample returns n items | Unit | ✅ Pass |
| TC-SOP-007 | op_sample with seed is deterministic | Unit | ✅ Pass |
| TC-SOP-008 | BRT-SOP-001 raised for unknown operator | Unit | ✅ Pass |
| TC-SOP-009 | BRT-SOP-002 raised when required param missing | Unit | ✅ Pass |
| TC-SOP-010 | BRT-SOP-003 raised when items not in dependency_input | Unit | ✅ Pass |
| TC-SOP-011 | BRT-SOP-005 raised when group_by key absent | Unit | ✅ Pass |
| TC-SOP-012 | dispatch_state_op returns resolved InboundEnvelope | Unit | ✅ Pass |

## S2-06 — Agent Executor

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-ANS-001 | execute(answerer) returns resolved InboundEnvelope | Unit (mocked) | ✅ Pass |
| TC-ANS-002 | Governance injected into system prompt | Unit (mocked) | ✅ Pass |
| TC-ANS-003 | BRT-ANS-001 raised when model_endpoint is None | Unit | ✅ Pass |
| TC-ANS-004 | Unparseable JSON → failed envelope (not crash) | Unit (mocked) | ✅ Pass |
| TC-ANS-005 | Missing output key → failed envelope (BRT-ANS-005) | Unit (mocked) | ✅ Pass |
| TC-ANS-006 | scratch_cleanup directives applied after successful call | Unit (mocked) | ✅ Pass |
| TC-ANS-007 | model_call event logged after successful call | Unit (mocked) | ✅ Pass |
| TC-ANS-008 | HTTP error from model → BRT-ANS-002 | Unit (mocked) | ✅ Pass |

## S2-05 — Log Writer

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-LOG-001 | write_event lands in logs table | Unit | ✅ Pass |
| TC-LOG-002 | payload_in stored with direction='in' | Unit | ✅ Pass |
| TC-LOG-003 | payload_out stored with direction='out' | Unit | ✅ Pass |
| TC-LOG-004 | BRT-LOG-001 raised for invalid event_type | Unit | ✅ Pass |
| TC-LOG-005 | decision dict serialised to JSON | Unit | ✅ Pass |
| TC-LOG-006 | write_event with only event_type succeeds | Unit | ✅ Pass |
| TC-LOG-007 | All 7 valid event_types accepted | Unit | ✅ Pass |

## S2-04 — DB State + Scratch Helpers

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-DB-020 | create_run returns UUID, record persisted | Unit | ✅ Pass |
| TC-DB-021 | update_run_status transitions correctly + sets completed_at | Unit | ✅ Pass |
| TC-DB-022 | BRT-DB-010 raised for unknown run_id | Unit | ✅ Pass |
| TC-DB-023 | BRT-DB-012 raised for invalid status value | Unit | ✅ Pass |
| TC-DB-024 | save_envelope inserts with status=in_flight | Unit | ✅ Pass |
| TC-DB-025 | update_envelope_inbound marks envelope completed | Unit | ✅ Pass |
| TC-DB-026 | BRT-DB-011 raised for unknown envelope_id | Unit | ✅ Pass |
| TC-DB-027 | BRT-DB-010 raised by get_run for unknown id | Unit | ✅ Pass |
| TC-DB-028 | get_envelopes returns ordered list | Unit | ✅ Pass |
| TC-DB-029 | get_config returns None for missing key | Unit | ✅ Pass |
| TC-DB-030 | set_config + get_config roundtrip (str and dict) | Unit | ✅ Pass |
| TC-DB-040 | write_scratch inserts row and returns id | Unit | ✅ Pass |
| TC-DB-041 | read_scratch_for_level returns rows with parsed data | Unit | ✅ Pass |
| TC-DB-042 | delete_scratch_for_level removes rows and returns count | Unit | ✅ Pass |
| TC-DB-043 | count_scratch_for_level returns correct count | Unit | ✅ Pass |
| TC-DB-044 | count_scratch_for_level returns 0 when empty | Unit | ✅ Pass |
| TC-DB-045 | apply_cleanup_directives with ids deletes specified rows | Unit | ✅ Pass |
| TC-DB-046 | apply_cleanup_directives with row_id deletes single row | Unit | ✅ Pass |
| TC-DB-047 | apply_cleanup_directives ignores write actions | Unit | ✅ Pass |
| TC-DB-048 | Level-path isolation — rows from different levels don't mix | Unit | ✅ Pass |

## S2-03 — Database Schema

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-DB-001 | create_all is idempotent | Unit | ✅ Pass |
| TC-DB-002 | All 8 tables exist after create_all | Unit | ✅ Pass |
| TC-DB-003 | runs.status CHECK rejects invalid values | Unit | ✅ Pass |
| TC-DB-004 | subtasks.status CHECK rejects invalid values | Unit | ✅ Pass |
| TC-DB-005 | logs.event_type CHECK rejects invalid values | Unit | ✅ Pass |
| TC-DB-006 | log_payloads.direction CHECK rejects invalid values | Unit | ✅ Pass |
| TC-DB-007 | skill_usage_log.outcome CHECK rejects invalid values | Unit | ✅ Pass |
| TC-DB-008 | idx_scratch_run_level index exists | Unit | ✅ Pass |
| TC-DB-009 | config key is unique; upsert works | Unit | ✅ Pass |
| TC-DB-010 | Valid run record inserts cleanly | Unit | ✅ Pass |

## S2-02 — Envelope Validator

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-ENV-020 | validate_outbound passes for valid WrappedTask | Unit | ✅ Pass |
| TC-ENV-021 | BRT-ENV-003 raised on depth violation after mutation | Unit | ✅ Pass |
| TC-ENV-022 | BRT-ENV-004 raised on empty instruction after mutation | Unit | ✅ Pass |
| TC-ENV-023 | validate_inbound passes for resolved envelope matching schema | Unit | ✅ Pass |
| TC-ENV-024 | validate_inbound passes for failed envelope with failure_reason | Unit | ✅ Pass |
| TC-ENV-025 | BRT-ENV-012: failed envelope missing failure_reason raises | Unit | ✅ Pass |
| TC-ENV-026 | BRT-ENV-011: output missing required key raises | Unit | ✅ Pass |
| TC-ENV-027 | Extra output keys beyond schema are acceptable | Unit | ✅ Pass |
| TC-ENV-028 | Failed envelope with empty output passes (no schema check on failure) | Unit | ✅ Pass |
| TC-ENV-029 | Empty output_schema means no key check needed | Unit | ✅ Pass |

## S1-10 — Slice 1 End-to-End

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-E2E-001 | App starts and /health returns 200 | E2E | ✅ Pass |
| TC-E2E-002 | POST /models/discover returns upserted count | E2E (mocked ext) | ✅ Pass |
| TC-E2E-003 | Seed matrix has ≥1 local and ≥1 OpenRouter model | E2E | ✅ Pass |
| TC-E2E-004 | router.select_model('coding') returns valid ModelInfo | E2E | ✅ Pass |
| TC-E2E-005 | LOCAL_FIRST strategy returns a local model | E2E | ✅ Pass |
| TC-E2E-006 | GET /models returns all matrix models as JSON | E2E | ✅ Pass |
| TC-E2E-007 | GET /models/matrix returns all 13+ seed models | E2E | ✅ Pass |

## S1-08 — Registry API Endpoints

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-API-010 | GET /models returns a list (matrix loaded from disk) | Integration | ✅ Pass |
| TC-API-011 | GET /models/matrix returns full matrix as JSON array | Integration | ✅ Pass |
| TC-API-012 | POST /models/discover returns { upserted: int } | Integration (mocked) | ✅ Pass |
| TC-API-013 | POST /models/benchmark/{id} returns 404 BRT-API-002 on unknown model | Integration | ✅ Pass |
| TC-API-014 | POST /keys saves KEY_* and ENDPOINT_* env variables | Integration | ✅ Pass |
| TC-API-015 | POST /keys returns 400 BRT-API-001 on missing name | Integration | ✅ Pass |

## S1-07 — Model Benchmark

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-REG-050 | _load_prompts loads seed file with all 7 task_types, 5 prompts each | Unit | ✅ Pass |
| TC-REG-051 | BRT-REG-011 raised on missing prompts file | Unit | ✅ Pass |
| TC-REG-052 | benchmark_model returns score, throughput_tps, elapsed_sec | Unit (mocked) | ✅ Pass |
| TC-REG-053 | Score is averaged across all prompts | Unit (mocked) | ✅ Pass |
| TC-REG-054 | BRT-REG-015 raised when model has no endpoint | Unit | ✅ Pass |
| TC-REG-055 | benchmark_all updates matrix scores and throughput | Unit (mocked) | ✅ Pass |

## S1-06 — Model Discovery

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-REG-040 | discover_lmstudio parses /v1/models response correctly | Unit (mocked) | ✅ Pass |
| TC-REG-041 | discover_lmstudio returns [] on connection failure (silent skip) | Unit (mocked) | ✅ Pass |
| TC-REG-042 | discover_ollama parses /api/tags correctly | Unit (mocked) | ✅ Pass |
| TC-REG-043 | discover_ollama returns [] on connection failure (silent skip) | Unit (mocked) | ✅ Pass |
| TC-REG-044 | discover_openrouter parses pricing + context_length; filters image models | Unit (mocked) | ✅ Pass |
| TC-REG-045 | discover_openrouter returns [] on connection failure | Unit (mocked) | ✅ Pass |
| TC-REG-046 | discover_all upserts models and preserves existing task_scores | Unit (mocked) | ✅ Pass |

## S1-05 — Model Router

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-REG-030 | SYSTEM_CHOOSE picks cheapest qualifying coding model | Unit | ✅ Pass |
| TC-REG-031 | LOCAL_FIRST restricts to local models when one qualifies | Unit | ✅ Pass |
| TC-REG-032 | LOCAL_FIRST falls back to external when quality bar eliminates all locals | Unit | ✅ Pass |
| TC-REG-033 | specialty_hint prefers models with matching tag | Unit | ✅ Pass |
| TC-REG-034 | BRT-REG-020 raised when quality requirement is impossibly high | Unit | ✅ Pass |
| TC-REG-035 | Roleplay task returns cheapest qualifying roleplay model | Unit | ✅ Pass |

## S1-04 — Effective Cost Formula

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-REG-020 | Local model returns time-only cost (no dollars) | Unit | ✅ Pass |
| TC-REG-021 | External model returns dollar + time combined cost | Unit | ✅ Pass |
| TC-REG-022 | BRT-REG-013 raised on zero/negative throughput_tps | Unit | ✅ Pass |
| TC-REG-023 | Local model with no throughput_tps returns 0.0 | Unit | ✅ Pass |
| TC-REG-024 | Custom time_cost_per_sec applied correctly | Unit | ✅ Pass |

## S1-03 — CapabilityMatrix

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-REG-010 | Load seed file; 13 models, local + external present | Unit | ✅ Pass |
| TC-REG-011 | query_by_task('coding') returns highest-score model first | Unit | ✅ Pass |
| TC-REG-012 | BRT-REG-010 raised on missing file | Unit | ✅ Pass |
| TC-REG-013 | BRT-REG-011 raised on malformed YAML (non-list) | Unit | ✅ Pass |
| TC-REG-014 | BRT-REG-012 raised on update_score for unknown model | Unit | ✅ Pass |
| TC-REG-015 | upsert inserts new model and replaces existing one | Unit | ✅ Pass |
| TC-REG-016 | update_throughput persists tps value | Unit | ✅ Pass |

## S1-02 — ModelInfo Pydantic Model

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-REG-001 | ModelInfo roundtrip serialisation (local + external) | Unit | ✅ Pass |
| TC-REG-002 | BRT-REG-001 raised on invalid source | Unit | ✅ Pass |
| TC-REG-003 | BRT-REG-002 raised on negative pricing | Unit | ✅ Pass |
| TC-REG-004 | Local model with no pricing/throughput is valid | Unit | ✅ Pass |
| TC-REG-005 | task_scores and specialty_tags default to empty | Unit | ✅ Pass |

## S1-00 — Project Scaffold

| ID | Description | Level | Status |
|----|-------------|-------|--------|
| TC-API-001 | `GET /health` returns HTTP 200 and `{"status":"ok"}` | Unit | ✅ Pass |
| TC-API-002 | `POST /health` returns HTTP 405 Method Not Allowed | Unit | ✅ Pass |
