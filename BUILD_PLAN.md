# Build Plan — BORG_ROUTE (Slice-Based, Extreme-Spec)
> Last updated: 2026-04-18 (Session 2 — reassessment + extreme-spec expansion)
> Each slice produces a working, tested, committed increment.
> Read only the slice you are currently working on.
> Check off tasks as you complete them. Update CHECKPOINT.md after every session.

---

## How To Use This Plan

Each task is specified to a level of detail a **cheaper builder model** can
follow without ambiguity. Every task carries:

- **File path(s)** it creates or edits
- **Function signatures** (where the function is non-obvious)
- **Error codes** to raise (pre-assigned so the Parallel Commit Law is easy)
- **What to write in the doc triad** (user-guide, presentation, test-plan)
- **Builder model tier** — Heavy / Mid / Light — guiding which model to use

Builder model tiers (from `DESIGN_PRINCIPLES.md §12`):
- **Heavy** — Claude Sonnet 4 or equivalent flagship
- **Mid** — DeepSeek V3.1 or Qwen3-Coder
- **Light** — Claude Haiku 4.5 or local 27B

The Parallel Commit Law (from `CODING_STANDARD_CORE.md`) applies to EVERY
feat: commit — code + error handling + user-guide + presentation + tests.
Builder should treat each task as one commit.

---

## Slice 0: Scaffolding ✅ Complete
Docs, git, architecture reassessed (Session 2), prompts written into
`src/prompts/`, skills catalogue written into `data/skills.yaml`,
capability matrix seeded in `data/capability_matrix.yaml`.

---

## Slice 1: Registry Foundation ✅ Complete
**Goal:** System can discover models from LM Studio, Ollama, OpenRouter, and
user-provided API keys. Capability matrix with pricing + throughput. Router
that selects a model for a task spec using effective_cost. No envelopes yet.

**Verify:** ✅ `POST /models/discover` populates matrix. ✅ `GET /models/matrix`
returns all discovered + seeded models. ✅ Router unit test selects correct model
for a given task spec. ✅ `POST /models/benchmark/{id}` runs a 5-prompt suite.
**Tests:** 53 passing.

---

### S1-00 — Create Python project skeleton
**Builder:** Light
**Files:** `pyproject.toml`, `main.py`, `.python-version`
**pyproject.toml dependencies:**
```toml
[project]
name = "borg_route"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "httpx>=0.28",
    "aiosqlite>=0.20",
    "python-dotenv>=1.0",
    "pydantic>=2.9",
    "pyyaml>=6.0",
    "spacy>=3.8",
]
[project.optional-dependencies]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24", "httpx>=0.28"]
```
**main.py:** FastAPI app, mounts `ui/` as static, startup event reads `.env`
via `python-dotenv.load_dotenv()`, health endpoint `GET /health → {"status": "ok"}`.
**Docs updates:** `install-guide.md` install steps; `error-registry.md` no new codes.

---

### S1-01 — Seed data files (already exist; verify)
**Builder:** n/a — already written during Session 2
**Files (verify exist, do not regenerate):**
- `data/capability_matrix.yaml` — 8 local + 5 external seed rows
- `data/skills.yaml` — 10 skills
- `src/prompts/base.py`, `src/prompts/orchestrator.py`, `src/prompts/answerer.py`

**Action:** builder creates `src/prompts/__init__.py` if missing; verifies
imports work with `python -c "from src.prompts import ORCHESTRATOR_SYSTEM_PROMPT"`.

---

### S1-02 — `src/registry/models.py` — The `ModelInfo` Pydantic model
**Builder:** Light
**Create:** `src/registry/__init__.py` (empty), `src/registry/models.py`
**Signature:**
```python
class Pricing(BaseModel):
    input_per_token: float
    output_per_token: float

class ModelInfo(BaseModel):
    model_id: str                   # "lmstudio/gemma-4-e4b-it"
    source: Literal["lmstudio", "ollama", "openrouter", "custom_key"]
    endpoint: Optional[str] = None  # for locals / custom keys
    context_length: int
    pricing: Optional[Pricing] = None   # None for local
    throughput_tps: Optional[float] = None
    task_scores: dict[str, float] = {}  # task_type -> 0..10
    specialty_tags: list[str] = []
    params_b: Optional[float] = None
    publisher: Optional[str] = None
    notes: Optional[str] = None
```
**Errors:** BRT-REG-001 on invalid source, BRT-REG-002 on negative pricing.
**Tests:** `tests/registry/test_models.py` — roundtrip serialisation, validation edge cases.

---

