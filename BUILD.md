# BUILD.md: BORG_ROUTE showcase build

Plan and build log. Written 15-Nov-2026 by the planner (Fable). The approved plan this file implements is summarised in `CHECKPOINT.md`; this file is what the builders work from.

**One-paragraph brief.** BORG_ROUTE takes a prompt, lets a planner model break it into steps, and is meant to send each step to the cheapest model that can do it. Today it does not: one model does every step and cost is never recorded. This build makes the claim true, adds an on-premise lane (raw documents never leave the machine), proves the saving with real runs, rebuilds the interface, packages the repo for strangers, and produces a presentation. Deej tests nothing; every check is in the build.

---

## 1. Roles and models

Deej's ruling: Opus 5.5 and Sonnet 5.5 do the work, both at **medium** effort. Fable plans only.

| Role | Model, effort | Does |
|---|---|---|
| Lead | Opus 5.5, medium (the session Deej starts) | Walks the work orders in order. Stays thin: dispatches each work order to a builder, runs its "Done when" commands once, sends the diff to a reviewer, commits, updates `CHECKPOINT.md`. |
| Builder | The model named on the work order (Agent tool: `model: "opus"` or `"sonnet"`, `effort: "medium"`) | Builds one work order. Runs only the tests it touches while working. |
| Reviewer | Opus 5.5, medium, fresh context (Agent tool, new agent each time) | Reads the diff of one finished work order with the brief in section 4. Never the builder of that diff. Does not re-run suites. |
| Acceptance | Opus 5.5, medium, fresh context | Work order 5.8 only. |

The lead may set `effort: "medium"` on every agent it starts: Deej asked for it explicitly on 15-Nov-2026.

If the lead cannot start helpers, it builds each work order itself and still sends every diff to a fresh reviewer agent; if it cannot start any agent, it stops and writes that in `CHECKPOINT.md`.

