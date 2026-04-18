# Test Plan — BORG_ROUTE
> Naming: `TC-[MODULE]-[NNN] — Plain English description`
> Three levels: Unit (mocked) → Integration (external mocked) → E2E (full system)

---

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
