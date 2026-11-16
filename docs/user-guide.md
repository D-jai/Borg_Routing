# User Guide — BORG_ROUTE
> Audience: Non-technical end users
> Updated with every feature — see Parallel Commit Law in CODING_STANDARD_CORE.md

---

<!-- Added: 2026-04-17 — Phase 0: Project Setup -->
## Getting Started

BORG_ROUTE is a cost-aware AI orchestration system that routes tasks to the
cheapest capable model. Point any OpenAI-compatible tool at it and it handles
the rest.

---

<!-- Added: 2026-04-18 — S1-00: Project scaffold + health endpoint -->
## Installation

### How to use it
1. Install Python 3.11 or newer from python.org.
2. Clone or unzip the project, then open a terminal in the project folder.
3. Run `python -m pip install -e ".[dev]"` to install all dependencies.
4. Copy `.env.example` to `.env` — fill in any API keys you have (you can add them later via the Keys page in the UI).
5. Download the spaCy language model: `python -m spacy download en_core_web_sm`
6. Start the server: `uvicorn main:app --reload`
7. Open `http://localhost:8000/health` in your browser — you should see `{"status":"ok"}`. The system is running.

### If something goes wrong
- **"Module not found" on startup** — Make sure you ran `pip install -e ".[dev]"` from the project root folder.
- **Port 8000 already in use** — Stop the other process or start BORG_ROUTE on a different port: `uvicorn main:app --reload --port 8001`

---

<!-- Added: 2026-04-18 — S1-09: Models UI + Keys UI -->
## Adding API Keys

To use external AI models (like those on OpenRouter), you need to add an API key.

### How to use it
1. Open `http://localhost:8000/ui/keys.html` in your browser.
2. For OpenRouter: enter your key in the OpenRouter row (get one free at openrouter.ai).
3. Click **Save**. The key is stored in your local `.env` file — it never leaves your machine.
4. Go to the **Models** page and click **Discover Models** to load the new provider's models.

### If something goes wrong
- **"All fields are required"** — fill in the Provider Name, Base URL, and API Key before saving.
- **Key not taking effect** — restart the server after saving a new key.

---

## Discovering Models

BORG_ROUTE finds local models (LM Studio / Ollama) automatically, and pulls
a **curated** list of OpenRouter models from `data/curated_models.yaml` —
by default 12 picks spanning frontier, mid, cheap, and specialty tiers.

### How to use it
1. Start your local model server (LM Studio or Ollama) if you want local models.
2. Open `http://localhost:8000/ui/models.html`.
3. Click **Discover Models**. LM Studio + Ollama are probed live; the 12 OpenRouter rows are populated with pricing + context length.
4. The table shows two sections: **LM Studio (local)** and **OpenRouter (curated)**.

### Editing the OpenRouter pool
Edit `data/curated_models.yaml` (add / remove `- id: <openrouter-id>`
entries) and re-run Discover. A good spread has 1-3 frontier, 2-4 mid,
2-4 cheap / fast, and 1 specialty (e.g. web-search).

### If something goes wrong
- **No local models appear** — LM Studio on ports 1234-1237, or Ollama at `http://localhost:11434`, must be running. Servers on other addresses are set with `LMSTUDIO_BASE_URLS`, `OLLAMA_BASE_URL` and `LOCAL_OPENAI_BASE_URLS` in `.env` (see the install guide).
- **OpenRouter section is empty** — Check that `data/curated_models.yaml` exists and lists at least one `id:` (BRT-REG-032).
- **"Invalid OpenRouter API key"** — Check `OPENROUTER_API_KEY` in `.env`.

---

## Curating Your Model Pool (Checkboxes)

Every row on the Models page has a checkbox. **Only checked models are
available to the orchestrator.** The default is unchecked for every row —
the operator opts models into the pool explicitly.

### How to use it
1. Open `http://localhost:8000/ui/models.html`.
2. Tick the checkbox next to each model you want the orchestrator to be able to pick today. Typical: 3-6 total.
3. Unchecking removes a model from the pool immediately. Scores are preserved — re-ticking restores instantly.
4. State persists in `data/capability_matrix.yaml`; it survives restarts.

### If something goes wrong
- **"BRT-REG-020: no model found"** — No row is checked, or none meets the quality bar. Tick at least one row that has been researched.