### S1-03 — `src/registry/capability_matrix.py` — Load/save YAML
**Builder:** Mid
**Create:** `src/registry/capability_matrix.py`
**Signatures:**
```python
class CapabilityMatrix:
    def __init__(self, path: str = "data/capability_matrix.yaml"): ...
    def load(self) -> None: ...
    def save(self) -> None: ...
    def all(self) -> list[ModelInfo]: ...
    def get(self, model_id: str) -> Optional[ModelInfo]: ...
    def query_by_task(self, task_type: str, min_score: float = 0,
                       min_context_length: int = 0) -> list[ModelInfo]: ...
    def upsert(self, model: ModelInfo) -> None: ...
    def update_score(self, model_id: str, task_type: str, score: float) -> None: ...
    def update_throughput(self, model_id: str, tps: float) -> None: ...
```
**Errors:** BRT-REG-010 (file not found), BRT-REG-011 (malformed yaml),
BRT-REG-012 (unknown model_id on update).
**Tests:** load seed file; query_by_task('coding') returns qwen3-coder first.

---

### S1-04 — `src/registry/cost.py` — Effective cost formula
**Builder:** Light
**Create:** `src/registry/cost.py`
**Signatures:**
```python
def effective_cost(
    model: ModelInfo,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    time_cost_per_sec: float = 0.0083,   # $30/hr default
) -> float:
    """Unified cost: dollars + (tokens/throughput) * time_cost_per_sec."""
```
**Logic:**
- If `model.pricing`: `dollars = input × input_per_token + output × output_per_token`
- Else: `dollars = 0`
- `time_seconds = (input + output) / max(model.throughput_tps, 1)`
- `return dollars + time_seconds × time_cost_per_sec`
**Errors:** BRT-REG-013 (zero/negative throughput).
**Tests:** local model returns time-only cost; external returns dollar+time combined.

---

### S1-05 — `src/registry/router.py` — select_model()
**Builder:** Mid
**Create:** `src/registry/router.py`
**Signature:**
```python
def select_model(
    matrix: CapabilityMatrix,
    task_type: str,
    estimated_input_tokens: int = 1000,
    estimated_output_tokens: int = 500,
    quality_requirement: float = 6.0,
    strategy: Literal["LOCAL_FIRST", "SYSTEM_CHOOSE"] = "SYSTEM_CHOOSE",
    specialty_hint: Optional[str] = None,
    time_cost_per_sec: float = 0.0083,
) -> ModelInfo:
    """Specialty-primary selection. Returns cheapest model meeting quality + context bar."""
```
**Logic:**
1. candidates = `matrix.query_by_task(task_type, min_score=quality_requirement,
   min_context_length=estimated_input_tokens)`
2. If `specialty_hint` given: prefer candidates with that tag in `specialty_tags`.
3. If `strategy == LOCAL_FIRST`: first try `[m for m in candidates if m.source in ("lmstudio","ollama")]`; if any meet bar, use only those.
4. Sort by `effective_cost(m, est_in, est_out, time_cost_per_sec)`.
5. Return first. Raise BRT-REG-020 if empty.
**Errors:** BRT-REG-020 (no model meets requirements).
**Tests:** coding task → qwen3-coder or deepseek; roleplay → mistral-small-creative or llama-3.3-8b-heretic; LOCAL_FIRST keeps externals out unless no local qualifies.

---

### S1-06 — `src/registry/discovery.py` — Discover local + external models
**Builder:** Mid
**Create:** `src/registry/discovery.py`
**Signatures:**
```python
async def discover_lmstudio(ports: list[int] = [1234,1235,1236,1237]) -> list[ModelInfo]: ...
async def discover_ollama(base_url: str = "http://localhost:11434") -> list[ModelInfo]: ...
async def discover_openrouter() -> list[ModelInfo]: ...
async def discover_custom_key(name: str, base_url: str, api_key: str) -> list[ModelInfo]: ...
async def discover_all(matrix: CapabilityMatrix, openrouter_key: Optional[str] = None) -> int:
    """Returns count of models upserted."""
```
**Implementation notes:**
- LM Studio: `GET {base}/v1/models` (OpenAI-compat). 2s timeout.
- Ollama: `GET {base}/api/tags`, map each `name` to `ollama/{name}`.
- OpenRouter: `GET https://openrouter.ai/api/v1/models` (no auth required for
  listing), parse `pricing.prompt`, `pricing.completion`, `context_length`.
  Filter out image-gen models (`modality` contains "image") per user preference.
- Each discovered model gets upserted into matrix. Preserve existing
  task_scores and throughput_tps if model already exists (don't overwrite
  from benchmarks with discovery defaults).
**Errors:** BRT-REG-001 (connection failure — silent skip, log only),
BRT-REG-003 (rate limit from OpenRouter), BRT-REG-004 (invalid key).
**Tests:** mock httpx, assert parsing of fixture responses.

---

