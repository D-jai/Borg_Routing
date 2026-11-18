# User Guide — BORG_ROUTE
> Audience: Non-technical end users
> Updated with every feature — see the commit law in CONTRIBUTING.md

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
4. Go to the **Models** page and click **Discover** to load the models and their prices.

### If something goes wrong
- **The page says "In .env, restart needed"** — the key is written in `.env` but the running server does not have it yet. Restart the server.
- Only the OpenRouter key is saved from this page. The Tavily key (web search) is set by adding `TAVILY_API_KEY` to `.env` and restarting.

---

## Discovering Models

BORG_ROUTE finds local models (LM Studio / Ollama) automatically, and pulls
a **curated** list of OpenRouter models from `data/curated_models.yaml` —
by default 12 picks spanning frontier, mid, cheap, and specialty tiers.

### How to use it
1. Start your local model server (LM Studio or Ollama) if you want local models.
2. Open `http://localhost:8000/ui/models.html`.
3. Click **Discover**. LM Studio and Ollama are probed live; the 12 OpenRouter rows are populated with pricing + context length.
4. The table lists every model with its tick box and scores, and marks each as running on this machine or in the cloud.

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
5. Discover Models, Get Model Info and the checkboxes all change the one matrix the router uses, so the next run sees them with no restart.

### If something goes wrong
- **"BRT-ORC-007: no orchestrator model available"** — No row is checked, or none can plan the run. Tick at least one row that has been researched. (`BRT-REG-020` reaches you only when a `compare_and_rank` or `verify_claim` step has more data than any ticked model can hold.)

### Tuning the quality bar and time-cost knobs (BRT-RT-01)
The router only picks models whose `task_scores` for the current task meet
the **quality bar**, then among those picks the one with the lowest **effective
cost**: the row's estimated dollars plus a **time cost per second** for the
waiting, so faster models are preferred when latency matters. (A chat alias
such as `borg_route:local` can change the order; the step's route notes say so.)

Fresh installs ship with these defaults (the quality bar of 9.0 means a frontier-class model plans each run; if no ticked model reaches it, the planner retries at a bar of 5.1 and picks the cheapest ticked model, by effective cost, that scores at least 5.1; the trace shows `BRT-ORC-NO-FRONTIER`):

| Knob | Default | Where it lives |
|---|---|---|
| `orchestrator_quality_requirement` | `9.0` | `.env` or Admin UI |
| `time_cost_per_sec` | `0.0001` (~$0.36/hr) | `.env` or Admin UI |

Lower the quality bar to let weaker local models orchestrate (useful while
you're still running Get Model Info; a model not yet researched has no scores and is not chosen by score-based routing until Get Model Info runs (the planner can still name it, and a request can call it by exact id), while the 12 curated rows ship with scores). Raise it to force frontier-only
orchestration. Lower the time cost toward 0 and speed counts for less, so
price decides; this only makes cloud rows cheaper, because local rows added
by Discover have no price and no measured speed, so they already carry no time cost.
Raise it toward `0.0083` ($30/hr) if you're running a SaaS
where every second of latency costs you real money. Both settings can be
changed live from **Admin UI → Settings** — the next run reads the new
value with no restart.

### Where cost appears and how it is computed
Every model call (each step, and the orchestrator's plan, re-plan and compose
calls) is logged with its token counts and its **dollars**:

`dollars = input tokens x the model's input price + output tokens x its output price` <!-- claim C04 -->


The prices come from the capability-matrix row of the model that served the call.
A model with no pricing (a local model) costs `0.0`, and so does a model the
matrix does not know; <!-- claim C05 --> the call is never left blank, with one exception: a
direct-to-model (bypass) call whose provider sends no usage is logged with its
tokens and dollars left blank (unknown), not zero. A call that was retried
after being cut off counts the tokens of both attempts. The tools do not carry
a price today, so tool calls log `0.0`.

- **Run total:** the run digest (`run_digests`) is the sum of the logged calls'
  dollars and tokens for the run and every child run it spawned, so it always
  matches the log. <!-- claim C06 --> It stores `cost_dollars`, `tokens_input`
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
  yet, but a streamed direct call is still logged once, even if the provider
  fails mid-stream or the client disconnects (tokens blank if unknown). When
  the provider refuses the stream, a single reply is fetched instead and
  recorded; if that also fails, nothing is recorded (as for a non-streamed
  call). A
  direct-to-model run is tagged `kind = 'bypass'` (all other runs are
  `orchestrated`), so `GET /runs` leaves bypass runs out by default; add
  `?include_bypass=true` to list them too. `GET /runs/{id}` still returns a
  bypass run by its id. A digest saved before the token and floor columns
  existed reads those fields as unknown (null), not 0.
- **One-pass floor.** Each finished run stores `direct_floor_dollars` in its digest (and `GET /runs/{id}` returns it as `digest.direct_floor_dollars`, with `direct_floor_model`, the baseline model it was priced on). It is a floor on what one direct pass on the baseline model would cost: (the characters of your message, plus the characters of every document supplied, each counted once) divided by 4 at the baseline model's input price, plus (the characters of the final answer) divided by 4 at its output price. <!-- claim C07 --> The receipt and the Run page show it as "One pass on <baseline model>: at least US$X", followed by a note (the amount is rounded down, so it never reads above the stored floor): the figure leaves out retries, and structured output such as JSON costs more tokens than the estimate assumes. On the round-1 benchmark (17-Nov-2026) the real direct cost was between 1.3 and 4.4 times this floor (job C 1.3, job A 2.2, job B 4.4; the top of the range includes a retry that was thrown away); `digest.direct_floor_ratio` carries that range, `[1.3, 4.4]`. <!-- claim C23 --> No page shows the old figure: the per-run `baseline_dollars` (BORG's own tokens priced at a reference model) is no longer written to the digest, old digest rows keep theirs, and the raw Details view strips every baseline field. A failed run (including one that timed out), a resumed child run, a run with no baseline model or none with a price, and a direct-to-model run, store no floor and the line is left out. The baseline model is the checked model with the highest `reasoning` score (ties go to the smaller model id); set `baseline_model_id` (Admin config) or `BASELINE_MODEL_ID` in `.env` to pick another.

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
   light, mid, heavy) decide. Unless a run alias such as `borg_route:local`
   prefers local models (see "Chat model aliases"), the step goes to the ticked
   model with the lowest effective cost (dollars plus time) whose score for that
   kind of work is at or above the tier's floor and whose context window fits the step. <!-- claim C01 -->
   A step that invokes a skill takes the skill's own tier and specialty when
   the planner gave none. A step with no labels is `reasoning`, `mid`.
   Near-synonyms are accepted (`creative` is read as writing, `code` as
   coding, `extract` as extraction, `summary` or `summarise` as
   summarize), and each such reading is written into the step's route
   notes; any other unknown word is treated as `reasoning`, also noted.
   If the skill a step names cannot be found, the trace shows a
   `BRT-ORC-023` warning and the step is routed without the skill's hints.
   An `apply_to_each` step's items all run on the step's own model.