### Tuning the quality bar and time-cost knobs (BRT-RT-01)
The router only picks models whose `task_scores` for the current task meet
the **quality bar**, then among those picks the cheapest. Cost combines the
row's pricing with a **time cost per second** so faster models are preferred
when latency matters.

Fresh installs ship with developer-friendly defaults:

| Knob | Default | Where it lives |
|---|---|---|
| `orchestrator_quality_requirement` | `6.5` | `.env` or Admin UI |
| `time_cost_per_sec` | `0.0001` (~$0.36/hr) | `.env` or Admin UI |

Lower the quality bar to let weaker local models orchestrate (useful while
you're still running Get Model Info). Raise it to force frontier-only
orchestration. Lower the time cost toward 0 to strongly prefer local
(free) models; raise it toward `0.0083` ($30/hr) if you're running a SaaS
where every second of latency costs you real money. Both settings can be
changed live from **Admin UI → Settings** — the next run reads the new
value with no restart.

### Where cost appears and how it is computed
Every model call (each step, and the orchestrator's plan, re-plan and compose
calls) is logged with its token counts and its **dollars**:

`dollars = input tokens x the model's input price + output tokens x its output price`

The prices come from the capability-matrix row of the model that served the call.
A model with no pricing (a local model) costs `0.0`, and so does a model the
matrix does not know; the call is never left blank. A call that was retried
after being cut off counts the tokens of both attempts. The tools do not carry
a price today, so tool calls log `0.0`.

- **Run total:** the run digest (`run_digests`) is the sum of the logged calls'
  dollars and tokens for the run and every child run it spawned, so it always
  matches the log. It stores `cost_dollars`, `baseline_dollars`, `tokens_input`
  and `tokens_output`, and `GET /runs/{id}` returns it as `digest`. An
  `orchestrate` or `apply_to_each` step's envelope shows the total of the calls
  it spawned. The Logs page's Cost Summary tab sums `model_call` rows plus the
  `orchestrator_decision` rows that carry tokens or dollars (route decisions
  carry neither). A call that was billed but then failed (a retry after a
  cut-off that errors) is still logged with what was billed.
- **Reply:** the `usage` block of the chat reply (`prompt_tokens`,
  `completion_tokens`, `total_tokens`) is the run's total across every call.
  A direct-to-model request (an exact model id) reports the provider's own
  usage and logs one priced `model_call` under its run id; if the provider
  sends no usage the block is left out. Streamed replies do not report usage
  yet.
- **Baseline (an estimate):** each call also stores `baseline_dollars`, the same
  tokens priced as if the baseline model had done the work; the run digest
  stores the run's total. Both are stored now; the interface receipt that will
  show the comparison is not built yet. It is an estimate: the baseline model
  might have needed more or fewer tokens, so treat the gap as indicative. The
  baseline is the checked model with the highest `reasoning` score (ties go to
  the smaller model id); set `baseline_model_id` (Admin config) or
  `BASELINE_MODEL_ID` in `.env` to pick another.

### How each step gets its model
The planner and the final answer use the orchestrator model (the quality
bar above). The planner is shown only the models that are ticked in the
pool and have an endpoint, all of them, best reasoning score first. Each
line gives the model's three strongest kinds of work with their scores and
its price per million tokens (input and output; a cheap model shows its
real price, such as `$0.001`, never `$0.00`; a model on your own machine
shows no token price). The planner labels each step with its kind of work
(`task_type`) and size (`tier`). Every other step in a plan is routed on
its own:

1. If the planner names a model for the step (`recommended_model`) and
   that model is ticked in the pool, the step uses it.
2. Otherwise the step's **kind of work** (`task_type`: reasoning, coding,
   summarize, writing, roleplay, agentic, extraction) and **size** (`tier`:
   light, mid, heavy) decide. The step goes to the cheapest ticked model
   whose score for that kind of work is at or above the tier's floor. A
   step that invokes a skill takes the skill's own tier and specialty when
   the planner gave none. A step with no labels is `reasoning`, `mid`.
   Near-synonyms are accepted (`creative` is read as writing, `code` as
   coding, `extract` as extraction, `summary` or `summarise` as
   summarize), and each such reading is written into the step's route
   notes; any other unknown word is treated as `reasoning`, also noted.
   If the skill a step names cannot be found, the trace shows a
   `BRT-ORC-023` warning and the step is routed without the skill's hints.
   An `apply_to_each` step's items all run on the step's own model.