### S1-07 — `src/registry/benchmark.py` — Quality + throughput measurement
**Builder:** Mid
**Create:** `src/registry/benchmark.py`, `data/benchmark_prompts.yaml`
**`benchmark_prompts.yaml`:** 5 prompts per task_type (reasoning, coding,
summarize, writing, roleplay, agentic, extraction). Each prompt has an
`expected_shape` (rubric for 0-10 scoring).
**Signatures:**
```python
async def benchmark_model(
    model: ModelInfo,
    task_type: str,
    prompts: list[dict],
    time_cost_per_sec: float = 0.0083,
) -> dict:
    """Returns {'score': 0..10, 'throughput_tps': float, 'elapsed_sec': float}."""

async def benchmark_all(matrix: CapabilityMatrix, task_types: list[str]) -> dict: ...
```
**Logic:**
- For each prompt: call model via httpx, measure wall-clock and output-token count.
- Score using a rubric model call (use a heavy model — claude-sonnet-4 — as the judge).
- Average scores = quality_score.
- Total output_tokens / total wall_clock = throughput_tps.
- Update matrix.
**Errors:** BRT-REG-011..015 (benchmark failures).
**Tests:** mocked; assert score averaging + throughput calc.

---

### S1-08 — `src/api/models.py` — Registry endpoints
**Builder:** Light
**Create:** `src/api/__init__.py`, `src/api/models.py`
**Endpoints:**
```
GET /models                             → list[ModelInfo]
POST /models/discover                   → { "added": int, "updated": int }
POST /models/benchmark/{model_id}       → { "score": float, "throughput_tps": float }
GET /models/matrix                      → full capability_matrix.yaml content as JSON
POST /keys                              → writes KEY_{NAME} env vars to .env;
                                          body: { name, base_url, api_key }
```
**Errors:** BRT-API-001 (bad request), BRT-API-002 (model not found).
**Tests:** FastAPI TestClient; assert shapes.

---

### S1-09 — `ui/models.html` + `ui/keys.html`
**Builder:** Light
**Files:**
- `ui/models.html` — table with columns (source, model_id, params, context, tier badges derived from task_scores max, throughput_tps, dollar_estimate_per_1k_out_tokens, [Benchmark] button). "Discover" button at top.
- `ui/keys.html` — form rows × 5 (name / base_url / api_key). OpenRouter pre-filled as row 1. Submit button POSTs to `/keys`.
- Both pages include a minimal shared header `ui/_header.html` with nav links.
**Docs:** `user-guide.md` — "Adding API Keys" + "Discovering Models" sections.

---

### S1-10 — Slice 1 end-to-end test
**Builder:** Mid
**Create:** `tests/e2e/test_slice_1.py`
**Asserts:**
- App starts
- `POST /models/discover` succeeds (mock external, real local-if-running)
- Matrix contains at least one local + one openrouter entry
- `router.select_model("coding")` returns a sensible model id
- No regressions in unit tests

**Slice 1 done when:** all S1-* checked; tests green; CHECKPOINT updated;
session committed + zipped.

---

## Slice 2: Core Pattern End-to-End (THE MILESTONE)

**Goal:** Prove the architecture works. OpenAI-compat API (B) + MCP server (D)
both live. User can POST a task via chat-completions OR as an MCP tool call;
orchestrator plans, dispatches `apply_to_each` + `state_op`, cascades a
summary, composer produces final response. Fold-in cleanup runs;
`child_residue` events logged when applicable. No recursion across runs yet,
no admin UI, no memory window.

**Verify:** Submit "classify these 10 github issues into 3 buckets and
summarise the distribution" via both surfaces. Both return a coherent final
answer. Run trace UI shows orchestrator plan, apply_to_each expansion,
state_op counts, compose step. Tests green.

---

### S2-01 — `src/envelopes/schema.py` — WrappedTask + InboundEnvelope
**Builder:** Heavy
**Create:** `src/envelopes/__init__.py`, `src/envelopes/schema.py`
**Signatures:**
```python
class SideEffectAction(BaseModel):
    table: str
    action: Literal["write", "delete"]
    row_id: Optional[str] = None
    ids: Optional[list[str]] = None

class WrappedTask(BaseModel):
    envelope_id: UUID
    run_id: UUID
    parent_run_id: Optional[UUID] = None
    subtask_id: UUID
    type: Literal["orchestrate", "answerer", "apply_to_each", "state_op"]
    current_depth: int
    max_depth: int
    level_path: str                 # e.g. "4d"
    model_endpoint: Optional[str] = None
    model_name: Optional[str] = None
    instruction: str
    context: str = ""
    state_query: Optional[str] = None
    side_effects: list[str] = []    # scratch table names allowed to be written
    dependency_input: dict = {}
    output_schema: dict
    max_output_tokens: int = 1000
    trigger_reason: str = "initial_plan"
    skill_id: Optional[str] = None
    skill_version: Optional[int] = None
    skill_args: Optional[dict] = None

class InboundEnvelope(BaseModel):
    envelope_id: UUID
    subtask_id: UUID
    status: Literal["resolved", "failed"]    # NO "unresolved"
    confidence: Optional[float] = None
    output: dict
    failure_reason: Optional[str] = None
    tokens_used: dict = {}   # {"input": int, "output": int}
    scratch_writes: list[SideEffectAction] = []
    scratch_cleanup: list[SideEffectAction] = []
```
**Errors:** BRT-ENV-001..005.
**Tests:** roundtrip; `status="unresolved"` rejected; required fields.

