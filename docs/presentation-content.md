# Presentation Content — BORG_ROUTE
> Audience: Clients, recruiters, investors
> Updated with every feature — see Parallel Commit Law in CODING_STANDARD_CORE.md

---

<!-- Added: 2026-04-17 — Phase 0 -->
## Project Overview

**What it is:** BORG_ROUTE is an Adaptive Recursive Agent Router — a local-first
AI middleware that decomposes tasks and routes each piece to the cheapest model
capable of handling it.

**Problem it solves:** Most AI setups send every request to the biggest, most
expensive model available. BORG_ROUTE makes cost-aware decomposition a
first-class architecture — not a hack.

**Key points:**
- Drop-in OpenAI-compatible API: point Cursor, Continue, or any AI tool at
  `http://localhost:8000/v1` and specify `model: "borg_route"`
- Orchestrator acts as a project manager: it structures work, then routes each
  atomic step to the cheapest capable model
- Full observability: every model call logged with token counts, cost, and
  decision trace

**Portfolio/LinkedIn angle:** Demonstrates production-level AI architecture
(multi-agent orchestration, cost modelling, local + cloud hybrid deployment)

---

<!-- Added: 2026-04-18 — S1-02: ModelInfo Pydantic model -->
## Model Registry — ModelInfo

**What it is:** A validated Pydantic model that represents a single AI model
in the capability matrix — local or external, with pricing, throughput, and
per-task quality scores.

**Why it matters:** Every routing and cost decision flows through this object.
Strong validation (BRT-REG-001/002) means bad config is caught at load time,
not at inference time.

**Key points:**
- Supports four sources: LM Studio, Ollama, OpenRouter, custom API keys
- Optional `Pricing` sub-model with non-negativity enforcement
- Per-task `task_scores` enable specialty-primary routing

**Demo talking point:** "The router never sees a raw dict — every model is a
validated object. Config errors surface immediately, not mid-run."

**Portfolio/LinkedIn angle:** Pydantic v2 schema design; typed domain models.

---

<!-- Added: 2026-04-18 — S2-01: Envelope schema -->
## Envelope Contract — WrappedTask + InboundEnvelope

**What it is:** The typed contract between the orchestrator and every model it dispatches to. `WrappedTask` is the outbound brief; `InboundEnvelope` is the structured result.

**Why it matters:** The output_schema field on every WrappedTask is a declarative compression specification — the model must fit its entire answer into the declared shape. This is the Wrapper-as-Compression Contract pattern that makes progressive distillation work without a separate summarisation step.

**Key points:**
- `status: Literal["resolved", "failed"]` — models always commit to an outcome; `"unresolved"` is a schema error (BRT-ENV-001)
- `level_path` (e.g. `"4d"`) encodes position in the run tree for fold-in cleanup and observability
- `trigger_reason` turns the envelope table into a queryable causal graph with a single WHERE clause
- Pydantic v2 enforces all invariants at construction time — bad envelopes never reach the model layer

**Demo talking point:** "Every model call is a typed contract. The schema is both the task spec and the compression spec. There is no separate summarisation step."

**Portfolio/LinkedIn angle:** Pydantic v2 advanced validation patterns; domain-driven schema design; typed inter-agent protocol.

---

<!-- Added: 2026-04-18 — S2-02: Envelope validator -->
## Envelope Contract Enforcement — validate_outbound + validate_inbound

**What it is:** A two-function contract layer that validates envelope correctness at every dispatch and fold-in boundary.

**Why it matters:** `validate_inbound` is the runtime guard against misbehaving models — it checks that the output matches the declared schema and that every failed envelope carries an explanation. These are the exact conditions the fold-in cleanup law depends on.

**Key points:**
- BRT-ENV-011: output schema mismatch caught at fold-in, before the parent agent consumes bad data
- BRT-ENV-012: a failed envelope without a reason is itself a contract violation — logged as diagnostic signal
- Validators re-check at call-site even if Pydantic already validated at construction (defense in depth against mutation)

**Demo talking point:** "Every inter-agent handoff is validated twice — at construction and at receipt. A model that returns the wrong shape is caught immediately, not silently propagated."

---

<!-- Added: 2026-04-18 — S1-00: Project scaffold + health endpoint -->
## FastAPI Scaffold + Health Endpoint

**What it is:** The foundational Python project setup — FastAPI app, dependency
management via `pyproject.toml`, environment variable loading, and a `/health`
endpoint.

**Why it matters:** Establishes the runnable skeleton that every subsequent
feature builds on. The health endpoint is the first integration surface any
operator or CI pipeline can check.

**Key points:**
- Single command install: `pip install -e ".[dev]"`
- Zero-config startup: `uvicorn main:app --reload`
- `GET /health → {"status":"ok"}` — always available to prove the server is up

**Demo talking point:** "The entire stack installs in one command, starts in
one command, and proves itself alive at `/health`."

**Portfolio/LinkedIn angle:** Clean Python project setup with modern tooling
(pyproject.toml, asyncio FastAPI, pytest-asyncio).