3. If no ticked model clears the floor, the step runs on the run's default
   model and the trace shows a `BRT-ORC-019` warning. If routing fails for
   any other reason, the step also runs on the default model and its route
   entry carries the error.

| Tier | Floor (default) | Setting |
|---|---|---|
| light | `6.0` | `LEAF_QUALITY_LIGHT` / config key `leaf_quality_light` |
| mid | `7.5` | `LEAF_QUALITY_MID` / config key `leaf_quality_mid` |
| heavy | `9.0` | `LEAF_QUALITY_HEAVY` / config key `leaf_quality_heavy` |

A config key (set through `POST /config`) wins over the `.env` value and
takes effect on the next run. The run trace records, for every step, why
its model was chosen: a `route` entry with the kind of work, tier, floor,
the chosen model and the other models that cleared the floor, cheapest
first.

---

## Researching Model Capabilities (Get Model Info)

Instead of running live benchmarks, BORG_ROUTE asks a web-search-enabled
LLM to research each model's **published** capabilities — MMLU, HumanEval,
MT-Bench, model cards — and assign relative 0-10 scores across seven task
types (reasoning, coding, summarize, writing, roleplay, agentic,
extraction). This is the "Get Model Info" button.

### How to use it
1. Set `OPENROUTER_API_KEY` in `.env` (the default scoring LLM is `perplexity/sonar-pro-search`, accessed via OpenRouter).
2. Open `http://localhost:8000/ui/models.html`.
3. Click **Get Model Info**. The scorer processes the matrix in batches of 5, with a rolling summary so it can keep relative rankings consistent across all models.
4. Takes 30-90 seconds for a full 19-row matrix and costs roughly $0.05-0.20 per run.
5. Score cells fill in per task type. You can re-run any time — each click re-researches from scratch.

### Overriding the scoring model
Set `SCORING_MODEL` and `SCORING_MODEL_ENDPOINT` in `.env`. The model must
support real web search — Perplexity Sonar, GPT-4o Search Preview, or
Gemini with search grounding. A non-search model will hallucinate scores.

### If something goes wrong
- **BRT-REG-030** — The scoring LLM endpoint is unreachable or the key is wrong. Check `.env`.
- **BRT-REG-031** — The scoring LLM returned unparseable content. Usually means the model does not actually support web search. Switch `SCORING_MODEL`.
- **Scores all come back at 5.0** — You clicked Get Info on rows the LLM could not research (private / custom / brand-new models). Consider hand-editing their `task_scores` in the YAML.

---

<!-- Added: 2026-04-18 — S2-01: Envelope schema -->
## How Tasks Are Packaged (Envelopes)

Every piece of work BORG_ROUTE sends to a model is wrapped in a **WrappedTask** envelope. When the model finishes, it returns an **InboundEnvelope**. You never interact with envelopes directly — they are internal to the system — but understanding them helps when reading run traces.

### What a WrappedTask contains
- **instruction** — the exact task the model must perform
- **governance_instruction** — what the operator originally asked, word for word. Every step, every item of an `apply_to_each` and every call inside a child run shows this in its governance block, so no model is ever told that a step's own text is "the user's instruction" (Law 1). A child run also gets a short "Your Assignment In This Run" block naming its sub-task.
- **output_schema** — the exact shape the model must return (this is the compression contract)
- **type** — one of: `orchestrate`, `answerer`, `apply_to_each`, `state_op`
- **level_path** — where this task sits in the run tree (e.g. `"1a"`, `"2b"`)
- **trigger_reason** — why this envelope was created (e.g. `initial_plan`, `escalation_from:env-id`)

### What an InboundEnvelope contains
- **status** — always `resolved` or `failed`. A model must always commit to an outcome.
- **output** — the model's structured answer, matching the output_schema
- **failure_reason** — if status is `failed`, a short explanation