---

### S2-02 — `src/envelopes/validator.py` — Contract enforcement
**Builder:** Heavy
**Create:** `src/envelopes/validator.py`
**Signatures:**
```python
def validate_outbound(task: WrappedTask) -> None:
    """Raise BRT-ENV-001..005 if invalid."""

def validate_inbound(env: InboundEnvelope, task: WrappedTask) -> None:
    """Raise BRT-ENV-010 if answerer returned 'unresolved' (impossible per schema).
       Raise BRT-ENV-011 if output doesn't match task.output_schema.
       Raise BRT-ENV-012 if failure_reason missing when status=failed."""
```
**Errors:** BRT-ENV-010 answerer contract violation; BRT-ENV-011 schema mismatch;
BRT-ENV-012 missing failure reason.
**Tests:** crafted envelopes trigger each error.

---

### S2-03 — `src/db/schema.py` — All tables for Slice 2
**Builder:** Heavy
**Create:** `src/db/__init__.py`, `src/db/schema.py`, `src/db/migrations/`
**Tables (SQL shown inline in schema.py, executed on startup via `create_all()`):**
```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  parent_run_id TEXT,
  status TEXT NOT NULL,  -- pending|running|completed|failed
  user_instruction TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMP,
  user_rating INTEGER  -- set by feedback endpoint
);

CREATE TABLE subtasks (
  subtask_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  type TEXT NOT NULL,
  instruction TEXT NOT NULL,
  status TEXT NOT NULL,  -- pending|in_flight|done|failed
  output TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE envelopes (
  envelope_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  subtask_id TEXT NOT NULL,
  type TEXT NOT NULL,
  level_path TEXT NOT NULL,
  model_id TEXT,
  trigger_reason TEXT NOT NULL,
  skill_id TEXT,
  skill_version INTEGER,
  status TEXT NOT NULL,
  outbound_payload TEXT NOT NULL,   -- JSON
  inbound_payload TEXT,              -- JSON
  tokens_input INTEGER,
  tokens_output INTEGER,
  dollars NUMERIC,
  latency_ms INTEGER,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMP
);

CREATE TABLE scratch (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  level_path TEXT NOT NULL,       -- who wrote this
  envelope_id TEXT NOT NULL,      -- which envelope wrote this
  table_name TEXT NOT NULL,       -- logical table: deferrals, cross_refs, etc.
  data TEXT NOT NULL,              -- JSON
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_scratch_run_level ON scratch(run_id, level_path);

CREATE TABLE logs (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  envelope_id TEXT,
  event_type TEXT NOT NULL,
      -- model_call | orchestrator_decision | child_residue
      -- escalation | deferral | reorchestration | error
  model_id TEXT,
  tokens_input INTEGER,
  tokens_output INTEGER,
  dollars NUMERIC,
  latency_ms INTEGER,
  decision_json TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_logs_run ON logs(run_id);
CREATE INDEX idx_logs_event_type ON logs(event_type);

CREATE TABLE log_payloads (
  payload_id INTEGER PRIMARY KEY AUTOINCREMENT,
  log_id INTEGER NOT NULL REFERENCES logs(log_id),
  direction TEXT NOT NULL,   -- in|out
  text TEXT NOT NULL,
  ner_processed BOOLEAN DEFAULT 0
);

CREATE TABLE skill_usage_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  skill_id TEXT NOT NULL,
  skill_version INTEGER NOT NULL,
  run_id TEXT,
  envelope_id TEXT,
  outcome TEXT NOT NULL,    -- success|partial|failure
  tokens_used INTEGER,
  latency_ms INTEGER,
  composer_accepted BOOLEAN,
  child_residue_count INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
**Errors:** BRT-DB-001..009.
**Tests:** tables exist after `create_all()`; constraints work.

---

### S2-04 — `src/db/state.py` + `src/db/scratch.py` — Async helpers
**Builder:** Heavy
**Create:** `src/db/state.py`, `src/db/scratch.py`
**`state.py` signatures:**
```python
async def create_run(user_instruction: str) -> UUID: ...
async def update_run_status(run_id: UUID, status: str) -> None: ...
async def save_envelope(task: WrappedTask) -> None: ...
async def update_envelope_inbound(envelope_id: UUID, env: InboundEnvelope) -> None: ...
async def get_run(run_id: UUID) -> dict: ...
async def get_envelopes(run_id: UUID) -> list[dict]: ...
async def get_config(key: str) -> Optional[str]: ...
async def set_config(key: str, value: str) -> None: ...
```
**`scratch.py` signatures:**
```python
async def write_scratch(run_id: UUID, level_path: str, envelope_id: UUID,
                          table_name: str, data: dict) -> int: ...
