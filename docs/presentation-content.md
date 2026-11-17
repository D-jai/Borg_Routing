# Presentation Content — BORG_ROUTE
> Audience: Clients, recruiters, investors
> Updated with every feature — see the commit law in CONTRIBUTING.md

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

<!-- Added: 2026-04-18 — S2-03: Database schema -->
## SQLite Schema — 8 Tables, All Constraints Enforced

**What it is:** The full SQLite schema for Slice 2 — runs, subtasks, envelopes, scratch, logs, log_payloads, skill_usage_log, config.

**Why it matters:** Every table has CHECK constraints and indexes. The `scratch` table with its `(run_id, level_path)` index is the foundation for the fold-in cleanup law — the parent can check for child residue with a single indexed query.

**Key points:**
- `trigger_reason` on envelopes turns the envelope table into a queryable causal graph
- `child_residue` as a `logs.event_type` gives free observability on misbehaving agents
- `run_digests` table (Slice 5) will compute cost/latency summaries without any AI calls
- WAL journal mode for safe concurrent reads during live runs

**Demo talking point:** "Residue detection is just `SELECT COUNT(*) WHERE level_path = {child_level}`. The cleanup law costs one indexed query per fold-in."

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

---

<!-- Added: 2026-04-18 — Slice 3: Recursion + Admin + Resume + Threads -->
## Slice 3: Recursive Orchestration + Admin + Resume

**What it is:** The system can now orchestrate sub-problems by spawning child runs
(with linked `parent_run_id`), enforce a configurable recursion depth ceiling (demoting
to answerer at the cap), resume failed/in-flight envelopes without repeating completed
work, manage governance rules via an Admin UI, and track multi-turn context via threads.

**Why it matters:** This makes BORG_ROUTE a production-grade orchestrator — it handles
arbitrarily complex task hierarchies, recovers gracefully from partial failures, and
gives operators real-time control over model behaviour without restarting the server.

**Key points:**
- **Recursive orchestration:** `type=orchestrate` nodes spawn child runs with `parent_run_id` linkage, visible in the run trace UI with drill-down navigation
- **Depth cap + demotion:** `MAX_RECURSION_DEPTH=5` (configurable); at the cap the node is demoted to an answerer rather than crashing the run
- **Resume:** `POST /runs/{id}/resume` re-dispatches only the failed/in-flight envelopes — completed work is preserved
- **Admin Rules Ledger:** operators append additive governance rules at runtime; rules cascade into every model call (Law 1 enforcement); retired rules are excluded but kept in the ledger
- **Threads:** `threads` + `messages` tables store conversation history; `extract_window()` provides the last N turns for the orchestrator's context

**Demo talking point:** "A complex task spawns sub-tasks, each with their own run ID linked to the parent. If a sub-task fails, one API call resumes only that work — nothing else is re-run. The admin can add a new governance rule mid-session and it applies to the very next model call."

**Portfolio/LinkedIn angle:** Production-quality recursive agent architecture with fault tolerance, live governance, and full audit trail.

---

### Slice 4 — Historical Memory + Rolling Summary

**Key points:**
- **Progressive Distillation in practice:** `compress_inactive_turns()` calls the model once to compress all turns older than the last N into a 3-8 sentence rolling summary — the orchestrator never sees raw history beyond its window
- **Zero-friction wiring:** `run_pipeline()` auto-loads `rolling_summary:{thread_id}` from the config table when a `thread_id` is provided — callers need no extra code
- **Orchestrator prompt already wired:** `{rolling_summary}` placeholder in the system prompt receives the stored summary on every run, keeping long conversations coherent without token bloat
- **Full audit trail:** every compression call is logged as a `model_call` event (tokens, latency, payload) in the logs table

---

### Slice 5 — Observability + NER + Skills UI