### If something goes wrong
- **"BRT-ENV-001" in logs** — A model returned `status="unresolved"`. This should never happen; the schema rejects it. Check the model's raw output in the run trace.
- **"BRT-ENV-003" in logs** — A plan exceeded the maximum recursion depth (default: 5). The task may be too complex to decompose at this depth; check the run trace and consider restructuring the request.
- **"BRT-ENV-011" with JSON-Schema-shaped `output_schema`** — Fixed in Phase 2 (BRT-VAL-01). The validator now walks `properties` and honours `required`, so answerer outputs like `{recommendation, reason}` are no longer rejected when the schema is `{"type": "object", "properties": {...}, "required": [...]}`.

---

## Fan-Out Synthesis — Compose Mode vs. Dispatched Answerer (BRT-PR-01a)

When a plan fans out to N parallel answerers, there are two correct ways to synthesise their outputs into a final response:

1. **Compose synthesis (preferred).** Emit only the N fan-out nodes. The runtime returns every resolved envelope's output as `stage_results` when the orchestrator re-enters `compose` mode. The orchestrator writes the final response there — one model call, no extra round trip.

2. **Dispatched synthesis with explicit dependencies.** Emit the N fan-out nodes plus one synthesis node that declares `depends_on: ["n1","n2","n3"]`. The runtime waits for all listed upstream ids to resolve, then injects their outputs into `dependency_input.upstream_results` (and into the `context` string) before dispatch. Use this only when the synthesis step needs a model that differs from the one compose would use.

Writing a synthesis node **without** `depends_on` is an anti-pattern — that node dispatches in parallel with its siblings, has no access to their outputs, and fails with "insufficient context". The orchestrator prompt now teaches this distinction explicitly.

### Observability codes
- **BRT-ORC-017** — A `depends_on` id referenced a node not present in the plan (orchestrator hallucination). The node dispatches anyway with whatever upstream outputs could be resolved; the trace shows the missing ids.

---

## Web Search Tool (BRT-WEB-01)

BORG_ROUTE has one built-in runtime tool: `web_search`. It closes the "scorer has web access, runtime doesn't" asymmetry identified in the Bug Report's deferred-capabilities section.

### How it works
The orchestrator adds a `tool_requests` array to any answerer node in its plan:

```json
{"type":"answerer","id":"n1",
 "instruction":"Summarise the findings.",
 "output_schema":{"summary":"string"},
 "tool_requests":[
   {"tool":"web_search","args":{"query":"GPT-5 release pricing 2026"}}
 ]}
```

The executor runs the search **before** calling the answerer LLM, then injects the normalised `{title, url, snippet}` list into the prompt. Any model — local or remote — sees the results; there is no model-native tool-calling loop.

### Providers and fallback
Tavily is the primary provider (cheap, fast, raw results). Perplexity Sonar (via OpenRouter) is the silent fallback.

- Set `TAVILY_API_KEY` for primary. If absent, Perplexity becomes primary — the first call logs `BRT-TOOL-DEGRADED` once per process and the run continues.
- If both providers fail, the envelope comes back `status=failed` with `failure_reason=BRT-TOOL-002`. The orchestrator's `re_plan` step decides whether to retry with a different query or give up.
- Flip the order with `BORG_WEB_SEARCH_PROVIDER_ORDER=perplexity,tavily` in `.env`.

### Multi-search heuristic
The orchestrator prompt teaches a broad + narrow protocol: for any non-trivial research question, plan **two** independent `web_search` nodes in parallel — one broad (the full question), one narrow (a specific proper noun / angle / time window). Re-enter `re_plan` with both result sets and decide:

- **Enough info** → proceed to compose.
- **Gap detected** → emit 1-2 more searches with refined queries.
- **Contradictions** → emit a verification search.

`max_depth` bounds the total spend.

### Observability
Each tool invocation writes a `model_call` event with `model_id="tool:web_search"` so the run trace can filter tool calls from real model calls. The payload captures the query and the top-k URLs.

### Error codes at a glance
- `BRT-TOOL-000` — plan references a tool not in the registry (caught in `validate_outbound`, before dispatch).
- `BRT-TOOL-001` — tool registered but no provider has credentials.
- `BRT-TOOL-002` — all providers failed at runtime.
- `BRT-TOOL-DEGRADED` — primary provider down, fallback answered (informational, once per process).