3. Outside the on-premise lane, if no ticked model clears the floor, the step
   runs on the run's default model and the trace shows a `BRT-ORC-019` warning. <!-- claim C02 -->
   (A step that must stay local never falls back to a cloud model; see "On-premise lane".) If routing fails for
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
up to eight models that cleared the floor (the chosen one included), lowest
effective cost first. <!-- claim C03 -->

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
4. It takes a while for a full pool and spends a little money on the scoring model, so run it when you add models, not before every question.
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
- **governance_instruction** — what the operator originally asked, word for word. Every step, every item of an `apply_to_each` and every call inside a child run shows this in its governance block, so no model is ever told that a step's own text is "the user's instruction" (Law 1). <!-- claim C08 --> A child run also gets a short "Your Assignment In This Run" block naming its sub-task.
- **output_schema** — the exact shape the model must return (this is the compression contract)
- **type** — one of: `orchestrate`, `answerer`, `apply_to_each`, `state_op`
- **state_op `source`** — a `state_op` step can name an earlier step (`"source": "n1"`; `inputs_ref` is read the same way, never ignored); it then works on the list that step produced (its `items`, `results`, `list` or `result`), or on the groups of rows a `group_by` without `count` produced. If the named step has produced neither (or does not exist), the step fails with BRT-SOP-003; counts per group are not rows, so a step reading them fails the same way. A list that is empty (for example a filter that matched nothing) is valid, so a following count gives 0.
- **state_op ops and params** (planner ruling 25) — the four ops and the params each takes:

  | Op | Params | Result |
  |---|---|---|
  | `filter` | `key` (required), `value` (required), `negate` (optional, true keeps the rows that do not match) | the whole rows whose field equals `value` |
  | `count` | `key` or `group_by` (optional) | `{"count": N}`, or the number of rows per value of the field (a row without it counts under `__missing__`) |
  | `group_by` | `key` or `group_by` (required), `count` (optional) | the rows per value of the field; with `"count": true`, the number of rows per value |
  | `sample` | `n` (required), `key` or `group_by` (optional), `seed` (optional) | `n` rows at random; with a grouping field, or reading a `group_by` step's groups, `n` from each group, as one flat list |

  A field is read from the row's `output` first, then the row itself, so the rows of an `apply_to_each` step (`{index, item, output: {...}}`) work as they are; `"output.bucket"` means the same as `"bucket"`. `filter` and `sample` return whole rows in that shape. A param the op does not take fails the step with BRT-SOP-006 and is never ignored; an unknown op (BRT-SOP-001) or a missing param (BRT-SOP-002) also fails it. Each of these messages names the op's params within the 200 characters re_plan is shown.
- **skill `items_ref`** — a skill that works on a list (such as `extract_fields`) takes `items_ref` naming the earlier step whose list it should walk (`"items_ref": "n1"`). The items are read from that step's output the same way a plain `apply_to_each` step does. If no such step has produced a list, the step fails with BRT-APE-001.
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

### When the final write-up goes wrong (BRT-ORC-028, -029, -030)

The planner, re-plan and final write-up (compose) share one prompt. The engine's message to the model names the mode in one line ("Mode: re_plan. Reply with the next action." or "Mode: compose. Reply with the final answer."). If the write-up still comes back in the wrong form, BORG_ROUTE tries, in order:

1. **The answer is there, in the wrong place** (for example `{"action": "compose", "response": "..."}`, or the text under `final_answer` or `answer`, or a list where text was expected): it is used as the answer, with no extra call. The trace shows BRT-ORC-028. When the reply also carries `action` or `next_nodes` (it is shaped like a re-plan), only `response`, `direct_answer`, `final_answer` or `answer` count; a `message`, `output` or similar field there is the planner's note, not the answer, and the write-up is asked for again (step 2).
2. **No answer in the reply:** the write-up is asked for once more (BRT-ORC-029).
3. **Still no answer:** you get the results of the steps that finished, under a plain note saying the final write-up failed: each step's answer, or for a per-item step each item's result and how many items failed. The steps that finished come back under the note, with `answer_kind` `salvaged` (in `raw_local` or `all_local`, a step that read documents shows its output only if it passed the output check). <!-- claim C10 --> The trace shows BRT-ORC-030 as a warning, and `GET /runs/{id}` shows `"answer_kind": "salvaged"` on the run (a written-up answer shows `"composed"`). With the on-premise lane in `raw_local` or `all_local`, a step that read your documents appears only if its output passed the output check, and only document ids (never document text) are shown for its items.

If the write-up call itself fails (a timeout, a connection error, a reply that is not JSON), step 3 happens straight away: the finished steps' results come back with the note, BRT-ORC-030 and `"answer_kind": "salvaged"`.

If no step finished either, the run fails with BRT-ORC-009 (or the failed call's own error) as before.

### Long lists in the answer: rows placed by code (BRT-ORC-032)

When a run has a finished per-item step (`apply_to_each`) or a `state_op` that returned a list, the write-up does not need to retype its rows. The engine adds a few lines to the write-up's message: each such step's id, its row count, whether its rows are in input order or stored order, and its field names (nothing else). The write-up may then write `@@ROWS:<step id>@@` where the list belongs, and the engine replaces it with that step's stored rows, one row per line in a ```` ```json ```` block, or `@@ROWS_TABLE:<step id>@@` for a Markdown table. Each row is `{"item": <the item or its id>, ...the step's output fields}`. A failed item stays in its place as `{"item": ..., "error": "<code>"}`. A failure the model itself reported shows as **BRT-ANS-010**. <!-- claim C24 --> So a 200-row answer costs the write-up a sentence and one marker instead of thousands of output tokens. A write-up that types the rows itself still works.

- **A marker is never left in an answer.** A marker that names no list step, or a near-miss such as `@@Row:x@@`, gets the write-up asked once more with the valid ids (BRT-ORC-032). If the second reply is wrong too, the finished steps' results come back as in step 3 above, with BRT-ORC-032 as a warning and `"answer_kind": "salvaged"`. Text like a marker that you wrote in your own message, or that a step returned, is left alone.
- **A write-up cut off by the token cap** while such a step exists is retried once (even at the 16,000-token ceiling), and the retry tells it to use the marker instead of typing the rows. If that retry is cut off too, the finished steps' results come back (salvaged).
- A `@@ROWS:` marker inside a code block but not alone on its line is placed as one compact line, so the block stays intact; a `@@ROWS_TABLE:` marker inside a code block is moved out of it.
- With no such step, nothing changes: the write-up's message is exactly as before.
- **Limit:** a list inside a child run (an `orchestrate` step) reaches the parent run as text, so the parent's write-up has to retype it.

Every completed run records how its answer was made in `answer_kind` (`GET /runs/{id}`): `composed` (the final write-up), `salvaged` (the finished steps' results with a note, BRT-ORC-030), `direct` (the planner answered without steps), `declined` (the write-up declined with `action: fail` and its reason is the answer, or nothing at all came back with no steps to show) or `shortcut` (a short conversational message answered by the Rule Zero shortcut). One more kind, `planner_only` (planner ruling 26a): the run had no documents, steps were planned, every one of them failed, and the write-up still returned text. The run is still `completed`, but the trace records BRT-ORC-033 as a warning, and the receipt (Ask page) and the Run page header say: "Every planned step failed (N of N), so the planning model wrote this answer alone, with no step's work behind it. Step failure codes: ...". Treat such an answer as the planning model's own, unchecked.

With documents the rule is stricter (planner ruling 26b): if the run has documents (any lane mode) and no step that read them finished, the run fails with BRT-ORC-034 before the write-up is called, so no answer text exists. The error lists each failed step's code; a step that read documents shows only its code and document ids (for example `BRT-LANE-002 (document doc_001,doc_002)`), never its message or any document text.

When some steps finished and some failed, the answer is composed as before. The receipt shows the number of steps, not how many of them failed; the Run page marks each failed step on the plan.

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

For unambiguously conversational short prompts — greetings, factual lookups, small requests, casual chat — BORG_ROUTE now bypasses the plan / re_plan cycle and answers in a single compose call. This saves 2-3 orchestrator calls on prompts where decomposition adds no value.

### When the shortcut fires
The pre-classifier is deterministic and rule-based:
- prompt length < 500 characters, AND
- no list markers (`-`, `*`, `•`, `1.`, `1)` on a line start), AND
- none of these phrases: `for each`, `list of`, `classify these`, `analyse these`, `analyze these`, `summarise each`, `summarize each`, `process each`, `compare these`, `compare all`, `for every`, AND
- none of these live-data words (the prompt needs a web search, so it takes the full path): `latest`, `current`, `today's`, `this week`, `this month`, `this year`, `news`, `price of`, `stock`, `weather`, `score`, `who won`, `breaking`, `right now`, `as of`.