**Key points:**
- **Run digests:** every completed run auto-generates a cost/token summary stored in `run_digests` — queryable and displayed in the Stats tab
- **User feedback loop:** thumbs up/down in the Chat UI writes `user_rating` to the run; the Stats tab shows the feedback ratio — closing the loop between user satisfaction and run data
- **NER pipeline:** a background async worker runs spaCy `en_core_web_sm` over all model call payloads, extracting named entities (PERSON, ORG, GPE) and SVO relations into `log_entities` / `log_relations` — zero-cost structured knowledge from existing traffic
- **Full observability surface:** `/ui/logs.html` — event list, cost summary by model, NER entity browser, JSONL export
- **Skills governance:** `/ui/skills.html` shows per-skill success rate, residue count, and cost; one click creates a new version that is live on the next orchestrator call

**Demo talking point:** "Every model call is logged with tokens and cost. The background NER worker extracts entities from those payloads automatically — no extra code needed. The Stats tab shows which models cost the most and how users rated the results."

**Portfolio/LinkedIn angle:** End-to-end observability stack built from scratch — cost accounting, NLP entity extraction, user feedback — all integrated into a coherent operational dashboard.

---

**Demo talking point:** "After 15 turns the system automatically compresses the first 10 into a paragraph summary. The orchestrator reads that summary and the last 5 turns — same reasoning quality, a fraction of the tokens."

**Portfolio/LinkedIn angle:** Demonstrates Progressive Distillation and Cascading Distillation patterns from first principles — memory management built as a first-class architectural concern, not bolted on.

---

<!-- Added: 2026-04-18 — Slice 6: Live UI + Streaming Polish -->
## Slice 6 — Live UI + Streaming Polish

**What it adds:**
- **Server-Sent Events run stream:** `GET /runs/{id}/stream` emits real-time envelope status changes as the orchestrator works. Clients get a live feed; no polling needed.
- **Token-level streaming chat:** `/v1/chat/completions` with `stream=true` now relays real SSE chunks from bypass model endpoints and word-streams orchestrated responses — OpenAI wire-compatible.
- **Live run trace UI:** `ui/run.html` auto-connects to the SSE stream — envelopes appear as dispatched, apply_to_each counter ticks up in real time, status dot turns green.
- **Streaming chat UI:** `ui/chat.html` displays tokens progressively as they arrive.
- **Enhanced dashboard:** cost summary cards, token leaderboard (horizontal bars per model), and a depth tree showing recursive parent→child run relationships.

**Demo talking point:** "Open the run trace and watch the orchestrator work in real time — each envelope appears as it's dispatched, the apply_to_each counter ticks up as subtasks fan out, and the status dot turns red or green the moment the run completes."

**Portfolio/LinkedIn angle:** Production-grade streaming architecture — SSE pub/sub from the DB layer through the API to the browser, all without WebSockets or external message queues.

---

<!-- Added: 2026-04-23 - Phase 1: Fresh-install-works-out-of-the-box -->
## Phase 1 — Fresh install works out of the box

**What it adds:**
- **BRT-QA-01:** Slice-5 NER tests now skip cleanly when the optional spaCy language model (en_core_web_sm) is not installed, so `python -m pytest` on a fresh checkout reports the advertised test count — no phantom failures from a module the operator never enabled.
- **BRT-OR-01:** OpenRouter rows are first-class. A new `resolve_model_call` helper injects the correct URL, bearer auth, and attribution headers, and strips the `openrouter/` prefix from the outbound model id. Every call now sends `max_tokens` so OpenRouter stops rejecting with HTTP 402 "tokens=0". `POST /keys` with name="OpenRouter" writes the canonical `OPENROUTER_API_KEY=` env var so discovery, scorer, and chat completions all share one secret.
- **BRT-DS-01:** Discovery is the single source of truth for local rows. Hand-seeded LM Studio entries were removed from `data/capability_matrix.yaml`; instead, a reconciliation pass at the end of `discover_all` marks any local row that was not seen live as `endpoint=None, checked=False`, killing ghost rows that would otherwise silently route to a server that is no longer running.
- **BRT-RT-01:** Local-developer-friendly routing defaults. `time_cost_per_sec` drops from 0.0083 to 0.0001 and `orchestrator_quality_requirement` drops from 8.0 to 6.5, so a mid-tier local model (e.g. `lmstudio/google/gemma-4-26b-a4b` at reasoning=6.8) can orchestrate without a frontier API key. The Admin UI time-cost knob now actually feeds the router via DB config — no restart needed.