---

## Rule Zero Shortcut (BRT-ZERO-01)

For unambiguously conversational short prompts — greetings, factual lookups, small requests, casual chat — BORG_ROUTE now bypasses the plan / re_plan cycle and answers in a single compose call. This saves 2-3 orchestrator calls (~$0.0035) on prompts where decomposition adds no value.

### When the shortcut fires
The pre-classifier is deterministic and rule-based:
- prompt length < 500 characters, AND
- no list markers (`-`, `*`, `•`, `1.`, `1)` on a line start), AND
- none of these phrases: `for each`, `list of`, `classify these`, `analyse these`, `analyze these`, `summarise each`, `summarize each`, `process each`, `compare these`, `compare all`, `for every`.

If all three checks pass, the orchestrator goes straight to compose mode. An `orchestrator_decision` event with `mode=rule_zero_shortcut` is written to the run trace, so you can tell the shortcut fired vs. a normal direct-mode decision.

### Disabling the shortcut
- **Per-install:** set `BORG_RULE_ZERO_SHORTCUT=false` in `.env`.
- **Live, no restart:** `POST /config` with `{"key":"rule_zero_shortcut","value":"false"}`. The config key wins over the env var.

Turn it off when you want to compare costs / latency before and after, or when debugging plan-mode behaviour on short prompts.

---

## Streaming Heartbeats (BRT-STR-01)

`POST /v1/chat/completions` with `stream: true` and `model: borg_route*` now emits SSE keepalive comments (`: heartbeat <ts>`) every `BORG_STREAM_HEARTBEAT_SEC` seconds while the pipeline runs. Previously the stream went silent until `orchestrate_run` finished, which on multi-node plans took long enough that browsers timed the fetch out before any bytes arrived.

Keepalive lines are ignored by SSE consumers but reset their idle timers. The real response is word-streamed after the pipeline resolves.

### Tuning
`BORG_STREAM_HEARTBEAT_SEC` (default `5`). Set to `0` to disable heartbeats entirely. Non-streaming (`stream: false`) requests are unaffected.

---

## Loud Failures (BRT-ORC-02)

Previously, three silent-fallback sites in `orchestrate_run` could make a broken run appear blank (no envelopes, no errors, just `status=failed`). Phase 2 made each visible in the run trace:

- **BRT-ORC-007** (no orchestrator model available) now propagates instead of being swapped for a synthetic empty-endpoint placeholder that crashed deep in `httpx`. Check that at least one model is checked and has a `task_score` above your quality bar.
- **BRT-ORC-015** (warning) — the default answerer selection failed and the orchestrator model is being reused. The run still completes; you'll see the event in the trace.
- **BRT-ORC-016** (error) — envelope save/update failed. Logged and the run continues; check DB health (`data/borg_route.db`).

### When a reply is cut off by the token cap (BRT-ORC-018, BRT-ANS-009)

Every call to a model carries a cap on how many tokens it may write: `ORCHESTRATOR_MAX_TOKENS` for planning and composing, `ANSWERER_MAX_TOKENS` for steps (both default to 2000 when unset). A reasoning-family model gets four times the cap, at least 8000, to leave room for its hidden thinking. A step may ask for a larger cap of its own (`max_output_tokens`, default 4000); the larger of the two is used.

If the model stops because it reached the cap, BORG_ROUTE repeats the call **once** at double the cap, never above 16000. There is no second retry. If the first cap is already 16000 or more (for example a reasoning model with `ANSWERER_MAX_TOKENS=6000`, which gives 24000), no retry is made.

What you see:

- **Planner or compose call retried:** the run carries on. The trace's `orchestrator_decision` event for that call shows `"length_retry": true` and `"code": "BRT-ORC-018"`, and its token counts are the two attempts added together.
- **A step still cut off:** the step fails with **BRT-ANS-009**. The message says either "even after one retry at double the cap (A -> B)" or "no retry", so you know which happened. Raise `ANSWERER_MAX_TOKENS` in `.env` or split the request into smaller pieces.

---

<!-- Added: 2026-04-18 — Slice 3: Recursion + Admin + Resume + Threads -->
## Recursion and Depth Control