If all four checks pass, the orchestrator goes straight to compose mode. An `orchestrator_decision` event with `mode=rule_zero_shortcut` is written to the run trace, so you can tell the shortcut fired vs. a normal direct-mode decision.

### Disabling the shortcut
- **Per-install:** set `BORG_RULE_ZERO_SHORTCUT=false` in `.env`.
- **Live, no restart:** `POST /config` with `{"key":"rule_zero_shortcut","value":"false"}`. The config key wins over the env var.

Turn it off when you want to compare costs / latency before and after, or when debugging plan-mode behaviour on short prompts.

---

## Streaming Heartbeats (BRT-STR-01)

`POST /v1/chat/completions` with `stream: true` and `model: borg_route*` now emits SSE keepalive comments (`: heartbeat <ts>`) every `BORG_STREAM_HEARTBEAT_SEC` seconds while the pipeline runs. Previously the stream went silent until `orchestrate_run` finished, which on multi-node plans took long enough that browsers timed the fetch out before any bytes arrived.

Keepalive lines are ignored by SSE consumers but reset their idle timers. The real response is word-streamed after the pipeline resolves, and the chunks add back up to the exact answer: line breaks, indents and code blocks arrive intact. Stage updates shown while a chat streams belong to that chat's own run only; other runs going on at the same time no longer leak in.

### Tuning
`BORG_STREAM_HEARTBEAT_SEC` (default `5`). The value must be positive; `0` does not turn heartbeats off. Non-streaming (`stream: false`) requests are unaffected.

---

## Loud Failures (BRT-ORC-02)

Previously, three silent-fallback sites in `orchestrate_run` could make a broken run appear blank (no envelopes, no errors, just `status=failed`). Phase 2 made each visible in the run trace:

- **BRT-ORC-007** (no orchestrator model available) now propagates instead of being swapped for a synthetic empty-endpoint placeholder that crashed deep in `httpx`. Check that at least one model is checked and has a `task_score` above your quality bar.
- **BRT-ORC-015** (warning) — the default answerer selection failed and the orchestrator model is being reused. The run still completes; you'll see the event in the trace.
- **BRT-ORC-016** (error) — envelope save/update failed. Logged and the run continues; check DB health (`data/borg_route.db`).

### When a reply is cut off by the token cap (BRT-ORC-018, BRT-ANS-009)

Every call to a model carries a cap on how many tokens it may write: `ORCHESTRATOR_MAX_TOKENS` for planning and composing, `ANSWERER_MAX_TOKENS` for steps (both default to 2000 when unset). A reasoning-family model gets four times the cap, at least 8000, to leave room for its hidden thinking. A step may ask for a larger cap of its own (`max_output_tokens`, default 4000); the larger of the two is used.

If the model stops because it reached the cap, BORG_ROUTE repeats the call **once** at double the cap, never above 16000. There is no second retry. <!-- claim C22 --> If the first cap is already 16000 or more (for example a reasoning model with `ANSWERER_MAX_TOKENS=6000`, which gives 24000), no retry is made.

What you see:

- **Planner or compose call retried:** the run carries on. The trace's `orchestrator_decision` event for that call shows `"length_retry": true` and `"code": "BRT-ORC-018"`, and its token counts are the two attempts added together.
- **Every planner, re-plan and compose call** records why the model stopped (`finish_reason`, for example `stop` or `length`) on its `orchestrator_decision` event, retries included, so you can tell from the trace whether a reply was cut off.
- **A step still cut off:** the step fails with **BRT-ANS-009**. The message says either "even after one retry at double the cap (A -> B)" or "no retry", so you know which happened. Raise `ANSWERER_MAX_TOKENS` in `.env` or split the request into smaller pieces.

**Per-call timeouts.** One step call may take `BORG_ANSWERER_CALL_TIMEOUT` seconds (default 120, else **BRT-ANS-006**) and one planner or compose call `BORG_ORCHESTRATOR_CALL_TIMEOUT` seconds (default 180, else **BRT-ORC-001**); both are read at each call, and a value that is not a positive number is ignored with a warning in the log.

---

<!-- Added: 2026-04-18 — Slice 3: Recursion + Admin + Resume + Threads -->
## Recursion and Depth Control

BORG_ROUTE can orchestrate sub-problems by spawning **child runs**. Each child run has its own run ID, linked to its parent via `parent_run_id`. The depth of nesting is controlled by the maximum recursion depth (5).

### What happens at the depth cap
When an orchestrate node is dispatched at the maximum depth, BORG_ROUTE **demotes it to an answerer** — the model responds directly rather than trying to recurse further. This is logged as a `BRT-ORC-020` event in the run trace.

### The depth limit
The limit is fixed at 5 in the engine (`MAX_DEPTH` in `src/orchestrator/engine.py`). It cannot be changed from the Admin pages.

---

## Admin Pages

The Admin pages sit in the left menu under **Admin**, starting at `http://localhost:8000/ui/admin_overview.html`. Changes apply from the next run; nothing needs a restart.

- **Overview** — a first-run checklist (cloud key saved, models in the pool, local server reachable, admin token, lane mode), what has been spent, and the last runs. A failed run has a **Run again** button.
- **Models** — tick the models the router may use. **Discover** finds local models and adds the curated cloud list; **Get Model Info** researches each model and updates its scores. Each row is marked as running on this machine or in the cloud.
- **Keys** — says whether the OpenRouter key is saved (the running server has it); a key that is only written in `.env` shows as "In .env, restart needed". It never shows a key. Paste a new key to replace the saved one.
- **Rules** — global rules (free text) and the rules ledger. Both are sent with every model call. A retired rule stops being sent but stays in the ledger.
- **Settings** — every setting the engine reads: the quality floors for light, mid and heavy steps, the orchestrator quality requirement, time cost per second, the baseline model (it prices the one-pass floor on the receipt, and the router prices each step on it), a pinned planner, the run time limit, Rule Zero, and the on-premise lane keys. A bad value is refused with `BRT-ADM-006` (lane keys: `BRT-LANE-005`). **Reset** (or saving an empty value) forgets the saved value, so the `.env` value, or the built-in default, applies again. Baseline model empty means `BASELINE_MODEL_ID` from `.env` if set, else the ticked model with the highest reasoning score. The pinned planner endpoint must be an `http://` or `https://` address. `POST /config` also refuses a key that is not a setting (`global_rules` and `admin_rules_ledger` excepted) and a rules ledger that is not a list of rule objects, both with `BRT-ADM-006`. The two max-token settings are read from `.env` only and are shown read-only.
- **Skills** — the skill library with each skill's usage.
- **Logs** — events, the cost summary, and Stats: run outcomes, tokens by model, feedback and the outlier runs. Outlier runs are ranked by what they cost or how long they took, never by prompt length.

### The admin lock

Pages that change things (keys, settings, rules, models, skills) and the logs (`GET /logs`, entities and export) are locked by `BORG_ADMIN_TOKEN` in `.env`. <!-- claim C18 -->

