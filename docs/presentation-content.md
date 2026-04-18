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