BORG_ROUTE can orchestrate sub-problems by spawning **child runs**. Each child run has its own run ID, linked to its parent via `parent_run_id`. The depth of nesting is controlled by **Max Recursion Depth** (default: 5).

### What happens at the depth cap
When an orchestrate node is dispatched at the maximum depth, BORG_ROUTE **demotes it to an answerer** — the model responds directly rather than trying to recurse further. This is logged as a `BRT-ORC-020` event in the run trace.

### How to adjust the depth limit
1. Open `http://localhost:8000/ui/admin.html`.
2. In the **Settings** section, adjust **Max Recursion Depth** (1–10).
3. Click **Save Settings**.

---

## Admin Panel

The Admin panel at `http://localhost:8000/ui/admin.html` lets you configure BORG_ROUTE's behaviour without restarting the server.

### Global Rules
Text entered here is injected verbatim into every model call's governance layer. Use it to add blanket constraints — for example, "Always respond in English" or "Never reveal pricing information."

### Admin Rules Ledger
Additive rules that accumulate over time. New rules are appended; old rules can be retired (they are excluded from future model calls but kept in the ledger for audit purposes).

### Settings
- **Max Recursion Depth** — Maximum levels of nested orchestration (1–10, default: 5).
- **Default Strategy** — `SYSTEM_CHOOSE` lets BORG_ROUTE pick the best available model; `LOCAL_FIRST` prefers local models and only falls back to external if no local model qualifies.
- **Context Window Size** — How many recent conversation turns to include in the orchestrator's context (default: 10).
- **Time Cost Per Second** — Dollar value assigned to one second of local-model compute time, used in cost estimation (default: $0.0083 ≈ $30/hr).

---

## Resuming a Failed Run

If a run fails partway through (network error, model timeout, etc.), you can resume it without re-running the work that already completed.

### How to resume
1. Open `http://localhost:8000/ui/dashboard.html`.
2. Find the failed run in the run history table.
3. Click **Resume** — only failed or in-progress runs show this button.
4. The system re-dispatches only the failed/in-flight envelopes. Completed work is not repeated.

### Via API
```
POST /runs/{run_id}/resume
```
Returns 202 immediately. Poll `GET /runs/{run_id}` for updated status.

The re-run works from the run's own instruction, the words you first sent,
and every step it makes is governed by those words (Law 1), never by the
text of a step saved in the failed run. Resuming a child run keeps its own
sub-task and is governed by the top-level run's instruction.

### If something goes wrong
- **"BRT-DB-020"** — The run is already completed and cannot be resumed.
- **"no_pending_work"** — All envelopes already succeeded; nothing to retry.

---

## Reading why each step used its model

`GET /runs/{run_id}` returns `run`, `envelopes`, `child_envelopes`, `events` and `digest`. `child_envelopes` holds the envelopes (with their `routing`) of every child run the run started, each tagged with its own `run_id`. Each envelope (one per step) now carries:

- `routing`: why the step went to its model. It holds `chosen`, `reason` (`routed`, `planner_pick` or `fallback_default`), `task_type`, `tier`, `floor`, `est_input_tokens`, `est_output_tokens`, `candidates` (the models that cleared the floor, cheapest first, at most 8, each with a score, `est_cost` and `est_dollars`), `baseline` (the model the run is compared against: the best-reasoning checked model, or the one set by the `baseline_model_id` config key / `BASELINE_MODEL_ID`) and `baseline_est_cost`. If a configured baseline is not in the matrix, `baseline_note` says which model was used instead.
  - Every figure under `routing` is an estimate, never a measured cost. The token counts are guessed from the step's text length, and `dollars` on the envelope is the only real cost.
  - `est_dollars` is the chosen model's estimated dollars only (0.0 for a local model). `baseline_est_cost` is the same estimate at the baseline model's prices, so the two compare like for like. `est_cost` is what the router ranks by: dollars plus the cost of waiting (`TIME_COST_PER_SEC`), so it is not comparable with `baseline_est_cost`.
  - A step whose routing failed (`fallback_default`) still carries the token estimate and `est_dollars`; if no estimate could be made they are `null`, never 0.0. A `planner_pick` also lists `candidates`, for information; the pick itself is unchanged. When no model cleared the floor, `candidates` is empty and `candidates_reason` says so.
  - For an `apply_to_each` step the estimates cover every item: `items` is the count, `per_item_est_dollars` and `per_item_baseline_est_cost` are one call, and `est_dollars` and `baseline_est_cost` are the totals.
  - For an `orchestrate` step `served_by` is `child_run`: a child run plans the step and picks its own models, so `chosen` is only the fallback if the step is demoted at max depth (`note` says so), `child_run_id` names the child run, and `est_dollars` and `baseline_est_cost` are `null`.