**Demo talking point:** Clone the repo, set `OPENROUTER_API_KEY`, run `pip install -e .` and `uvicorn main:app`, open the chat UI, and ask "hi" — you get a real answer, without editing a YAML, tweaking a quality bar, or learning a config key.

**Portfolio/LinkedIn angle:** Shows that getting the boring parts right - config, discovery, HTTP auth, defaults - is where open-source tools actually succeed or fail. Phase 1 is the turning point from works-for-the-author to works-for-everyone.

---

<!-- Added: 2026-04-23 - Phase 2: Real orchestrations succeed, not just "hi" -->
## Phase 2 — Real orchestrations succeed, not just "hi"

**What it adds:**
- **BRT-VAL-01:** The inbound envelope validator now understands JSON-Schema-shaped `output_schema` (`{"type": "object", "properties": {...}, "required": [...]}`), walking `properties` and honouring `required` instead of treating the meta-keys as required field names. Correct answerer outputs like `{recommendation, reason}` are no longer silently thrown away with "missing keys `['type', 'properties', 'required']`".
- **BRT-ORC-02:** Three silent-fallback sites in `orchestrate_run` now emit visible events instead of masking broken runs. `BRT-ORC-007` (no orchestrator model) propagates — no more empty-endpoint `httpx.UnsupportedProtocol` with a blank run trace. `BRT-ORC-015` logs default-answerer fallbacks. `BRT-ORC-016` logs envelope-persistence errors. The Run Trace page now shows what went wrong, not just that something did.
- **BRT-APE-01:** `apply_to_each` finally resolves `inputs_ref` as a prior-node-id string — the runtime walks the upstream envelope's output for an `items`/`results` list and stamps it into `dependency_input['items']` before dispatch. Empty item lists surface as `BRT-APE-005` (a visible failed envelope) instead of resolving as a phantom 0-result success that left the orchestrator re-planning against nothing.
- **BRT-PR-01a:** Two architectural additions. (1) The orchestrator system prompt now teaches when to synthesise in compose mode (using `stage_results`) versus when to dispatch an answerer with explicit `depends_on` — the anti-pattern of "unwired final recommendation node" is called out by name. (2) The engine implements answerer-to-answerer dependencies: when a node declares `depends_on: ["n1"]`, the runtime waits for `n1` to resolve, then injects its output into the child's `dependency_input.upstream_results` and into the `context` string. Hallucinated upstream ids are ignored by the ready-check and logged as `BRT-ORC-017`.

**Demo talking point:** Ask BORG "compare these three rate-limiting stacks and recommend one". Before Phase 2, the 4-node plan dispatched all four in parallel, the recommendation node had no access to the analyses, and the run collapsed silently. Now the recommendation node waits for its dependencies, sees their outputs, and writes a coherent answer — or the orchestrator synthesises in compose mode using `stage_results` directly.

**Portfolio/LinkedIn angle:** Phase 2 is the fine-grained contract work — schema validation, dependency wiring, visible failures. Not flashy, but it is where most LLM-orchestration frameworks quietly rot. Making failures visible is worth more than adding features.

---

<!-- Added: 2026-04-23 - Phase 3: Operator experience - no timeouts, no phantom overhead, no false expectations -->
## Phase 3 — Operator experience