**The design is settled. Do not brainstorm it again and do not ask Deej design questions.** Skills that fit: test-driven-development, systematic-debugging, verification-before-completion, requesting-code-review, subagent-driven-development (if used, move its `.superpowers/sdd` workspace to `C:\Stage\_archive\` at the end; never delete it).

## 2. Rules that bind every work order

1. **Read before acting.** `DESIGN_PRINCIPLES.md` (Two Laws, invariants), `AGENTS.md`, `CODING_STANDARD_CORE.md` (commit law), then the files the work order names.
2. **Check claims first.** Every "reported, not checked" fault below is a claim. Confirm it in the code before fixing it. If it is not real, record that in the build log and skip the fix.
3. **Protected files:** `src/prompts/*.py`, `data/skills.yaml`. The only approved edit is work order 1.3. `data/capability_matrix.yaml` changes only through the app's own Discover, Get Model Info and checkbox calls.
4. **Only four step types** (`orchestrate | answerer | apply_to_each | state_op`). New behaviour is an attribute, never a new type. No Coach or critic. No new runtime framework: FastAPI, aiosqlite, httpx, Pydantic, PyYAML, spaCy only. Test-only tools (Playwright) are allowed as an optional extra.
5. **Commit law.** Each `feat:` or `fix:` commit carries code, an error code for a new failure, the user-guide lines, a working-case test and a failing-case test, and `.env.example` for a new variable. One logical change per commit. Commit only the files the work order touched.
6. **Testing scope (Deej's ruling, 13-Nov-2026).** While building: only the tests for the code changed. At a work order's end: the lead runs its "Done when" commands once; that run is the record. A full `python -m pytest` runs once per phase end, and never twice on the same tree: once CI exists (5.6), the pushed tree's CI run **is** the full run and no local full run is made. A passing run is reported in one line.
7. **Never delete.** Move to `C:\Stage\_archive\2026-11-16-borg-route-showcase\` and add a row to `C:\Stage\memory\reference\archive-ledger.md`.
8. **Keys** live only in `.env`. Never print, log or commit a key.
9. **Write files with the Write and Edit tools**, not shell heredocs (they mangle escapes on this machine). Commands are PowerShell or Git Bash.
10. **New file and folder names:** underscores, no spaces.
11. **Standing permissions** (Deej, 15-Nov-2026): local commits on `showcase`; push `showcase` to the private `origin`; merge its pull request into `master` when CI is green and the reviewer has no objection. **Never without Deej's word:** making the repo public, deploying the hosted demo, posting anything, spending past US$20 on OpenRouter.
12. **Deej is not available for questions.** Decide minor or reversible things, add a line to section 8 (Rulings log), continue.

## 3. Stop rule and when to call the planner

- **Stuck:** two failed attempts at the same "Done when" check. Write the blocker under "Blocked" in `CHECKPOINT.md`, move to the next work order that does not depend on it. Notify Deej only when nothing independent is left.
- **Call the planner (Fable)** by writing a "Needs planner" entry in `CHECKPOINT.md` and notifying Deej, only for: a conflict with the Two Laws; a protected file beyond 1.3; Check 3 failing (no job won); a work order stuck twice with nothing independent left.

## 4. Reviewer brief (paste into every review agent)

> You did not write this change. Read the diff for work order <id> and the work order text. Your question is: what here is wrong, missing or unproven? Look for: behaviour the work order asked for that no test proves; a test that would still pass if the feature were removed; a broken invariant from `DESIGN_PRINCIPLES.md`; a claim in docs that the code does not back; a secret or personal path. Do not re-run test suites. Reply either "No objection" or a numbered list of defects, each with file, line and a concrete failing case. Do not suggest style changes.

A work order is done when its "Done when" commands pass **and** the reviewer replies "No objection". Review defects go back to a builder; two rounds at most, then the stop rule.

---

## 5. Facts the builders need

**Verified in the code by the planner:**

| Fact | Where |
|---|---|
| One answerer model is chosen for the whole run and passed to every step | `src/orchestrator/engine.py:1044-1052`, used at `:1117-1118` |
| Steps are built in `_build_task_from_node(node, ..., model_endpoint, model_name, user_instruction, results)` | `engine.py:679` |
| Skill steps return `lib.render(...)` with no model arguments | `engine.py:706-718` |
| `recommended_model` is offered to the planner and never read | `src/prompts/orchestrator.py:194,213`; no reader in `src/` |
| Router: `select_model(matrix, task_type, estimated_input_tokens=1000, estimated_output_tokens=500, quality_requirement=6.0, strategy, specialty_hint, time_cost_per_sec)`; only checked rows; sorts by `effective_cost` | `src/registry/router.py:23` |
| `effective_cost(model, in, out, time_cost_per_sec)`: dollars from `pricing.input_per_token/output_per_token`, plus time cost from `throughput_tps` | `src/registry/cost.py:22` |
| Task types in the matrix: `reasoning, coding, summarize, writing, roleplay, agentic, extraction` | `data/capability_matrix.yaml` |
| Model sources: `lmstudio, ollama, openrouter, custom_key` | `src/registry/models.py:29` |
| Skills carry `recommended_tier` (light, mid, heavy) and `recommended_specialty` (may be `null`, or a word such as `creative` that is not a task type) | `data/skills.yaml` |
| `dollars` is always `None` on the `model_call` event | `src/agents/executor.py:466-468` |
| Envelope update is called without token counts | `engine.py:1187` |
| Orchestrated answers are streamed with `response_text.split()`, which drops newlines | `src/api/chat_completions.py:131` |
| Config pattern: `await get_config("<key>", db_path=db_path)` with env fallback | `engine.py:891` |
| `/` is not routed; `/ui` is a static mount; `main.matrix` is created in `main.py:18` | `main.py` |
| Docker and LM Studio (`lms` CLI) are installed on this machine. Ollama and Playwright are not. Python is 3.14. | checked 15-Nov-2026 |
| `.env` exists with a working OpenRouter key; the key has a US$30 limit, nothing used | checked 15-Nov-2026 |

**Reported by review passes, not checked (rule 2 applies):**

- R1. Answering steps and child runs receive the step's text as the "Original User Instruction" (`src/agents/executor.py:340`, `:535`). Breaks Law 1.
- R2. The engine reads `_shared_matrix` (see `_get_matrix`, `engine.py:77`) while the API uses `main.matrix`; Discover and Get Model Info (`src/api/models.py:105,136`) do not refresh the engine's copy.
- R3. Stage polling for the chat stream is not filtered by run (`chat_completions.py:180-187`).
- R4. `state_op` stores `source_ref` and never resolves it (`engine.py:731`).
- R5. Chat aliases (`borg_route:light`, `:coding`, `:local`...) are parsed (`chat_completions.py:66`) and ignored.
- R6. The planner prompt's "available models" is the first five matrix rows, checked or not (`engine.py:921`).
- R7. The prompt's JSON examples use doubled braces; if substitution is plain string replacement the model sees literal `{{`.
- R8. `POST /runs/{id}/resume` re-runs the whole run; it is not a resume.
- R9. MCP `submit_task` creates a run and never starts it (`src/mcp/server.py:158`).
- R10. Dashboard "Outlier Runs" sorts by prompt length (`ui/dashboard.html:260-270`).
- R11. `ORCHESTRATOR_ENDPOINT`, `ORCHESTRATOR_MODEL_NAME`, `KEY_*`, `ENDPOINT_*` env variables are never read; four Admin settings are shown and ignored.
- R12. Dead code: `validate_outbound`, memory window and compressor, entity worker, `write_scratch`, `log_usage`, `subtasks` and `threads/messages` tables.
- R13. The uncommitted token-retry work: no test for the orchestrator retry or node-level cap; BRT-ORC-018 never recorded; reasoning models already exceed the 16k retry ceiling; ledger says three retries, code does one.

---

## 6. Work orders

Format: **id, model.** Goal. Files. Steps. Done when. Depends on.

### Phase 0: Baseline

**0.1, Sonnet. Settle the uncommitted token-retry work.**
- Files: the ten modified files in `git status`, `.dev/ledger/2026-04-26_token-truncation-retry.yaml`, `docs/errors/error-registry.md`.
- Steps: read the diff; check R13; add the missing error-registry entries for BRT-ANS-009 and BRT-ORC-018; make the ledger's demo text match the code (one retry); add one test for the orchestrator retry and one for the node-level cap; if the retry ceiling can never trigger for reasoning models, record that in the registry entry rather than redesigning. `CLAUDE.md` and `CODING_STANDARD_CORE.md` edits are unrelated: commit them separately as `docs:`.
- Done when: `python -m pytest tests/agents tests/envelopes -q` passes; two commits made.

**0.2, Sonnet. Baseline count and environment.**
- Steps: `pip install -e ".[dev]"` in a fresh venv (`.venv`, git-ignored); one `python -m pytest -q`; record passed/failed/skipped counts and Python version in the build log (section 9). Do not fix unrelated failures: list them.
- Done when: counts are in the build log.

**0.3, Sonnet. Permissions preflight.**
- Steps: confirm the session can write files, run `git`, `python`, `docker`, `lms`, `gh`, and start agents on `sonnet` and `opus`. Anything that prompts for approval goes in `CHECKPOINT.md` under "Needs Deej" immediately, before other work.
- Done when: the list is written (empty is fine).

### Phase 1: Make the claim true

**1.1, Opus. Claim test and stand-in model server.** Depends on 0.2.
- Goal: one end-to-end test that states the headline claim. It must fail today.
- Files: new `tests/e2e/stub_model_server.py`, new `tests/e2e/test_claim_routing.py`, `tests/conftest.py` only if a fixture is needed.
- Steps: read how existing tests fake model calls (`tests/orchestrator/test_engine_openrouter.py`, `tests/registry/test_model_call.py`). Build a scripted stand-in: an in-process fake for the HTTP post that answers by model id and by mode (plan, re_plan, compose, answerer), records every request (model id, endpoint, body), and returns token usage. Use a temp matrix with three checked models at different prices and scores (one top, one mid, one cheap) and a temp database. Script a plan with three steps: one `extraction/light`, one `coding/mid`, one `reasoning/heavy`.
- The test asserts: (a) at least two different models served the steps; (b) the cheap model served the light step and the top model did not; (c) every `model_call` log row has `dollars > 0` and the run digest total equals their sum; (d) the answer returned through the streaming endpoint equals the composed text byte for byte, including newlines; (e) the operator's exact instruction string appears in the system prompt of every step request.
- Done when: the test exists, runs, and fails on (a), (c), (d) at least; the failure output is pasted in the build log. Mark it `xfail(strict=True)` so later work orders flip it; 1.7 removes the marker.

**1.2, Opus. Per-step routing.** Depends on 1.1.
- Files: new `src/registry/node_router.py`; `src/orchestrator/engine.py`; `src/orchestrator/apply_to_each.py` if needed; `.env.example`; `docs/user-guide.md`; `docs/errors/error-registry.md`; new `tests/registry/test_node_router.py`.
- Design (settled):
  - `select_model_for_node(node, skill, matrix, floors, time_cost_per_sec, default_model) -> (ModelInfo, decision: dict)`. Pure function, no I/O.
  - Order: (1) `node["recommended_model"]` if it names a **checked** row with an endpoint: reason `planner_pick`. (2) Otherwise route: `task_type = node.task_type or skill.recommended_specialty or "reasoning"`; map `creative -> writing`; any word not in the seven task types becomes `reasoning` and is noted in the decision. `tier = node.tier or skill.recommended_tier or "mid"`. Floor from config: `leaf_quality_light` 6.0, `leaf_quality_mid` 7.5, `leaf_quality_heavy` 9.0 (env `LEAF_QUALITY_LIGHT|MID|HEAVY`, DB config keys of the same lower-case names, read with the existing `get_config` pattern). Call `select_model(matrix, task_type, est_in, est_out, quality_requirement=floor, time_cost_per_sec=...)`: reason `routed`. (3) If that raises BRT-REG-020, use `default_model` (today's run-wide choice), reason `fallback_default`, and log a warning-severity event with a new code (next free BRT-ORC number).
  - Token estimate: `est_in = max(200, len(instruction + context) // 4)`, `est_out = 500` (per item for `apply_to_each`).
  - `decision` holds: `reason`, `task_type`, `tier`, `floor`, `chosen`, `est_cost`, and `candidates` (model id, score, est. cost, for every row that cleared the floor, cheapest first, at most 8).
  - In `orchestrate_run`: keep choosing `default_model` once; call `select_model_for_node` per ready step and pass the result to `_build_task_from_node`. Skill steps: set `model_endpoint` and `model_name` on the `WrappedTask` that `lib.render` returns. `apply_to_each` children keep inheriting their parent step's model. The orchestrator and compose model selection is unchanged.
- Done when: `python -m pytest tests/registry/test_node_router.py tests/orchestrator -q` passes, with unit tests for each of the three reasons, the `creative` mapping, the unchecked `recommended_model` case, and a skill step getting a model; claim test assertions (a) and (b) pass when run without the xfail marker.

**1.3, Opus. Planner labels (the one approved protected-file edit).** Depends on 1.2.
- Files: `src/prompts/orchestrator.py`, `src/orchestrator/engine.py:921` area, `tests/prompts/`.
- Steps: check R6 and R7 first. Add exactly two lines to the `answerer` shape and the same two to the `apply_to_each` shape, next to `recommended_model`:
  `"task_type": "reasoning | coding | summarize | writing | roleplay | agentic | extraction",`
  `"tier": "light | mid | heavy (the smallest that does this step well)",`
  Nothing else in the prompt changes; no line is removed or reworded. If R6 is real: the "available models" given to the planner becomes the checked rows (id, tags, one-line strengths), most capable first. If R7 is real: fix it in the code that fills the template, not in the prompt text.
- Done when: `python -m pytest tests/prompts -q` passes; a test asserts both labels are present in the rendered prompt and that an unchecked model never appears in the planner's model list; `git diff --stat src/prompts/` shows only `orchestrator.py` with additions only (`git diff --numstat` deletions = 0, unless R7 forces otherwise, in which case record why).

**1.4, Opus. Law 1 repair.** Depends on 1.1.
- Files: `src/agents/executor.py`, `src/orchestrator/engine.py`, `src/envelopes/schema.py` if a field is needed, `src/prompts/base.py` only to read (do not edit), tests in `tests/agents/`.
- Steps: check R1. If real: every step and every child run must reach `build_system_prompt()` with the operator's verbatim instruction as the governance instruction, at any depth. The step's own instruction stays the task text. A child `orchestrate_run` receives the root instruction, not its parent step's text.
- Done when: claim test assertion (e) passes; a depth-2 test proves the root instruction reaches a grandchild step; `python -m pytest tests/agents tests/orchestrator -q` passes.

**1.5, Sonnet. Record why each model was chosen.** Depends on 1.2.
- Files: `src/orchestrator/engine.py`, `src/logging/` writer, `src/api/tasks.py` (run detail), tests.
- Steps: write the `decision` dict from 1.2 with the step's existing event (an `orchestrator_decision` event carrying `envelope_id`; `logs.event_type` has a CHECK constraint, so add no new event type). Add `baseline`: the checked model with the highest `reasoning` score (config key `baseline_model_id` overrides), and `baseline_est_cost` for the same estimate. `GET /runs/{id}` returns, per envelope: `routing` (the decision), and after 1.6 `dollars`, `latency_ms`.
- Done when: a test reads a finished stub run through `GET /runs/{id}` and finds a routing decision with at least one candidate on every routed step.

**1.6, Sonnet. Record cost.** Depends on 1.1.
- Files: `src/agents/executor.py:466`, `src/orchestrator/engine.py:1187` and the orchestrator call sites, `src/db/` envelope and digest writers, `src/api/chat_completions.py` (usage), tests.
- Steps: `dollars = tokens_in * pricing.input_per_token + tokens_out * pricing.output_per_token` from the matrix row of the model that served the call; `0.0` for a model with no pricing (local). Apply to answerer calls, orchestrator calls (plan, re_plan, compose) and tool calls that have a price. Pass token counts to `update_envelope_inbound`. `run_digests` totals come from the logged rows. Fill `usage` in the OpenAI-format reply. Also store `baseline_dollars` per call: the same tokens at the baseline model's prices (1.5).
- Done when: claim test assertion (c) passes; a test proves a local model call logs `dollars == 0.0` and a priced call logs the exact product.

**1.7, Sonnet. Demo-path bugs.** Depends on 1.6.
- Files: `src/api/chat_completions.py`, `src/orchestrator/engine.py`, `src/orchestrator/state_ops.py`, `src/api/models.py`, `main.py`, `ui/chat.html` (dropdown only), tests.
- Steps, each after checking its claim: (a) stream orchestrated answers in chunks that preserve every character, including newlines (split on a fixed size or on `re.split(r"(\s+)")`, never `.split()`); (b) R3: filter stage events by run id; (c) R2: one matrix object shared by API and engine, or an explicit refresh after Discover, Get Model Info and checkbox changes; (d) R4: `state_op` resolves `source` to the upstream step's output list; (e) R5: aliases either drive the router (tier maps to a floor, specialty to a task type, `:local` to `strategy="LOCAL_FIRST"`) or are removed from the dropdown and the docs. Prefer making them work; it is small with 1.2 in place.
- Done when: `python -m pytest tests/api tests/orchestrator -q` passes with one new test per item; the claim test passes with the `xfail` marker removed.

**1.8, Sonnet. Local model servers by address.** Depends on 0.2.
- Files: `src/registry/discovery.py:78,111`, `.env.example`, `docs/install-guide.md`, tests in `tests/registry/`.
- Steps: `LMSTUDIO_BASE_URLS` (comma-separated, default the four localhost ports used today), `OLLAMA_BASE_URL` (default `http://localhost:11434`), `LOCAL_OPENAI_BASE_URLS` (comma-separated, optional: any OpenAI-compatible server, discovered through its `/v1/models`, rows get `source: "custom_key"` plus the local flag from 2.2). Defaults must reproduce today's behaviour exactly.
- Done when: `python -m pytest tests/registry -q` passes; a test proves a non-default address is probed and the default set is unchanged.

**1.9, Sonnet. Law 2 status note.**
- Files: `DESIGN_PRINCIPLES.md`.
- Steps: check R12 for the scratch path. If nothing in production writes scratch, add a short "Status" note under Law 2 saying the rule stands, the machinery is not exercised by any current step, and it is out of the showcase claims. Build nothing.
- Done when: the note is committed as `docs:`.

**1.10, lead. Check 1.**
- Steps: new `scripts/smoke_live.py`: starts no server; calls `run_pipeline` with one real prompt that needs three kinds of step (for example: "From the 12 short product reviews below, extract product and sentiment for each, write a Python function that tallies them, and recommend which product to stock."), with at least three checked OpenRouter models of different price. Reads the database and asserts: two or more models on steps, total dollars above zero and below US$0.50, a routing decision on each step. Prints one summary line and the spend. Refresh the pool first if no three priced models are checked (see 3.1).
- Done when: the script exits 0; the summary line and spend are in the build log; full `python -m pytest -q` passes (phase-end run); push `showcase`.

### Phase 2: On-premise lane

**2.1, Opus. Design note, then attack it.** Depends on Phase 1.
- File: new `docs/design/on_premise_lane.md` (one page).
- Settled by the planner:
  - **The promise, in these words:** "Raw document text is sent only to models on your own machines. Everything sent to a cloud model is listed, and you can read it." It is not a promise that no fact from a document ever leaves: a local step's declared output can be sent on. A second mode, `all_local`, sends nothing to any cloud model.
  - **Documents by reference.** `BORG_DOCS_ROOT` in `.env` names the only folder documents may be read from. A request carries `documents: {"folder": "<relative path>"}` or `{"files": [...]}` on `POST /v1/task` and as an extra field on `POST /v1/chat/completions`. Paths that resolve outside the root are refused with a new error code. The planner receives a manifest only: count, names, sizes, types.
  - **Raw steps.** The runtime, not the planner, marks a step `data_scope: "raw"` when its input includes document content (an `apply_to_each` over the documents, or an answerer given a document). Steps that consume only other steps' outputs are `derived`.
  - **Routing.** With the lane on (`lane_mode: raw_local`), raw steps are offered only local models. `is_local(model)`: source `lmstudio` or `ollama`, or its endpoint host is in `BORG_LOCAL_HOSTS`. No local model clears the floor: the step fails with a new error code. It never falls back to a cloud model. `lane_mode: all_local` applies the same filter to every call, including the planner and compose.
  - **Record.** Every model call logs `left_machine: true|false` and, when true, the full outgoing payload is kept (not the 2000-character cut) so the "what left" list is complete.
  - Config key `lane_mode`: `off | raw_local | all_local`, default `off`. With `off`, behaviour is exactly Phase 1.
- Open to the lead: field names, how document text is loaded and chunked for a step, file types supported first (plain text, Markdown, and PDF text only if it needs no new runtime dependency).
- Steps: write the note; start a reviewer with the question "how does raw document text reach a cloud model despite this design?"; fix every real path found; record the answers in the note.
- Done when: the note is committed with a "Attacks considered" section.

**2.2, Opus. Build the lane.** Depends on 2.1.
- Files: `src/registry/models.py` or a new `src/registry/locality.py`, `src/registry/node_router.py`, `src/orchestrator/engine.py`, `src/agents/executor.py`, `src/api/tasks.py`, `src/api/chat_completions.py`, `src/api/admin.py` (config), logging, `.env.example`, `docs/user-guide.md`, `docs/errors/error-registry.md`.
- Done when: `python -m pytest tests/registry tests/orchestrator tests/api -q` passes, including the privacy test in 2.3.

**2.3, Opus. Privacy test.** Built alongside 2.2 (write it first).
- File: new `tests/e2e/test_privacy_lane.py`, using the stand-in from 1.1 with one local and two cloud models.
- Asserts: (a) `raw_local`: documents contain canary strings that the task's output shape does not ask for; no canary appears in any request to a non-local model; the planner request contains file names and no document text; (b) `raw_local` with no local model: the raw step fails with the lane's error code and zero requests went to a cloud model for that step; (c) `all_local`: zero requests to any non-local model in the whole run; (d) `off`: behaviour equals Phase 1; (e) a path outside `BORG_DOCS_ROOT` (including `..` and an absolute path) is refused; (f) every call's `left_machine` matches the stand-in's own record of where it went.
- Done when: all six pass. **Check 2.** Phase-end full run, push.

### Phase 3: Proof

Spend ceiling US$20 on OpenRouter for this whole phase, counted from logged `dollars`.

**3.1, Sonnet. Refresh the pool.**
- Steps: start the app; `POST /models/discover`; update `data/curated_models.yaml` to about twelve current OpenRouter models chosen from the live list (do not trust model names from memory or from the April file): two top-tier, three mid, four cheap, one coding specialist, plus the web-search scorer; `POST /models/research` to score them (this costs a little: count it); check at least one top, two mid and two cheap rows. Start LM Studio's server (`lms server start`), download one small instruct model that fits 16 GB RAM (file under about 5 GB), discover it, score it, check it.
- Done when: `GET /models/matrix` shows the checked rows with scores and prices; the list and the spend are in the build log.

**3.2, Sonnet. Runner and data.** Depends on 3.1.
- Files: new `bench/README.md`, `bench/run_bench.py`, `bench/jobs/*.yaml`, `bench/data/`, `bench/judge.py`, `bench/checks.py`, tests in `tests/bench/`.
- Steps:
  - `bench/README.md` states the win rule **before any run**: BORG_ROUTE wins a job when its median cost is at most 70% of the direct arm's median cost **and** quality is not worse (job A and D: accuracy within 2 percentage points; B: at least as many machine checks pass; C: the judge does not prefer the direct answer in a majority of comparisons).
  - Data, generated by a seeded script and committed: job A, 200 short support emails with known category (five classes) and urgency (1 to 5); job C, about fifteen documents totalling roughly 150,000 tokens with eight planted facts and two planted contradictions; job D, 60 client notes with canary account numbers and known labels.
  - Arms: `direct` (one call, or the fewest calls that fit, to the baseline top model), `borg_cloud`, and for A and D `borg_local_workers` (raw steps on the LM Studio model). D has no direct arm; it compares `raw_local` with `borg_cloud`.
  - Job B: a small all-Python service spec (SQLite schema, FastAPI handlers, OpenAPI YAML, pytest tests). `bench/checks.py`: SQL executes in SQLite, Python files parse, YAML loads and has `openapi`, `paths`; if Docker is running, the generated tests are run inside `docker run --network none` with a timeout. Never run generated code outside a container.
  - `bench/judge.py`: a model in neither arm; answers shown as "1" and "2" with no arm names; each pair judged twice with the order swapped; a split is a tie. Before judging real answers it must pass a sanity case: a deliberately truncated wrong answer must lose both times, or the judge is replaced.
  - Budget guard: before each run, sum logged dollars for the phase; refuse to start a run if the sum plus that run's estimate would pass US$20. A pre-flight estimate from live prices must come to US$14 or less for the full matrix of runs; otherwise shrink job C first, then repeats from three to two.
  - Each run writes `bench/results/<YYYY_MM_DD>/<job>_<arm>_<n>.json`: cost, wall time, tokens, models per step, the answer, quality measures.
- Done when: `python -m pytest tests/bench -q` passes (runner against the stand-in, budget guard refusing an over-budget run, judge sanity logic); `python bench/run_bench.py --dry-run` prints the run matrix and the estimate.

**3.3, lead (Opus). Run and read.** Depends on 3.2.
- Steps: run the matrix once. If the local arm takes more than 20 minutes a run, do one repeat of it. Write `bench/results/<date>/summary.md`: a table per job (median cost, time, quality, per arm), the win rule's verdict per job, what the numbers do not show, total spend. No number is rounded in BORG_ROUTE's favour. Export one representative BORG run per job for replay (4.5).
- **Check 3.** If BORG_ROUTE wins at least one job: continue. If it wins none: do not tune anything; write "Needs planner" in `CHECKPOINT.md`, notify Deej, and carry on with Phases 4 and 5 using the results as they are. The README and deck then wait for the planner.
- Done when: `summary.md` is committed; spend is in the build log; push.

### Phase 4: Interface

The look is fixed by `docs/design/ui_reference.html`. Open it in a browser first. Copy its tokens, spacing, components and wording style; its numbers are samples.

**4.1, Sonnet. Shared assets.** Depends on Phase 1.
- Files: new `ui/assets/app.css`, `ui/assets/app.js`, `ui/assets/vendor/marked.min.js`, `ui/assets/vendor/purify.min.js`, `ui/assets/logo.svg`.
- Steps: lift the CSS tokens and components from the reference into `app.css`. `app.js`: renders the shell (sidebar, top bar, theme switch stored in `localStorage` inside try/catch), `api()` fetch helper that adds the admin token and shows errors as inline notices (no `alert()`), money, duration and time formatters (local time), safe markdown render. Vendor the two libraries at pinned versions; no CDN link remains anywhere in `ui/`.
- Done when: `Select-String -Path ui\*.html -Pattern "cdn\.|unpkg|alert\("` finds nothing after 4.3; a static check script (`scripts/check_ui_static.py`) confirms every page links `app.css` and `app.js` and has no `<style>` block longer than 15 lines.

**4.2, Opus. Workspace.** Depends on 4.1, 1.5, 1.6, 2.2.
- Files: `ui/ask.html` (replaces `chat.html`), `ui/run.html`, `ui/runs.html` (history, from the dashboard's Runs tab), `main.py` (`/` redirects to `/ui/ask.html`; `/ui/chat.html` redirects too).
- Ask: conversation, a receipt under every answer (steps, models with a local or cloud mark, time, cost, "estimate for the same steps on <baseline model>" labelled as an estimate), a link to the run, thumbs. A documents control that sends a folder reference (lane), with a plain line saying which lane mode is on. Stage progress stays. Chat history stays in `localStorage`.
- Run: header (prompt, status, four totals: cost, time, steps, calls that left the machine); the plan as an inline SVG diagram built from the envelopes and `depends_on` (plan, steps in columns by dependency depth, compose), live while the run is going (reuse `/runs/{id}/stream`); click a step for a side panel: model, tier, task type, "why this model" candidate table from the routing decision, cost, time, tokens, left the machine or not, failure reason in plain words; a timeline of events in sentences; raw JSON only behind a "Details" switch; a "What left this machine" list when the lane is on. Show the run's `failure_reason` when it failed.
- No graph library: plain SVG. Layout must stay readable at 12 steps and collapse `apply_to_each` children into one node with a count.
- Done when: 4.6 passes for these pages; a test serves a stub run and asserts `GET /runs/{id}` carries every field the page reads.

**4.3, Sonnet. Admin.** Depends on 4.1.
- Files: `ui/admin_overview.html`, `ui/models.html`, `ui/keys.html`, `ui/rules.html` and `ui/settings.html` (split from `admin.html`), `ui/skills.html`, `ui/logs.html`, `ui/dashboard.html` (Stats becomes part of Overview and Logs; retire the file by moving it to the archive), `ui/index.html` and `ui/_header.html` (archive), `src/api/admin.py`.
- Steps: Overview with a first-run checklist (key saved, models in pool, local server reachable, admin token set, lane mode), spend tiles, last runs. Models: the pool table with readable score cells and a local or cloud mark. Keys: shows "saved" or "not saved" per provider, never a value; needs a small `GET /keys/status`. Settings: every setting the engine actually reads is editable and saved through `/config`, including the three tier floors, time cost, baseline model and lane mode; settings the engine ignores (R11) are removed from the page and from `.env.example`. Fix R10. R8: rename the button to "Run again" and say what it does.
- Done when: 4.6 passes for these pages; tests cover `GET /keys/status` (never returns a key) and each editable setting taking effect on the next run without a restart.

**4.4, Sonnet. Admin lock.** Depends on 4.3.
- Files: new `src/api/auth.py`, the routers, `.env.example`, `docs/install-guide.md`, tests.
- Steps: `BORG_ADMIN_TOKEN` in `.env`. A FastAPI dependency on every route that changes state for operators: `/keys`, `POST /config*`, `/models/discover`, `/models/research`, `PATCH /models/*`, `POST /skills/*`. Token sent as `Authorization: Bearer`. When the variable is unset: allowed from loopback only, refused from any other address, and the Overview checklist shows it as not set. The Admin pages ask for the token once and keep it in `sessionStorage`. Use `hmac.compare_digest`. Asking questions and reading one's own run stay open.
- Done when: tests prove a wrong or missing token gets 401 on each guarded route, a right one passes, and the unset-token loopback rule holds in both directions.

**4.5, Sonnet. Replay mode.** Depends on 3.3, 4.2.
- Files: new `src/api/replay.py`, `main.py`, `demo/runs/*.json`, `scripts/export_run.py`, `.env.example`, tests.
- Steps: `scripts/export_run.py <run_id>` writes one file holding the run, its envelopes, events with relative timestamps, routing decisions, costs and the answer; it strips keys and absolute local paths. With `BORG_DEMO_MODE=replay`: Ask offers the recorded prompts as buttons and no free text; choosing one plays the recorded stage events and envelope updates over the same stream endpoints at recorded speed (a `speed` factor allowed), then the recorded answer and receipt; every state-changing route returns 403; no model, key or network call is made; a "Replay of a recorded run on <date>" notice shows on every page. Job D's export must contain no canary and no raw document text: the export script refuses otherwise.
- Done when: tests prove replay makes zero outbound HTTP calls (patch `httpx` to raise), state-changing routes return 403, and a replayed run renders the same receipt totals as the source run.

**4.6, Sonnet. Interface check.** Depends on 4.1; run after 4.2, 4.3, 4.5.
- Files: `pyproject.toml` (optional extra `ui_test` with Playwright), new `scripts/check_ui.py`, `docs/design/screens/`.
- Steps: start the app in replay mode on a free port; for each page, at 390 and 1280 pixels wide, light and dark: load, wait for network idle, fail on any console error or failed request, assert the page's required elements (listed in the script, one list per page), assert no horizontal scroll at 390, save a screenshot. On Ask and Run: play one recorded run to the end and assert the receipt and at least three step nodes appear.
- Done when: the script exits 0 and screenshots are saved. **Check 4:** a reviewer agent is given the screenshots and `ui_reference.html` and asked what differs from the reference, what is unreadable, what overflows; "No objection" closes the phase. Phase-end full run, push.

### Phase 5: Package

**5.1, Sonnet. README and licence.** Depends on 3.3, 4.6.
- Files: `README.md`, `LICENSE` (MIT, "Dhananjay Raol, Aliud Inquisito Inc."), `docs/design/screens/` images.
- README order: name and one line ("BORG_ROUTE: Brokered Orchestration, Retrieval and Generation. Breaks a job into steps and sends each step to the cheapest model that can do it, on your machines first."); one screenshot of the Run view; who it is for (companies that want AI on their own machines); see it in two minutes (`docker compose up`, replay demo, no key); the measured numbers copied from `bench/results/.../summary.md` with a link; where it does not pay (short prompts cost more, with the measured figure); how it works in five sentences and one diagram; run it for real (key, pool, first question); the on-premise lane and its exact promise; limits; licence; contact. Every claim carries a claim id from 5.5. Plain words, no superlatives, no numbers that are not in the results.
- Done when: `python scripts/check_claims.py` passes (after 5.5).

**5.2, Sonnet. One-command install.**
- Files: `Dockerfile`, `compose.yaml`, `.dockerignore`, `pyproject.toml` (spaCy to an optional extra `ner`; check nothing imports it at start-up), `docs/install-guide.md`.
- Steps: `compose.yaml` starts the app in replay mode by default with no `.env` needed; a documented override runs it live with `.env`. Data and database on a named volume. The app must start from any working directory (fix the relative paths the review reported: resolve `data/` and `ui/` from the package location).
- Done when: `docker compose up -d --build` then `curl -f http://localhost:8000/health` and a fetch of `/ui/ask.html` succeed; `docker compose down`.

**5.3, Sonnet. Clean the tree.**
- Steps: remove personal paths and names from `docs/install-guide.md`, `CLAUDE.md`, `AGENTS.md` (keep both files, generic and current: real test count, real page paths). Fold the commit law into a new short `CONTRIBUTING.md`. Move `.dev/`, `CODING_STANDARD_CORE.md`, `NOTES.md`, `ui/index.html`, `ui/_header.html`, `ui/dashboard.html`, `ui/chat.html`, `ui/admin.html` to the archive folder (rule 7) with one ledger row; `git rm` them. Replace "BUG-0xx" comment references with a plain description of what the code does.
- Done when: `git grep -n -i "C:\\\\Users\|C:\\\\Stage\|OneDrive\|Deej"` returns nothing in tracked files except `LICENSE`, `README.md` contact and `CHECKPOINT.md`/`BUILD.md`.

**5.4, Sonnet. Docs match the code.**
- Files: `docs/user-guide.md`, `docs/install-guide.md`, `docs/errors/error-registry.md`, `DESIGN_PRINCIPLES.md` (section 8 paths only; the Laws are untouched), `docs/presentation-content.md` (archive it; the deck replaces it).
- Steps: every page, route, setting, env variable and count in the docs is checked against the code. The honest cost section in the install guide is rewritten from the measured results.
- Done when: a script-free read-through by the reviewer finds no doc statement the code contradicts.

**5.5, Sonnet. Claims table and checker.** Depends on 5.1.
- Files: section 7 of this file, new `scripts/check_claims.py`, `tests/test_claims.py`.
- Steps: every factual claim in `README.md`, the deck and `docs/user-guide.md` gets an id (`C01`...) and a row: claim text, backing (a pytest node id, or a results file plus a JSON path and the expected value). The checker fails when a claim id appears in a document without a row, a row's test does not exist, or a results value differs from the number written in the document.
- Done when: `python scripts/check_claims.py` exits 0; removing one row makes it exit 1 (tested).

**5.6, Sonnet. CI.**
- File: `.github/workflows/ci.yml`.
- Steps: on push to `showcase` and `master` and on pull requests: Python 3.11 and 3.13; install; `python -m pytest -q`; `python scripts/check_claims.py`; `python scripts/check_ui_static.py`; a job that builds the image, starts compose in replay mode and checks `/health` and `/ui/ask.html`. No secrets are used.
- Done when: the workflow is green on the pushed `showcase` branch.

**5.7, Sonnet. MCP.**
- Steps: check R9. Fix `submit_task` so the run starts, with a test, or remove MCP from every claim and mark it "experimental" in the docs. Pick the smaller change.
- Done when: test passes or the claim is gone.

**5.8, Opus (fresh context). Acceptance: the stranger test.** Depends on 5.1 to 5.7.
- Brief for the agent: "You are a developer who has never seen this project. Clone `showcase` into a new empty folder outside `C:\Stage\BORG_ROUTE`. Use only `README.md`. Get the replay demo playing and say what you saw. Then follow the 'run it for real' steps as far as they go without a key. Report every place you had to guess, every step that failed, every statement in the README that turned out wrong. Change nothing."
- Done when: the report lists no failed step and no wrong statement; each guess is fixed in the README by a builder and the agent's rerun is clean. **Check 5.** Open the pull request `showcase -> master`; merge when CI is green.

### Phase 6: Presentation

**6.1, Opus. Deck.** Depends on 3.3, 4.6.
- Files: `docs/presentation/` (the deck as one self-contained HTML file, one slide per screen, arrow keys, print stylesheet giving one slide per page), `docs/presentation/borg_route_deck.pdf`, `docs/presentation/video_script.md`.
- Steps: follow `docs/design/deck_outline.md` slide by slide. Same tokens as the interface. Numbers only from `bench/results/.../summary.md`, each with its claim id. Export the PDF by printing the HTML with Playwright (square or 4:5 pages suit a LinkedIn document post). `video_script.md`: 60 to 90 seconds over the replay demo, as a shot list with on-screen action, narration line and duration per shot.
- Done when: `python scripts/check_claims.py` passes with the deck included; the PDF exists with one page per slide; a reviewer given the PDF pages as images finds no overflow, no unreadable text and no claim without an id.

### After the build (Deej's word needed, do not do)

Make the repo public. Deploy the replay demo on the VPS with the HostingerVPS session. LinkedIn post drafts (deej-linkedin-posts and deej-voice skills). Record the video.

---

## 7. Claims table

Filled by 5.5. No claim leaves the building without a row.

| Id | Claim | Backing |
|---|---|---|
| | | |

## 8. Rulings log

| Date | Ruling | By |
|---|---|---|
| 15-Nov-2026 | Audience: companies that want AI on their own machines | Deej |
| 15-Nov-2026 | Additive edit to `src/prompts/orchestrator.py` allowed (work order 1.3 only) | Deej |
| 15-Nov-2026 | Proof spend ceiling US$20 on OpenRouter | Deej |
| 15-Nov-2026 | Hosted watch-only replay demo: yes, on his go-ahead | Deej |
| 15-Nov-2026 | Build first, post later | Deej |
| 15-Nov-2026 | Deej tests nothing; the build verifies itself | Deej |
| 15-Nov-2026 | Opus 5.5 and Sonnet 5.5 at medium build; Fable plans only | Deej |
| 15-Nov-2026 | Commits on `showcase`, push to private origin, merge when green and reviewed | Deej (plan approval) |
| 15-Nov-2026 | One release; lane before proof; MIT; same repo and history; teal and gold look; LM Studio for the local model; dead features cut from claims; BORG acronym into README and install guide | Planner, approved with the plan |
| 15-Nov-2026 | Receipt's "same steps on the top model" figure is an estimate and is labelled so; README and deck numbers come only from measured bench results | Planner |
| 15-Nov-2026 | Job B is an all-Python spec so every output can be machine-checked; generated code runs only in a no-network container | Planner |

## 9. Build log

One line per finished work order: date, id, commit, "Done when" result, reviewer verdict.

| Date | Work order | Commit | Result | Review |
|---|---|---|---|---|
| | | | | |