async def read_scratch_for_level(run_id: UUID, level_path: str) -> list[dict]: ...
async def delete_scratch_for_level(run_id: UUID, level_path: str) -> int: ...
async def count_scratch_for_level(run_id: UUID, level_path: str) -> int: ...
async def apply_cleanup_directives(run_id: UUID,
                                    directives: list[SideEffectAction]) -> None: ...
```
**Errors:** BRT-DB-010..019.
**Tests:** concurrent writes; fold-in residue detection.

---

### S2-05 — `src/logging/writer.py` — Fire-and-forget event logger
**Builder:** Heavy
**Create:** `src/logging/__init__.py`, `src/logging/writer.py`
**Signatures:**
```python
async def write_event(
    event_type: str,
    run_id: Optional[UUID] = None,
    envelope_id: Optional[UUID] = None,
    model_id: Optional[str] = None,
    tokens_input: Optional[int] = None,
    tokens_output: Optional[int] = None,
    dollars: Optional[float] = None,
    latency_ms: Optional[int] = None,
    decision: Optional[dict] = None,
    payload_in: Optional[str] = None,
    payload_out: Optional[str] = None,
) -> None:
    """Non-blocking. Schedules writes via asyncio.create_task."""
```
**Event types (allowed values):** `model_call`, `orchestrator_decision`,
`child_residue`, `escalation`, `deferral`, `reorchestration`, `error`.
**Errors:** BRT-LOG-001..009.
**Tests:** events land in `logs`; payloads land in `log_payloads` with `ner_processed=False`.

---

### S2-06 — `src/agents/executor.py` — Stateless agent caller
**Builder:** Heavy
**Create:** `src/agents/__init__.py`, `src/agents/executor.py`
**Signature:**
```python
async def execute(task: WrappedTask) -> InboundEnvelope:
    """
    For type=answerer: build system prompt, call model via httpx, validate inbound,
                        return envelope, log model_call event.
    For type=state_op: dispatch to src/orchestrator/state_ops.py (no model).
    For type=apply_to_each: dispatch to src/orchestrator/apply_to_each.py.
    For type=orchestrate: dispatch to src/orchestrator/engine.py recursively.
    """
```
**Logic for answerer:**
1. Load governance: `gov = await load_governance(get_config)`
2. Build system prompt: `sys = build_system_prompt(ANSWERER_SYSTEM_PROMPT.format(**task_fields), gov['global_rules'], gov['admin_rules_ledger'], user_instruction)`
3. Call model: `httpx POST {endpoint}/v1/chat/completions` with system=sys, user="begin"
4. Parse response JSON, construct `InboundEnvelope`
5. Validate: `validate_inbound(env, task)` → if fails, set status=failed
6. If scratch_cleanup present: `apply_cleanup_directives(...)`
7. Log `model_call` event with tokens, dollars, latency
8. Return env
**Errors:** BRT-ANS-001..010.
**Tests:** mock httpx; assert correct governance injection; assert validation on bad inbound.

---

### S2-07 — `src/orchestrator/state_ops.py` — Deterministic operators
**Builder:** Mid
**Create:** `src/orchestrator/__init__.py`, `src/orchestrator/state_ops.py`
**Signatures:**
```python
async def op_filter(run_id, source_ref, params) -> list[dict]: ...
async def op_count(run_id, source_ref, params) -> int | dict: ...
async def op_group_by(run_id, source_ref, params) -> dict[str, list[dict]]: ...
async def op_sample(run_id, source_ref, params) -> list[dict]: ...
async def dispatch_state_op(task: WrappedTask) -> InboundEnvelope: ...
```
**Logic:** all operate on a SQL view of a source (scratch table, envelope outputs, etc.)
without any model calls. Pure Python/SQL.
**Errors:** BRT-SOP-001..005.
**Tests:** each op produces expected output over seeded scratch.

---

### S2-08 — `src/skills/library.py` — Skill loader + renderer
**Builder:** Heavy
**Create:** `src/skills/__init__.py`, `src/skills/library.py`
**Signatures:**
```python
class SkillLibrary:
    def load(self, path: str = "data/skills.yaml") -> None: ...
    def get(self, skill_id: str, version: Optional[int] = None) -> dict: ...
    def catalogue_for_prompt(self) -> str:
        """Format the library as a compact section for inclusion in the
           orchestrator system prompt."""
    def render(self, skill_id: str, version: int, args: dict) -> WrappedTask:
        """Substitute args into the skill's outbound_template and return a
           WrappedTask ready for dispatch."""