- **Token set:** the first time you save something, the page shows a box asking for the token. Type it once and press **Save token**; the call is retried and the token is kept until you close the tab. A wrong token is forgotten and asked for again. Overview shows "Admin token set".
- **Token not set:** only the computer the server runs on can use these pages. From any other computer the page says that remote admin is off and what to set (`BRT-API-046`). Overview shows "Admin token not set".
- Asking questions and reading a run stay open to everyone. <!-- claim C19 --> In a run's details, the exact text sent to cloud models is shown only to an admin (the same rule), and while the lane is on the event text (error messages and other decision notes) is hidden from anyone else; type, model, tokens, cost, times and routing stay.
- The token must be ASCII (letters, digits, punctuation; no spaces or accents). Anything else is reported as misconfigured (`BRT-API-047`, `admin_token_valid: false` in `GET /keys/status`) and the locked routes refuse everyone until it is fixed.
- **Logs, Export JSONL** is a button: it downloads through the same token prompt and saves a file.
- API clients send `Authorization: Bearer <token>`; a missing or wrong token gets 401 (`BRT-API-045`).

---

## Status endpoints (Admin pages)

These two read-only routes feed the Overview checklist and the Keys page. Neither returns, logs or echoes a key.

### `GET /keys/status`
Returns `{"providers": [{"id", "label", "env_var", "saved", "env_file_only"}], "admin_token_set": bool, "admin_token_valid": bool}` for OpenRouter and Tavily. `admin_token_set` says whether `BORG_ADMIN_TOKEN` is set and `admin_token_valid` whether it is ASCII-only (never the token); Overview uses them. `saved` is true only when the running server has the key (set from `.env` at start-up, or by saving it on the Keys page). `env_file_only` is true when the key is a line in `.env` but the running server does not have it yet: restart the server.

### `GET /status/local`
Probes the configured local model servers (Ollama, LM Studio, other local OpenAI-compatible addresses) with a short request and returns `{"reachable": bool, "servers": [{"name", "url", "reachable"}]}`. `reachable` is true when any server answers. No model is loaded by the probe.

### `GET /runs`
Besides the `runs` page (`limit`, newest first), the response carries `counts`: `total`, `completed`, `failed`, `running`, `pending`, `thumbs_up` and `thumbs_down` over every run, not just the page. The Overview and Logs Stats counts come from there.

---

## Running a Failed Run Again

If a run fails partway through (network error, model timeout, etc.), you can run it again from the Admin Overview.

### How to run it again
1. Open `http://localhost:8000/ui/admin_overview.html`.
2. Find the failed run under **Last runs**.
3. Click **Run again**. It is offered only for failed runs. It plans the run from the start, with the words you first sent (also when the run failed before it had any steps, for example at planning). It does not continue where the run stopped, and it refuses runs that read documents. A run that is running, or that was just submitted and still has work in flight, cannot be run again (409, BRT-API-024); wait for it to finish or fail.

### Via API
```
POST /runs/{run_id}/resume
```
Returns 202 immediately. Poll `GET /runs/{run_id}` for updated status.

The re-run works from the run's own instruction, the words you first sent,
and every step it makes is governed by those words (Law 1), never by the
text of a step saved in the failed run. <!-- claim C09 --> Resuming a child run keeps its own
sub-task and is governed by the top-level run's instruction.

### If something goes wrong
- **"BRT-DB-020"** — The run is already completed and cannot be resumed.
- **"BRT-API-024" (409)** — The run is still running. Wait until it finishes or fails.
- **"no_pending_work"** — The run is not failed and has no failed or unfinished step, so there is nothing to do.

---

## Chat model aliases

The `model` field of `POST /v1/chat/completions` (and the **Routing** dropdown on the Ask page) steers how the router picks a model for each step:

- `borg_route`: the planner's own tier for each step.
- `borg_route:light`, `:mid`, `:heavy`: every step is routed at that tier's quality floor, replacing the tier the planner chose. `:light` is the cheap run, `:heavy` the careful one. Planning and the final answer still use the orchestrator model.
- `borg_route:coding`, `:creative`, `:reasoning`, `:extraction`: every step is routed for that kind of work, replacing the kind the planner chose, the same way a tier alias replaces the tier (`creative` is read as writing). Each step's routing notes say what the alias replaced.
- `borg_route:local`: a local model (LM Studio, Ollama) that clears the step's floor is preferred over a cheaper cloud one; if none clears it, the cheapest cloud model is used as usual.

A step's `recommended_model` still comes first. The alias also reaches the child runs of the request. Each step's `routing` (see below) lists the alias and a note saying what it changed.

---

## Reading why each step used its model

`GET /runs/{run_id}` returns `run`, `envelopes`, `child_envelopes`, `events`, `digest`, `outbound` and `calls_left_machine`. `run.lane_mode` is the lane mode the run started under (`off`, `raw_local`, `all_local` or `by_reference`; null for a run from before it was recorded), not today's setting. `calls_left_machine` is `{count, source}`: the outbound ledger's count (`source` "ledger") when the run used the lane, otherwise the number of logged calls to models that are not on this machine, child runs included (`source` "locality"). When it cannot be worked out (a model no longer in the matrix, or no call logged) both are null and the pages say "Not recorded"; it is never turned into 0. Each envelope's `outbound_payload` carries `node_id`, the plan node it ran, so a step with no route event (a `state_op`) still maps to the plan. Every model row from `GET /models` and `GET /models/matrix` carries `is_local`, worked out from the endpoint's host (loopback or `BORG_LOCAL_HOSTS`), `-cloud` tags and the allow-list, never from the `source` label. `child_envelopes` holds the envelopes (with their `routing`) of every child run the run started, each tagged with its own `run_id`. Do not add these dollars to the parent's: the `dollars` of an `orchestrate` envelope already include everything its child run spent, and a child run's `orchestrate` envelope already includes its own grandchildren. The run total is `digest`. Each envelope (one per step) now carries:

- `routing`: why the step went to its model. It holds `chosen`, `reason` (`routed`, `planner_pick` or `fallback_default`), `task_type`, `tier`, `floor`, `est_input_tokens`, `est_output_tokens`, `candidates` (the models that cleared the floor, lowest `est_cost` first, at most 8, each with a score, `est_cost` (dollars plus time cost) and `est_dollars` (dollars only)), `baseline` (a reference model the router prices the same step on: the best-reasoning checked model, or the one set by the `baseline_model_id` config key / `BASELINE_MODEL_ID`) and `baseline_est_cost`. These are kept in the record for anyone reading the API; the pages show no baseline comparison. If a configured baseline is not in the matrix, `baseline_note` says which model was used instead.
  - Every figure under `routing` is an estimate, never a measured cost. The token counts are guessed from the step's text length, and `dollars` on the envelope is the only real cost.
  - `est_dollars` is the chosen model's estimated dollars only (0.0 for a local model). `baseline_est_cost` is the same estimate at the baseline model's prices, so the two compare like for like. `est_cost` is what the router ranks by: dollars plus the cost of waiting (`TIME_COST_PER_SEC`), so it is not comparable with `baseline_est_cost`.
  - A step whose routing failed (`fallback_default`) still carries the token estimate and `est_dollars`; if no estimate could be made they are `null`, never 0.0. A `planner_pick` also lists `candidates`, for information; the pick itself is unchanged. When no model cleared the floor, `candidates` is empty and `candidates_reason` says so.
  - For an `apply_to_each` step the estimates cover every item: `items` is the count, `per_item_est_dollars` and `per_item_baseline_est_cost` are one call, and `est_dollars` and `baseline_est_cost` are the totals. They assume one call per item, so for a step whose items are packed (see "Packing small items into one call") they overstate the cost; every model is overstated alike, so the choice of model is the same.
  - For an `orchestrate` step `served_by` is `child_run`: a child run plans the step and picks its own models, so `chosen` is only the fallback if the step is demoted at max depth (`note` says so), `child_run_id` names the child run, and `est_dollars` and `baseline_est_cost` are `null`. A step demoted at max depth runs as one single call instead: it has no `served_by` or `child_run_id`, carries `est_dollars` and `baseline_est_cost` like any single call, and its `note` is "demoted at max depth". A skill step is described by the type it runs as (`apply_to_each` or `orchestrate`), not by the type written in the plan.
