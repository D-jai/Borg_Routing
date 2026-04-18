# Test Plan — BORG_ROUTE
> Naming: `TC-[MODULE]-[NNN] — Plain English description`
> Three levels: Unit (mocked) → Integration (external mocked) → E2E (full system)

---

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