async def log_usage(skill_id: str, skill_version: int, run_id: UUID,
                     envelope_id: UUID, outcome: str, tokens: int,
                     latency_ms: int, composer_accepted: Optional[bool] = None,
                     child_residue_count: int = 0) -> None: ...
```
**Errors:** BRT-SKL-001..010.
**Tests:** render `classify_into_buckets` with sample args; resulting WrappedTask passes validator.

---

### S2-09 — `src/orchestrator/apply_to_each.py` — Runtime expansion
**Builder:** Heavy
**Create:** `src/orchestrator/apply_to_each.py`
**Signature:**
```python
async def expand_and_run(task: WrappedTask) -> InboundEnvelope:
    """
    1. Resolve task.dependency_input['inputs_ref'] to a list of items.
    2. Create N child WrappedTask envelopes, one per item, type=answerer.
    3. Run with asyncio.Semaphore(concurrency_limit).
    4. Aggregate: successful outputs → results[], failed → failures[].
    5. Return InboundEnvelope with status=resolved, output={results, failures}.
       (Best-effort: even if 100% failed, the apply_to_each node 'resolves'.)
    """
```
**Errors:** BRT-APE-001..005.
**Tests:** 10 items; inject 2 failures; assert aggregate shape.

---

### S2-10 — `src/orchestrator/fold_in.py` — Verifier + residue logging
**Builder:** Heavy
**Create:** `src/orchestrator/fold_in.py`
**Signature:**
```python
async def fold_in(run_id: UUID, child_level_path: str, child_model_id: str) -> dict:
    """
    Called by engine when a child orchestrate/answerer completes.
    1. count = await count_scratch_for_level(run_id, child_level_path)
    2. If count > 0:
        - residue_rows = await read_scratch_for_level(run_id, child_level_path)
        - await write_event('child_residue', run_id=..., model_id=child_model_id,
                             decision={'residue_count': count, 'rows_sample': first_3})
    3. Digest = brief summary of residue_rows for parent's context
    4. await delete_scratch_for_level(run_id, child_level_path)
    5. Return digest (may be empty dict if no residue).
    """
```
**Errors:** BRT-ORC-010..015.
**Tests:** child that leaves residue → `child_residue` logged, scratch cleaned.

---

### S2-11 — `src/orchestrator/engine.py` — Main orchestration loop
**Builder:** Heavy
**Create:** `src/orchestrator/engine.py`
**Signatures:**
```python
async def orchestrate_run(run_id: UUID, user_instruction: str,
                           thread_window: list[dict] = [],
                           rolling_summary: str = "") -> str:
    """Returns final response text. End-to-end orchestration."""

async def _invoke_orchestrator_model(mode: str, ...) -> dict:
    """One call to the orchestrator model. Build system prompt using
       ORCHESTRATOR_SYSTEM_PROMPT + governance; parse JSON response;
       log orchestrator_decision event."""

async def _dispatch_node(node: dict, run_id, depth, parent_level) -> InboundEnvelope:
    """Dispatch a single plan node through executor.execute()."""
```
**Main loop pseudocode:**
```
plan_result = _invoke_orchestrator_model("plan", ...)
if plan_result.direct_answer:
    return plan_result.direct_answer

current_nodes = plan_result.plan.nodes
results = {}
loop_count = 0
while loop_count < MAX_LOOPS:
    # dispatch ready nodes in parallel
    ready = [n for n in current_nodes if all(d in results for d in n.get('depends_on', []))]
    outcomes = await asyncio.gather(*[_dispatch_node(n, ...) for n in ready])
    for node, env in zip(ready, outcomes):
        results[node['id']] = env
        if node.type == 'orchestrate':
            digest = await fold_in(run_id, child_level, child_model)
            results[node['id']].output['_fold_digest'] = digest

    # re_plan
    rp = _invoke_orchestrator_model("re_plan", stage_results=results, ...)
    if rp.action == "compose": break
    if rp.action == "continue" or rp.action == "cascade":
        current_nodes = rp.next_nodes
    elif rp.action == "fail":
        return "I'm unable to complete this task. Reason: " + rp.rationale

# compose
comp = _invoke_orchestrator_model("compose", stage_results=results, ...)
return comp.response
```
**Errors:** BRT-ORC-001..009.
**Tests:** end-to-end with mocked models; assert orchestrator is called 3 times
(plan, re_plan, compose) for a 1-stage plan.

---

### S2-12 — `src/pipeline/runner.py` — Top-level entry
**Builder:** Mid
**Create:** `src/pipeline/__init__.py`, `src/pipeline/runner.py`
**Signature:**
```python
async def run_pipeline(user_instruction: str, thread_window: list[dict] = []) -> dict:
    """Returns {'run_id': UUID, 'response': str, 'dollars': float, 'latency_ms': int}."""