- `dollars`: what the step's call actually cost.
- `latency_ms`: the step's own wall-clock run time in milliseconds: from the start of its call (model calls, tools, a child run) until it returns. It leaves out the pricing and database work done after the step returns. Latency of each individual call is on the log rows.

---

## On-premise lane

The lane hands documents to the steps that read them, by reference. What it promises depends on the mode:

- `raw_local` and `all_local` keep your documents on your own machines. Their promise, in these words: **raw document text is sent only to models on your own machines. Everything sent to a cloud model is listed, and you can read it.** <!-- claim C11 -->
- `by_reference` makes **no privacy promise**: any ticked model, cloud included, may read a document's text. It only keeps bulk data out of the prompt, and still lists every request (see "By reference, for cloud models" below).

The full design is `docs/design/on_premise_lane.md`.

### What counts as "local"

A model is local when the address its requests go to is this machine, never because of its label on the Models page:

- `localhost`, `127.x.x.x` and `[::1]` are local. So is any host you list in `BORG_LOCAL_HOSTS` in `.env` (names, IP addresses or ranges such as `10.8.0.0/16`, comma-separated). A private address that is not listed (for example `192.168.1.20`) is not local: it could forward to a cloud service.
- Addresses written in unusual forms (`127.1`, `2130706433`, `0x7f.1`, `0.0.0.0`, a URL with a user name in it) are not local.
- An Ollama model whose tag ends in `-cloud`, or that Ollama reports as served from a remote host, is never local: it is served from localhost but runs elsewhere.

In `raw_local` and `all_local`, when a step must stay local, the router offers it only local models, ignores a cloud model the planner recommended, and never falls back to a cloud model. If no local model clears the step's quality floor, the step fails with **BRT-LANE-002** and nothing is sent. <!-- claim C13 --> Each such step's `routing` shows `"local_only": true`. In `by_reference` no step must stay local: a step that reads documents is routed like any other step, and the document's size still counts toward the model's context.

### Turning the lane on

Set `lane_mode` in Admin (or `BORG_LANE_MODE` in `.env`):

| Mode | Planning and the final answer | Steps that read documents | Other steps | Web search | Direct-to-model, Discover, Get Model Info |
|---|---|---|---|---|---|
| `off` (default) | as before | documents are refused | as before | allowed | allowed |
| `raw_local` | may use a cloud model | **local models only** | may use a cloud model | allowed, checked, listed | allowed and listed; documents refused |
| `all_local` | **local only** | **local only** | **local only** | refused | refused |
| `by_reference` | may use a cloud model | **any ticked model, cloud included** | may use a cloud model | allowed, listed | allowed and listed; documents refused |

With `off`, nothing changes from the behaviour before the lane existed.

### By reference, for cloud models (`by_reference`)

`by_reference` makes no privacy promise. It hands documents over exactly as `raw_local` does (`BORG_DOCS_ROOT` and its path checks, a manifest to the planner, the text loaded inside the step that reads it, long documents in parts), but the step that reads a document may be served by any ticked model, cloud included, and that model receives the document's text. The output guard (**BRT-LANE-004**) and the check on everything sent out (**BRT-LANE-006**) do not run, so no step is refused for reading or copying a document. Every request is still written to `outbound_requests`, with whether it left the machine, and listed under the run. The planner's manifest says the steps that read documents may run on cloud models. A run that read documents is still not resumed (**BRT-LANE-008**), as in the other modes.

Use it to keep bulk data out of the prompt. Lists of more than a few dozen items go in as documents, not in the prompt.

### Handing over documents

1. Put the documents in one folder and set `BORG_DOCS_ROOT` in `.env` to it. It may not be `.` or the application folder (which holds `.env` and the database).
2. Name them relative to that folder: `"documents": {"folder": "client_notes"}` (every file in it and below) or `"documents": {"files": ["client_notes/acme.txt"]}`.
3. Only `.txt` and `.md` are read (as UTF-8). Other files are listed as "unsupported, not read".

A path that leaves the folder (`..`, an absolute path, `C:x.txt`, `\x.txt`, a network path, a link or junction pointing outside) is refused with **BRT-LANE-001** before anything is read; <!-- claim C15 --> the message names only the path you sent. A file replaced between the check and the read is refused too.

The planner never sees the text. It gets a list: an id per document (`doc_001`), its name, type and size. Set `lane_manifest` to `ids` to hide the names as well. A document longer than `lane_part_chars` (default 8000 characters) is split into parts (`doc_001#2`); nothing is cut off.

### How a step that reads documents works

- The runtime, not the planner, marks a step **raw** when it reads documents (`"inputs_ref": "documents"`, `"documents": ["doc_001"]`, or a skill given `items_ref: documents`), or when it reads the output of a raw step that was withheld. Raw steps run on local models only (in `by_reference`, on any ticked model, with no output guard and no call check; the limits and checks below are those of `raw_local` and `all_local`).
- The text is loaded inside the step, right before its own (local) request. Plans, saved envelopes and results carry document ids, never text.
- **What a local step returns can be passed on.** That is the limit of the promise, so it is bounded: an output longer than `lane_max_output_chars` (default 1500 characters), one that copies a passage of `lane_max_verbatim_chars` (default 120) characters from the document, or one that holds a short document (under that limit) whole, is withheld (**BRT-LANE-004**). <!-- claim C17 --> The check reads the output's values as text, so line breaks and accented letters do not hide a copy. Ask such steps for short fields (names, dates, labels).
- When a raw step fails (in `raw_local` and `all_local`), the planner and the final answer see only the code and the document id, for example `{"code": "BRT-LANE-004", "doc": "doc_001"}`. A failure the local model itself reported always shows as **BRT-LANE-004**, whatever code or text it wrote. Only a failure BORG_ROUTE itself raised keeps its own code (for example **BRT-ANS-009** for a truncated answer, or a `BRT-LANE-*` refusal). The full error stays in the local trace.
- In `by_reference` a failed step that read documents is shown like any other failed step: the code and the first 200 characters of the message, with **BRT-ANS-010** when the model itself reported the failure. Text a model wrote is never read for a code, in any mode. A failure BORG_ROUTE itself raised while such a step ran keeps its own code and also names the document id, for example `{"code": "BRT-LANE-007", "doc": "doc_001", "message": "BRT-LANE-007: the ledger row ... could not be written ..."}` when the record of a request could not be written. (A path outside `BORG_DOCS_ROOT` never gets this far: the whole request is refused with **BRT-LANE-001** before any step runs.)
- With documents, the planner may not answer directly or skip the plan (**BRT-ORC-003**), and the short-prompt shortcut is off.
- An `orchestrate` step given documents is refused outside `all_local` (**BRT-LANE-005**): its own planner would read them.
- Every call a raw step makes, retries included, is checked right before it is sent; a call about to leave the machine is blocked (**BRT-LANE-003**, which means a defect).
- Rows the engine writes into the answer for a row marker (see "Long lists in the answer") are rows the write-up already received in that run: placing them sends nothing new off the machine. Each rows block is also checked against the documents with the same copy rules as the output check, before it is placed; a hit fails the run and nothing is stored (**BRT-LANE-009**, which means a defect). The write-up's own text is not checked this way.

### The check on everything sent out, and the record