**What it adds:**
- **BRT-STR-01:** The streaming endpoint now emits SSE keepalive comments every 5 seconds while `orchestrate_run` is still running, so browsers no longer time out the fetch on multi-node plans that take 60-90 seconds. The heartbeat interval is configurable via `BORG_STREAM_HEARTBEAT_SEC`. Non-streaming requests are unchanged; the fix is a wrap around the existing pipeline, not a rewrite of the orchestrator as a generator.
- **BRT-ZERO-01:** Trivial conversational prompts — "hi", "what is photosynthesis?", short Q&A — now skip plan and re_plan, going straight to a single compose call. The pre-classifier is deterministic (length < 500 chars, no list markers, no bulk phrases), costs nothing, and is toggleable via `BORG_RULE_ZERO_SHORTCUT` / the `rule_zero_shortcut` config key. ~$0.0035 and 2-3 model calls saved per trivial prompt.
- **BRT-USAGE-01:** A new "When BORG_ROUTE pays off" section in the install guide: measured cost table (deepseek direct vs. BORG orchestrated — real numbers showing ~21× ratio on a small prompt), break-even formula for bulk workloads (N ≈ 130 items), three workload examples where BORG wins, three pool recipes. Honest framing about what BORG is and isn't good at, so new operators stop judging it on a single short prompt.

**Demo talking point:** On `stream: true`, ask a long prompt that triggers a 4-node plan. Watch the browser keep the connection alive via heartbeats until the real answer word-streams in. Then ask "hi" — one orchestrator call, sub-second response, BRT-ZERO-01 event in the trace.

**Portfolio/LinkedIn angle:** Phase 3 is about expectation-setting and user-facing polish. The heartbeat fix is twelve lines of code; the Rule Zero shortcut is a regex and two phrase lists. Both change the first-minute experience of BORG_ROUTE from "broken" to "obvious". The docs section is the honest-marketing counterpart — admitting where BORG costs more so the operator trusts you when you claim it saves money at scale.

---

<!-- Added: 2026-04-23 - Phase 4: Runtime tooling + adaptive research -->
## Phase 4 — Runtime tooling

**What it adds:**
- **BRT-WEB-01:** BORG_ROUTE gains its first runtime tool — `web_search` — powered by Tavily primary + Perplexity fallback. The design choice ("Option B+") is the quietly-clever part: the executor pre-fetches search results **before** the LLM call and injects them into the prompt, so every answerer model gets web access without anyone having to implement model-native tool-calling. Local gemma gets search. Claude gets search. Any OpenAI-compatible model gets search. One HTTP call per envelope, bounded cost, no tool-call loops.
- **Multi-search heuristic (orchestrator prompt):** for any research question, the orchestrator emits two parallel `web_search` nodes — one broad, one narrow — then uses its existing `re_plan` loop to decide whether to compose, refine with more searches, or verify contradictions. `max_depth` bounds the total search spend deterministically. The whole adaptive-tool-use pattern is built entirely out of primitives we already had (fan-out, re_plan, depends_on) — no new orchestration concepts needed.
- **Tool dispatcher foundation:** a new `src/tools/` package (BaseTool, ToolRequest, ToolInvocationResult, run_tool_requests) sets up the home for inbox and outbox tools in Phase 5. The envelope protocol grows one field, `tool_requests: list[ToolRequest]`, and the executor resolves it before dispatch.
- **Provider failover:** Tavily down → silent fallback to Perplexity (one `BRT-TOOL-DEGRADED` event in the trace, run continues). Both down → `BRT-TOOL-002` envelope failure, orchestrator re-plans.
- **Cross-cutting observability:** every tool call gets a `model_call` trace event with `model_id="tool:web_search"`, so the Live Trace UI shows tool calls alongside real model calls, same schema, no magic.

**Demo talking point:** ask "what was the highest-grossing movie last month?" on a pure-local-model pool. Before Phase 4: hallucinated answer. After Phase 4: two parallel Tavily searches resolve in ~1s, gemma-26b composes a grounded answer with URLs. Same model. New tool. Zero provider lock-in.

**Portfolio/LinkedIn angle:** Phase 4 closes an architectural gap that most agent frameworks solve by requiring a frontier model with native tool-calling. BORG_ROUTE solves it by doing the tool work in the executor — pre-fetched, deterministic, legible, bounded. This is the kind of design decision that separates production-grade orchestration from demo-ware: you keep the hard-won invariants (one HTTP call per envelope, per-envelope token accounting, local models first-class) and only grow the envelope protocol by one field.