```
**Logic:** create run; orchestrate_run; mark complete; return summary.
**Errors:** BRT-PIP-001..005.

---

### S2-13 — `src/api/chat_completions.py` — OpenAI-compat gateway (Surface B)
**Builder:** Heavy
**Create:** `src/api/chat_completions.py`
**Endpoint:** `POST /v1/chat/completions`
**Accepts:** OpenAI chat-completions request body (messages, model, stream, max_tokens, etc.)
**Model aliases handled:**
- `borg_route` → full orchestration, SYSTEM_CHOOSE strategy
- `borg_route:light|mid|heavy` → quality_requirement set to 5/7/9
- `borg_route:coding|creative|reasoning|extraction` → specialty hint
- `borg_route:local` → LOCAL_FIRST strategy
- Any exact model_id in matrix → bypass orchestration, direct to that model
**Returns:** OpenAI-shaped response (choices[0].message.content, usage, etc.)
**Streaming:** if `stream=true`, emit SSE with OpenAI-compat `data:` lines.
**Errors:** BRT-API-010..019.
**Tests:** compat with `openai` Python client — assert a request from the
official SDK works unchanged.

---

### S2-14 — `src/api/tasks.py` — Batch/task endpoint (Surface C partial)
**Builder:** Mid
**Create:** `src/api/tasks.py`
**Endpoints:**
```
POST /v1/task         body: {instruction, inputs_ref (optional)}
                       → 202 { run_id }