- `dollars`: what the step's call actually cost.
- `latency_ms`: how long the step took, in milliseconds.

---

## Multi-turn Conversations (Threads)

BORG_ROUTE tracks conversation history in **threads**. Each thread stores the sequence of user and assistant messages. When a new run is created within a thread, the last N messages are passed to the orchestrator as context, enabling coherent multi-turn conversations.

### How threads work
1. A thread is created automatically when you start a conversation via the Chat UI.
2. Each user message and assistant response is stored in the thread.
3. The orchestrator receives the last **Context Window Size** turns as `thread_window`.
4. Run IDs are linked to their thread via the `thread_id` field on the run record.

---

## Feedback — Rating Runs

After each assistant reply in the Chat UI, thumbs-up (👍) and thumbs-down (👎) buttons appear. Click either to submit a rating. Ratings are stored against the run and visible in the Dashboard Stats tab.

You can also rate a run via the API: `POST /runs/{run_id}/rating` with body `{"rating": 1}` (up) or `{"rating": -1}` (down).

---

## Observability Logs

The Logs page at `http://localhost:8000/ui/logs.html` provides three tabs:

- **Events** — all model_call, orchestrator_decision, child_residue, and error events with filtering by run ID and event type
- **Cost Summary** — aggregate token counts and dollar cost grouped by model (`model_call` plus priced `orchestrator_decision` rows)
- **NER Entities** — named entities extracted from log payloads by the background NER worker

Click **Export JSONL** to download a full log snapshot for offline analysis.

### NER Worker (Optional, Currently Parked)

**Status:** Shipped and tested, but **not auto-started**. Treat this as a
dormant feature you can turn on later if you want richer log analytics. Day-to-day
operation of BORG_ROUTE does not require it.

#### What NER is for
NER (Named Entity Recognition) reads every text payload the system logs —
prompts, model outputs, orchestrator decisions, error messages — and pulls out
structured facts:

- **Named entities** — people, organisations, places, dates, products, money
  amounts (`PERSON`, `ORG`, `GPE`, `DATE`, `MONEY`, etc., via spaCy's
  `en_core_web_sm` model).
- **Subject-verb-object relations** — simple `(subject, verb, object)` triples
  extracted from sentences via dependency parsing.

Results land in the `log_entities` and `log_relations` tables and are surfaced
on the **NER Entities** tab of `/ui/logs.html`.

#### What it was meant to be useful for
1. **Search across runs** — "show me every run that mentioned customer X" or
   "every model call that produced a dollar amount."
2. **Audit and compliance** — quickly spot PII, company names, or sensitive
   terms leaking into prompts or outputs.
3. **Skill-improvement signal** — see which entity types a skill reliably
   handles vs. drops, feeding the Skills Library continuous-improvement loop.
4. **Offline exploration** — export JSONL from the Logs page and join
   entities to runs for ad-hoc analysis.

#### How to turn it on (when you decide you want it)
1. Download the spaCy model once:
   ```
   python -m spacy download en_core_web_sm
   ```
2. Start the worker alongside the FastAPI server — either inline in
   `main.py` startup, or as a separate process:
   ```python
   # Option A: inline in main.py @app.on_event("startup")
   import asyncio
   from src.logging.ner_worker import run_ner_worker
   asyncio.create_task(run_ner_worker())
   ```
   ```python
   # Option B: standalone script (run_ner.py)
   import asyncio
   from src.logging.ner_worker import run_ner_worker
   asyncio.run(run_ner_worker())
   ```