- While the lane is `raw_local` or `all_local` and the run has documents, every request headed off the machine (planner, final answer, cloud steps, web search, direct-to-model) is compared with the documents first. If it holds a passage of 120 characters, more than 10% of a document's 8-word sequences (`lane_max_ngram_share`), or a short document whole, it is blocked (**BRT-LANE-006**). <!-- claim C16 --> Case, punctuation, line breaks, repeated JSON escaping, invisible characters (zero-width space, soft hyphen) and a copy split one word per field do not hide a match. What it does not catch is listed under "What is not promised".
- Every request a run makes while the lane is on is written to the `outbound_requests` table before it is sent, with its exact body (secret fields such as `api_key` are blanked in the stored copy), and whether it left the machine. If that write fails, the request is not sent (**BRT-LANE-007**). <!-- claim C14 --> A retry is its own row. Many steps sending at once (a document in dozens of parts) take turns writing: each write waits for the database (up to 30 seconds) and is tried up to 5 times, so only a write that truly fails blocks a request.
- Local calls ignore proxy settings (`HTTP_PROXY` and the like), so a proxy cannot carry them off the machine.

### Using the lane from the API

- `POST /v1/task`: add `"documents": {"folder": "..."}` (or `"files"`) to the body. A refused path returns 400 with **BRT-LANE-001** and creates no run; documents while `lane_mode` is `off` return 400 with **BRT-LANE-005**.
- `POST /v1/chat/completions`: the same `documents` field, at the top level next to `messages`, with a `borg_route` model. With a direct model id it is refused (**BRT-LANE-005**); in `all_local` every direct-model request is refused.
- `GET /runs/{run_id}` returns `outbound`: every request this run and its child runs sent off the machine, oldest first, each with `run_id`, `url`, `destination`, `attempt`, `status` and the exact `body`. It is empty when the lane was off. The `body` of each entry is shown only to an admin (a valid `BORG_ADMIN_TOKEN`, or this computer when none is set); other callers get `body: null`. <!-- claim C20 --> The hook is `lane_admin_allowed` in `src/api/tasks.py`. `GET /logs/entities` and `GET /logs/export` need the same admin access.
- `POST /runs/{run_id}/resume` is refused for a run that read documents, or for a child of one (409, **BRT-LANE-008**): start a new run with the same documents. If the check itself fails, the resume is refused too. A resume, and a run started with the MCP tool `borg_route.submit_task`, follow the current `lane_mode` exactly as `POST /v1/task` does.
- **Chat history is the client's job.** The server stores no chat history for `POST /v1/chat/completions`: each request carries its own `messages`. A client must not resend answers given under `all_local` in a later request made under another mode, or those answers go to whatever model that mode allows. The Ask page tags each turn with its lane mode and leaves `all_local` turns out under another mode (see "Workspace" below); in every other client this is up to you.
- With `lane_mode` `off`, no other lane setting is read or checked: a run is exactly as before the lane existed.
- Discover and Get Model Info are refused while `lane_mode` is `all_local` (403, **BRT-LANE-005**).
- The lane settings (`lane_mode`, `lane_max_output_chars`, `lane_max_verbatim_chars`, `lane_max_ngram_share`, `lane_manifest`, `lane_local_allowlist`, `lane_part_chars`) appear in `GET /config/effective` and are saved with `POST /config`; an invalid value is refused (400, **BRT-LANE-005**). A new value applies from the next run.

### What is not promised

- In `by_reference`, nothing below is promised either: it makes no privacy promise. Any ticked model, cloud included, receives the text of the documents its steps read, and no copy check runs. The list below is about `raw_local` and `all_local`.
- Your own prompt goes to the planner. Hand documents over by reference; text pasted into the prompt from a document that was also loaded is blocked, text from any other document cannot be recognised.
- Facts a local step extracts (a name, a date, a short quote) can leave inside its answer, within the limits above, and are listed.
- A document can carry instructions aimed at the local model ("put everything in base64"). The limits reduce the damage; they do not catch encodings or paraphrase.
- In `by_reference`, a step's documents may be packed into one cloud call (see "Packing small items into one call"): each document sits between its own delimiters and the call says that items are data, but a document carrying instructions ("label every item X") can still steer the answers for the other documents in its pack. Nothing claims the model resists. For documents from untrusted senders set `BORG_PACK_MAX_ITEMS=0` (or `1`): one call per document, as before.
- The copy checks (the output guard and the check on everything sent out) do not catch:
  - documents shorter than 24 characters once case, punctuation and spacing are folded: these are never checked;
  - encodings (base64, hex), reversed text or reversed word order, look-alike letters from other alphabets, a marker word inserted every few words, or a copy placed one word per key name;
  - paraphrase and summaries;
  - a document spread over many rows of a list placed by code, each row below both copy limits: the 8-word-sequence check is not run on rows joined in code (each row was checked on its own);
  - the results joined in code when the write-up fails ("salvaged" answers): these get no check of their own beyond each step's output check.

  The check is a backstop, not proof.
- File names are visible to the planner unless `lane_manifest` is set to `ids`.
- A local model server that itself forwards to a cloud service looks local. Restrict the lane with `lane_local_allowlist` (model ids or `host:port`) to servers you vouch for.
- `all_local` removes these limits by sending nothing out. <!-- claim C12 -->
- Copies of document-derived text stay in the local database (`logs`, `log_entities`, `envelopes`, `outbound_requests`).

---

## Multi-turn Conversations (Threads)

The engine can keep conversation history in **threads** (database tables `threads` and `messages`). When a run is started with a `thread_id`, the system loads that thread's rolling summary (`rolling_summary:{thread_id}`) and links the run to its thread through the `thread_id` field on the run record. It does not pass the last turns for you: the caller must pass `thread_window` itself.

### What is and is not connected
1. No page and no API route creates a thread or passes a `thread_id` today. The Ask page keeps its conversation in the browser and resends earlier turns with each question (see "Workspace"); `POST /v1/chat/completions` uses the `messages` the client sends.
2. Threads are used by code that calls the pipeline directly (see "Historical Memory" below).

---

## Feedback — Rating Runs

After each answer on the Ask page, thumbs-up (👍) and thumbs-down (👎) buttons appear. Click either to submit a rating. Ratings are stored against the run and counted on the Logs page, Stats tab.

You can also rate a run via the API: `POST /runs/{run_id}/rating` with body `{"rating": 1}` (up) or `{"rating": -1}` (down).

---

## Observability Logs

The Logs page at `http://localhost:8000/ui/logs.html` has three tabs:

- **Events** — all model_call, orchestrator_decision, child_residue, and error events with filtering by run ID and event type
- **Cost Summary** — aggregate token counts and dollar cost grouped by model (`model_call` plus priced `orchestrator_decision` rows)
- **Stats** — run outcomes, tokens by model, feedback and outlier runs (see "Logs and Stats")

Click **Export JSONL** to download up to 1000 `logs` rows, oldest first, for offline analysis. The export has no entities.

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

Results land in the `log_entities` and `log_relations` tables and are available only through `GET /logs/entities` (admin); the JSONL export does not include them. The Logs page has no NER tab.

#### What it was meant to be useful for
1. **Search across runs** — "show me every run that mentioned customer X" or
   "every model call that produced a dollar amount."
2. **Audit and compliance** — quickly spot PII, company names, or sensitive
   terms leaking into prompts or outputs.
3. **Skill-improvement signal** — see which entity types a skill reliably
   handles vs. drops, feeding the Skills Library continuous-improvement loop.
4. **Offline exploration** — export JSONL from the Logs page for the log rows, and fetch entities from `GET /logs/entities` to join them to runs for ad-hoc analysis.

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
3. Call `GET /logs/entities` (admin). Entities for new payloads
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

## Packing small items into one call (`BORG_PACK_MAX_ITEMS`)