GET /runs/{id}         → full run state (envelopes, events, digest)
```
**Errors:** BRT-API-020..029.
**Tests:** submit task, poll until complete, assert final response present.

---

### S2-15 — `src/mcp/server.py` — MCP server (Surface D)
**Builder:** Heavy
**Create:** `src/mcp/__init__.py`, `src/mcp/server.py`
**Tools exposed (MCP schema):**
- `borg_route.chat(messages: list[dict], model: str = "borg_route") -> dict`
- `borg_route.submit_task(instruction: str, inputs_ref: str = None) -> dict`
- `borg_route.get_run(run_id: str) -> dict`
- `borg_route.list_models() -> list[dict]`
**Mount:** `POST /mcp` endpoint that speaks MCP protocol over HTTP (streamable).
**Dependency:** Use `mcp` Python package if available; otherwise hand-roll the
JSON-RPC 2.0 protocol — it's small.
**Errors:** BRT-API-030..039.
**Tests:** use an MCP client (claude-dev or similar) against the running server;
invoke each tool.

---

### S2-16 — `ui/chat.html` + `ui/run.html` — Minimal reference UI
**Builder:** Light
**Files:**
- `ui/chat.html` — input textarea + submit → POSTs to `/v1/chat/completions` with `model: "borg_route"`; displays response. No streaming yet (Slice 6 adds SSE).
- `ui/run.html?id={run_id}` — fetches `GET /runs/{id}`; renders envelope table
  (columns: level_path, type, skill_id, trigger_reason, model, tokens, status);
  separate section showing orchestrator_decision events with rationale.
- `ui/_header.html` — shared nav (Chat, Models, Keys, Admin placeholder, Dashboard placeholder).

---

### S2-17 — Slice 2 E2E test — "classify + summarise 10 issues"
**Builder:** Mid
**Create:** `tests/e2e/test_slice_2.py`
**Seed data:** 10 fixture github-issue-like strings in `tests/fixtures/issues.json`.
**Task:** "Classify these 10 issues into buckets [bug, feature_request, question]
and summarise the distribution."
**Assertions:**
- `POST /v1/chat/completions` returns coherent response text
- Same request via MCP `borg_route.chat` returns same shape
- Run trace has: 1 orchestrator_decision (plan), 1 apply_to_each envelope
  expanded into 10 child envelopes, 1 state_op (count), 1 orchestrator_decision
  (re_plan), 1 orchestrator_decision (compose)
- No `child_residue` events (agents behaved)
- `skill_usage_log` has 1 entry for `classify_into_buckets` with outcome=success

**Slice 2 done when:** S2-01..S2-17 checked; both API surfaces serve the
canonical task; MCP server callable; run trace UI shows the full pipeline;
tests green; CHECKPOINT updated; zipped.

---

## Slice 3: Recursion + Admin + Resume + Multi-turn memory
Condensed — builder to detail with same discipline, borrowing patterns from
Slice 2.

### Tasks
- [ ] S3-01: `engine.py` — handle `type=orchestrate`: spawn child run, link `parent_run_id`, enforce `MAX_RECURSION_DEPTH=5`, demote at cap — BRT-ORC-020..029
- [ ] S3-02: `runner.py` — child runner folds digest into parent `dependency_input`
- [ ] S3-03: `src/db/state.py` — `resume_run()`: re-dispatch failed/in-flight only — BRT-DB-020
- [ ] S3-04: `src/api/admin.py` — `GET/POST /config`, `POST /config/rules` (append), `POST /config/rules/{id}/retire`
- [ ] S3-05: Rule ledger logic in config + governance loader
- [ ] S3-06: `POST /runs/{id}/resume`
- [ ] S3-07: `ui/admin.html` — rules append + ledger view, depth slider, strategy toggle, window size, time_cost_per_sec
- [ ] S3-08: `ui/dashboard.html` — run history, Resume button, depth tree
- [ ] S3-09: `ui/run.html` — child-run drill-down on `orchestrate` rows
- [ ] S3-10: threads + messages tables; window extractor; thread-scoped runs
- [ ] S3-11: Tests — recursion, resume, rule ledger, multi-turn context

---

## Slice 4: Historical Memory + Rolling Summary

- [ ] S4-01: `src/memory/window.py` — last N turns extraction
- [ ] S4-02: `src/memory/compressor.py` — compress inactive turns into rolling summary via model call (logged) — BRT-MEM-001..005
- [ ] S4-03: `ORCHESTRATOR_SYSTEM_PROMPT` rendered with window + rolling summary
- [ ] S4-04: Tests — 15-turn thread; assert compact context passed to model

---

## Slice 5: Observability + NER + Skills UI

- [ ] S5-01: `run_digests` table; `user_rating` on runs
- [ ] S5-02: digest computation at run completion
- [ ] S5-03: `POST /runs/{id}/rating`
- [ ] S5-04: thumbs up/down in `ui/chat.html`
- [ ] S5-05: `src/logging/ner_worker.py` — async poll, spacy en_core_web_sm, populate log_entities + log_relations — BRT-LOG-010..020
- [ ] S5-06: `src/api/logs.py` — list, entities, export JSONL, cost-summary
- [ ] S5-07: `ui/logs.html`
- [ ] S5-08: `ui/dashboard.html` — Stats tab (cost chart, outliers, feedback)
- [ ] S5-09: `src/api/skills.py` — list/stats/new-version; `ui/skills.html` — per-skill success/residue/cost cards; edit → new version
- [ ] S5-10: Tests

---

## Slice 6: Live UI + Streaming Polish

- [ ] S6-01: `GET /runs/{id}/stream` — SSE emit envelope status changes
- [ ] S6-02: Streaming `/v1/chat/completions` — token-level SSE
- [ ] S6-03: `ui/run.html` — SSE listener; live apply_to_each counter
- [ ] S6-04: `ui/chat.html` — token streaming
- [ ] S6-05: `ui/dashboard.html` — cost cards, token leaderboard, depth tree
- [ ] S6-06: Tests — SSE lifecycle

---

## Error Code Reference

| Module | Prefix | Scope |
|--------|--------|-------|
| Registry | BRT-REG | Discovery, matrix, benchmark, router |
| Envelope | BRT-ENV | Schema validation, answerer contract, governance |
| Orchestrator | BRT-ORC | Engine, DAG, distillation, recursion, fold-in |
| Answerer | BRT-ANS | Leaf execution, contract, self-cleanup |
| apply_to_each | BRT-APE | Batch expansion, concurrency, aggregation |
| state_op | BRT-SOP | Deterministic operators |
| Composer | BRT-CMP | Synthesis, context bucket assembly |
| Skills | BRT-SKL | Skill library load, render, usage tracking |
| Pipeline | BRT-PIP | Run lifecycle orchestration |
| Database | BRT-DB | SQLite operations, resume |
| Logging | BRT-LOG | Log writer, NER worker, residue detection |
| API | BRT-API | FastAPI endpoints + MCP server |
| Memory | BRT-MEM | Window, historical compressor |
| Admin | BRT-ADM | Config, rule ledger |

---

## Progress Tracker

| Slice | Status | Notes |
|-------|--------|-------|
| Slice 0: Scaffolding + prompts + skills + seed matrix | ✅ Complete | Docs, `src/prompts/*`, `data/skills.yaml`, `data/capability_matrix.yaml` |
| Slice 1: Registry Foundation | ✅ Complete | S1-00..S1-10, 53 tests |
| Slice 2: Core Pattern E2E (B + D surfaces) | 🔲 Not started | **Start here** — THE architectural milestone |
| Slice 3: Recursion + Admin + Resume + Multi-turn | 🔲 Not started | |
| Slice 4: Historical Memory | 🔲 Not started | |
| Slice 5: Observability + NER + Skills UI | 🔲 Not started | |
| Slice 6: Live Streaming Polish | 🔲 Not started | |