3. Open `/ui/logs.html` → **NER Entities** tab. Entities for new payloads
   should appear within ~5 seconds (the worker's poll interval).

#### How it behaves
- Polls `log_payloads` every 5 seconds, processes batches of 20.
- Caps input at 10 000 characters per payload to stay memory-safe.
- Sets `ner_processed=1` on each row so work is never repeated.
- Failures on a single payload are logged and skipped — the worker never crashes.

#### Error codes to know
- **BRT-LOG-010** — spaCy model not available. Run the `spacy download`
  command above.
- **BRT-LOG-011** — NER failed on a specific payload (malformed text, etc.).
  The row is marked processed and skipped.
- **BRT-LOG-012** — DB write failure after extraction.
- **BRT-LOG-013** — worker poll error; worker continues after logging.

#### Why it is parked by default
The worker loads a ~50 MB spaCy model on first use and adds a steady poll
to the SQLite database. For small deployments that is wasted work. The code
is kept ready so you can enable it the moment entity-level log analytics
become worth the cost — nothing else in the pipeline depends on it.

---

## Skills Library

The Skills page at `http://localhost:8000/ui/skills.html` shows all loaded skills with:
- Call count, success rate, residue count, average tokens and latency
- An **Edit → New Version** button that opens a form to create an improved version of any skill

New versions are appended to `data/skills.yaml` with an incremented version number. The orchestrator will use the highest version by default.

You can also manage skills via the API:
- `GET /skills` — list all skills with stats
- `GET /skills/{skill_id}` — detail for one skill including all versions
- `POST /skills/{skill_id}/version` — create new version (JSON body with `description`, `when_to_use`, `not_when`, `recommended_tier`)

---

## Dashboard Stats Tab

The Dashboard at `http://localhost:8000/ui/dashboard.html` now has two tabs:

- **Runs** — full run history table with trace and resume buttons
- **Stats** — run outcome counts, cost breakdown by model, feedback summary (thumbs up/down), and top outlier runs

---

## Historical Memory (Rolling Summary)

For long conversations BORG_ROUTE compresses older turns into a rolling summary so the orchestrator always has useful context without overloading its window.

**How it works:**
1. After a thread grows beyond a threshold, call `compress_inactive_turns(thread_id, model_endpoint, model_name, keep_last_n=5)` from `src/memory/compressor.py`.
2. The compressor sends all turns older than the last `keep_last_n` to the model, which writes a concise summary (3-8 sentences).
3. The summary is stored in the config table under `rolling_summary:{thread_id}`.
4. On the next `run_pipeline()` call with that `thread_id`, the summary is auto-loaded and passed to the orchestrator as `{rolling_summary}` in its system prompt.

**Triggering compression manually (Python):**
```python
from src.memory.compressor import compress_inactive_turns
summary = await compress_inactive_turns(
    thread_id="<your-thread-id>",
    model_endpoint="http://localhost:1234",
    model_name="your-model-name",
    keep_last_n=5,
)
```

**Error codes:** BRT-MEM-003 (model call failed), BRT-MEM-004 (empty/bad response), BRT-MEM-005 (config write failed).

---

## Dashboard

The Dashboard at `http://localhost:8000/ui/dashboard.html` shows all runs with their status, parent link, and creation time. Click **Trace** to open the full run trace for any run.

The **Stats** tab now shows:
- **Cost Cards** — total cost, total tokens, number of models used, and total calls at a glance.
- **Token Leaderboard** — horizontal bar chart showing token consumption per model (top 8).
- **Depth Tree** — visual tree of runs and their child runs, so you can see recursive orchestration at a glance.
- **Outlier Runs** — the five most complex runs by instruction length.

---

<!-- Added: 2026-04-18 — S6-01..S6-04: Live streaming -->
## Live Run Trace

When you open a run trace (`/ui/run.html?id=<run_id>`) while a run is still in progress, the page automatically connects to the live event stream. You will see:

- A **green pulsing dot** labelled "Live" in the top bar while the stream is active.
- Envelope rows appearing and updating in real time as the orchestrator dispatches tasks.
- An **apply_to_each counter** showing how many parallel subtasks have been launched.
- The dot changes to "Completed" or "Failed" when the run finishes and the stream closes.

No page refresh is needed — everything updates automatically.

## Streaming Chat

The Chat page (`/ui/chat.html`) now streams tokens progressively as the model generates them. Text appears word by word instead of waiting for the full response. The **View run trace** link still appears after each reply, and the rating buttons (👍/👎) work as before.