A per-item step (`apply_to_each`) repeats the same fixed text in every call: the rules, your instruction, the step's task and its output shape. When the item itself is a short line, almost all of each call is that repeat. With packing, a step whose calls go to a cloud model sends several items in one call: the rules and your instruction once, word for word, then the step's task once, then each item between its own start and end lines under a short id. The reply is one JSON object with one value per id, and each value is what a single call returns (`status`, `confidence`, `output`, `failure_reason`). Every item still gets its own stored result, so the planner, the write-up, rows placed by code and the receipt see exactly what they would see without packing. <!-- claim C25 -->

- **Setting.** `BORG_PACK_MAX_ITEMS` in `.env`, default 20 items per call. `0` or `1` turns packing off: one call per item, and every request is exactly as before. A value that is not a whole number turns packing off with warning **BRT-APE-007**.
- **Cloud calls only.** A call to a model on this machine (Ollama, LM Studio, any server at a local address) is never packed, in any lane mode, so steps that read documents in `raw_local` and `all_local` never are. Packing cuts the token bill, and a local model has none: its gain would be prompt-reading time only. Against that stand the context window a local server really serves, which BORG_ROUTE cannot see (a prompt longer than it can lose its start, where your instruction and the rules sit, with no error), and small models answering 20 items at once.
- **Which items share a call.** Items are taken in order. An item over 400 estimated tokens (characters / 4) runs alone. A call holds at most 4,000 estimated tokens of items, and its prompt plus expected output must fit the model's context and the step's output cap, counting each estimate twice over for safety. The first call of a step goes alone; the other calls are sized from the tokens its reply really used per item. A step whose items fit one call is sent as one call.
- **When the reply is not right.** An item whose id is missing, given twice, or whose value does not match the output shape is sent again on its own, once. An answer under an id that is not in that call is never accepted. A reply cut off at the length limit is split in half and each half sent again (a call of 20 items makes at most 59 calls in the worst case, against 40 one at a time). A call that fails or times out sends its items one at a time; after a timeout the rest of that step runs one item per call. An item the model declines (`status: failed`) is stored as declined (**BRT-ANS-010** upward) and not retried, as with a single call.
- **Known limit: swapped answers.** If the model writes item A's answer under item B's id and B's under A's, nothing can tell. The ids are random-looking, not numbers, so a reply that renumbers or shifts them is caught, but a swap between two real ids is not.
- **Known limit: one item can steer another.** Items in one call are read together. A line inside one item ("label every item X") can change the answers for the others, whatever the frame says. For documents from untrusted senders, set `BORG_PACK_MAX_ITEMS=0`.
- **Cost.** Each packed call's tokens and dollars are split across its items (in whole tokens and whole picodollars, so the parts add up exactly to the call) and logged as one row per item. A call cut off at the length limit is paid and counted too. Every call count (the Run page, `GET /logs/cost-summary`, "calls that left this machine") counts a packed call once.
- **Run page.** The step's "Items" line reads "N items in M calls", and "k retried alone" when some items were sent again on their own. With packing off it reads "N items in N calls". One `orchestrator_decision` event per packed call (`mode: "pack_outcome"`) records how many items were accepted, declined, missing, given twice, under an unknown id, or invalid, and whether the call was cut, timed out or failed.
- **Record of what left.** In the lane's record of outbound requests, each item call names its step (`step_envelope_id`), and a packed call that carries documents lists every document id in it (`documents`).

## How steps get their items, and what the planner sees when a step fails

