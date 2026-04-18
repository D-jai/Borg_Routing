# Test Plan — BORG_ROUTE
> Naming: `TC-[MODULE]-[NNN] — Plain English description`
> Three levels: Unit (mocked) → Integration (external mocked) → E2E (full system)

---

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