- When the list is in your message, the planner puts it in the step's `items` (for a skill such as `extract_fields`, `items` in its args, or `items_ref` given as a list), and the step makes one call per item, or packs small items into fewer calls (see "Packing small items into one call" below). Lists of more than a few dozen items belong in documents, not in the prompt: your whole message is sent with every step.
- A step whose `inputs_ref`, `items_ref` or `source` names another step of the same plan waits for that step to finish, just as if it were listed in `depends_on`.
- If `inputs_ref` or `items_ref` names something that is not a step id, and the step has no items of its own, the step fails with BRT-APE-006 before any model call. The message starts with the fix (a list from your message belongs in `items`; `inputs_ref` and `items_ref` must name a step of this run, not a parent run's) and ends with the id it could not find, cut to 40 characters.
- Within one plan, steps run in waves until none is ready; only then does the planner re-plan. A step that waits on another step of the plan (through `inputs_ref`, a skill's `items_ref`, `source` or `depends_on`) runs in the same plan once that step finishes.
- Steps that wait on each other in a loop (n1 reads n2 and n2 reads n1) can never run: they fail with BRT-ORC-024 before re-planning, the message names the loop, and no model is called.
- A step that takes its items from a step that failed fails with BRT-APE-005, naming that step, and makes no model call. This holds for a `state_op` too: one whose `source` (or `inputs_ref`) step failed is not run. A state_op over a failed source now fails instead of computing on an empty list (a `count` over a failed `apply_to_each` used to report 0).
- A skill step with its items written in the step and `items_ref` set to `null` runs as if `items_ref` were left out. A step that cannot be built at all (a skill arg the template refuses, for example) fails with BRT-ORC-008: an `error` event names the step, and the planner sees the reason when it re-plans.
- If a step that a later step `depends_on` failed (including one that failed in a loop, BRT-ORC-024), the later step fails with BRT-ORC-025 before any model is called, naming the failed step, so a chain of dependent steps stops costing money at the first failure.
- When a step has no id, it gets one no other step in the run has used, so a step added at re-planning always runs and keeps its own result. Ids the planner writes are kept as written, with one exception: a re-planned step that reuses the id of a step that already ran (a retry named like the failed step) is renamed `<id>_2` (then `_3`, and so on) and runs; the trace shows BRT-ORC-026 with the old and new ids. Other steps of the same re-plan that name that id (in `depends_on`, `inputs_ref`, `source` or a skill's `*_ref`) are pointed at the new step, unless that would make the two steps wait on each other (the renamed step itself waits on that step): then the reference keeps reading the earlier result, so both run, the renamed one after. Every other reference, and the renamed step's own reference to its old id, still reads the earlier result, which keeps its original id. A number written as an id is the same id as its text (`1` and `"1"` name one step).
- If the planner's step list holds something that is not a step (for example a stray string such as `"rationale: "`), that entry is dropped with BRT-ORC-027 and the other steps run. If nothing valid is left, the reply is treated as having no steps: a plan falls back to the final answer (or BRT-ORC-003 when the run has documents), and a re-plan goes to the final answer.
- A list can be given in place of a ref: `inputs_ref`, `items_ref` or `failed_items_ref` written as a list of strings is the step's items. An empty list fails with BRT-APE-005; a number or an object fails with BRT-APE-006.
- A skill step's instruction is always complete before it is sent: list args such as `fields` (names, or `{name, type, description}` entries) are written out one per line, and if a skill's args cannot fill its template (for example `fields: []`), the step fails with BRT-SKL-008 before any model call and the planner is told why.
- A skill step can ask for more than its skill does: only fields named in the step's `extra_fields` argument (for example `{"urgency": "integer from 1 to 5"}`, a list of names, or a JSON Schema whose `properties` are the fields) that the skill's schema lacks are added to what each item must return, and one line of the instruction ("Also return these fields: ...") asks for them. An answer without them fails the step's check (BRT-ENV-011). The step's `output_schema` is never added (it describes the rows of the final answer, not each item), JSON Schema words such as `type`, `items`, `properties` or `required` are never fields, and the skill's own keys are never replaced. A malformed `extra_fields` fails with BRT-SKL-008 before any model call.
- A skill whose output is "derived from fields" (such as `extract_fields`) asks for one key per field: `"<type> | null"`, or `"enum:a|b|c|null"` when `enum_values` are given. `fields` may be `{name, type, description}` entries, bare names, a name-to-type map or a comma string; a nameless, duplicate or empty field fails with BRT-SKL-008. A schema argument given as a JSON string is parsed.
- Every templated field of a skill is filled in: `concurrency_limit` takes the argument's number (20 when left out; values above 256 run at 256, and the step's output notes the cap), `decompose_task`'s `context_for_child` becomes the child's context.
- `compare_and_rank` and `verify_claim` see their data in the step's context as a numbered list: the items written in the step, or the output of the step their `items_ref` or `evidence_ref` names. With neither, the step fails with BRT-SKL-008 and makes no model call.
- A skill step reads the run's documents whenever any of its `*_ref` arguments is `documents` (for example `verify_claim` with `evidence_ref: documents`). In `raw_local` it then runs on a local model or fails with BRT-LANE-002; in `by_reference` its documents go to whichever checked model is routed. It never runs without them.
- The data a `compare_and_rank` or `verify_claim` step reads counts toward the step's size when its model is chosen: only models whose context window holds the data plus the answer are considered. If none can, the step fails with BRT-REG-020 before any model call. The data is never cut short; give the step fewer or shorter entries.
- A list argument may be given as a JSON string (`fields: '{"a": "string", "b": "int"}'`); a string that starts with `[` or `{` but is not valid JSON fails with BRT-SKL-008. `concurrency_limit` accepts a whole number written as a float (12.0 is 12).
- An argument whose type allows null may be left out; the planner's skill list marks these as "optional".
- BORG_ROUTE runs at most 256 items of one apply_to_each step at a time, whatever `concurrency_limit` says. If a run shows BRT-ANS-011, too many model calls were in flight at once and one waited too long for a free connection; the model was not slow.
- A cookie a provider sets is never kept or sent to another provider. List arguments (`fields`, `buckets`, `criteria`) may be written as a comma string such as "billing, sales".
- A ref names a step of this run only. A child run (an `orchestrate` step) cannot name its parent's steps.
- `documents` names the run's documents only while the on-premise lane is on; with the lane off it is an ordinary step id.
- When a step fails, the planner is told its error code and the first 200 characters of the reason, so it can retry or change course. The final answer step sees only the code. For steps that read your documents on-premise, the planner is told only the code and the document id, never the message.
- If the model itself says it cannot do a step, the code is BRT-ANS-010 (on-premise steps show BRT-LANE-004), whatever code its own text starts with.

---

## Skills Library

The Skills page at `http://localhost:8000/ui/skills.html` is a read-only table of the loaded skills: version, tier, what it does, calls, succeeded, average tokens and average time. It has no edit form; new versions are made through the API below.

A new version is appended to `data/skills.yaml` with an incremented version number. The orchestrator will use the highest version by default.

You can also manage skills via the API:
- `GET /skills` — list all skills with stats
- `GET /skills/{skill_id}` — detail for one skill including all versions
- `POST /skills/{skill_id}/version` — create new version (JSON body with `description`, `when_to_use`, `not_when`, `recommended_tier`)

---

## Stats

The Stats tab of the Logs page (`http://localhost:8000/ui/logs.html`) shows run outcome counts, token use by model, feedback (thumbs up/down) and the outlier runs.

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

## Logs and Stats

The Logs page at `http://localhost:8000/ui/logs.html` has three tabs: **Events**, **Cost summary** and **Stats**. Stats shows:
- **Totals** — runs by outcome, and thumbs up and down.
- **Tokens by model** — a bar per model (top 8).
- **Runs and the runs they started** — which runs spawned child runs.
- **Outlier runs** — the five top-level runs that cost the most, or took the longest (your choice); `GET /logs/outlier-runs?sort=cost|duration`. A run's cost is the whole run tree (the run and every run it started); its duration is the span of the tree's logged activity, from the first to the last log row. Runs the router started inside another run are not listed on their own.

---

## Workspace: Ask, Run and Runs

Open `http://localhost:8000/` (or `/ui/`); it goes to the Ask page. The old chat page address, `/ui/chat.html`, goes there too.

**Nothing loads from another site.** Every response carries a Content-Security-Policy: pages, scripts, images and requests come from this server only (images may also be `data:`), no inline script runs, and the page cannot be put in a frame. <!-- claim C21 --> An answer that contains an image from another site shows no image, and a link to another site is shown as plain text.

### Ask (`/ui/ask.html`)

- Type the job and press Enter (Shift+Enter makes a new line). While it runs, a progress line says what is happening (planning, choosing a model for a step, writing the answer) with a clock.
- The answer arrives word by word. Under it is a **receipt**: number of steps, models used (each marked "this machine" with a filled dot or "cloud" with a ring, with its number of calls), time and cost. Cost is the whole run's total from its digest, steps' own runs included. When the run stored a floor, the receipt adds "One pass on <baseline model>: at least US$X" with the note described under **One-pass floor** above; a figure of BORG's own tokens priced at the flagship is never shown.
- **See how it was done** opens the run. The thumbs record a rating (`POST /runs/{id}/rating`).
- **Add documents (folder)** sends a folder, relative to `BORG_DOCS_ROOT`, by reference (`"documents": {"folder": "..."}`). A line beside it says which lane mode is on. With the lane off, documents cannot be added.
- **Routing** picks a chat alias (see "Chat model aliases").
- The conversation is kept in this browser (`localStorage`); **Clear chat** empties it. Each turn remembers the lane mode it ran under. The server stores no chat history: the page resends earlier turns with each question, **except turns asked under `all_local`, which are resent only while the mode is still `all_local`**. Under any other mode they stay on screen and are never sent again. The page reads the lane mode again just before every send; if it cannot read it, or the value is not one of the four modes, it treats the new turn as `all_local` and resends no `all_local` turn. Other clients of `POST /v1/chat/completions` send their own history and must apply this rule themselves.

### Run (`/ui/run.html?id=<run_id>`)

- **Header:** what was asked, status, the lane mode the run recorded, and four totals: cost of this run (the run's digest; before it exists, "cost so far" from this run's logged calls), time, steps (and model calls), and calls that left this machine (the server's figure; "Not recorded" when it cannot be known). When the run stored a floor, the cost tile adds "One pass on <baseline model>: at least US$X" and a note under the tiles says what it leaves out; no "one top model" figure is shown. A failed run shows its `failure_reason` in plain words with its code.
- **Plan:** a diagram drawn from the run's steps: Plan, then the steps in columns by what they depend on (`depends_on`, `inputs_ref`, `source`, a skill's `*_ref`), then Compose. An `apply_to_each` step is one node with its item count. A step that ran as its own run links to that run; its cost is already inside this run's total. While the run is going, the page follows `/runs/{id}/stream` and redraws as steps finish: one request at a time, an old response never overwrites a newer one, and a dropped stream reconnects with a growing wait (1 s up to 15 s) until the run ends.
- **Select a step** to see its model (this machine or cloud), kind of work and tier, the score it needed, cost, time and tokens, whether its input left this machine, why it failed (plain words), and **Why this model**: the candidates that cleared the bar, lowest effective cost first. The row the router chose says why (lowest effective cost, the planner's pick, or a routing alias or strategy). Two estimated columns: effective cost (dollars plus time cost, what the router ranks by) and dollars alone; both are made before the step ran. The raw record is behind **Details**.
- **What happened:** the run's events as sentences, with the time since the start.
- **What left this machine** (when the lane is on or the run has a record of requests): every request sent off this machine, where it went and what it was. The exact text of each request is shown to the admin only.

### Runs (`/ui/runs.html`)

The history of runs, newest first, with status and rating. Direct-to-model questions (bypass runs) are left out unless **Include direct-to-model questions** is ticked.
