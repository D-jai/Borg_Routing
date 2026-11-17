# Error Registry — BORG_ROUTE
> Format: `BRT-[MODULE]-[NNN]`
> Modules: AUTH, API, STG, ING, QRY, CFG, CLI — add more as needed

---

## BRT-REG — Registry Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-REG-001 | Invalid model source | `src/registry/models.py::ModelInfo.validate_source` | Must be one of: `lmstudio`, `ollama`, `openrouter`, `custom_key`. Check the `source` field in your YAML or request body. |
| BRT-REG-002 | Negative pricing value | `src/registry/models.py::Pricing.no_negative_pricing` | `input_per_token` and `output_per_token` must be ≥ 0. Check `data/capability_matrix.yaml` or the model record being upserted. |
| BRT-REG-010 | capability_matrix.yaml not found | `src/registry/capability_matrix.py::CapabilityMatrix.load` | Ensure `data/capability_matrix.yaml` exists in the project root. It ships with the project — if missing, restore from git or a backup zip. |
| BRT-REG-011 | Malformed capability matrix YAML | `src/registry/capability_matrix.py::CapabilityMatrix.load` | The file must be a YAML list of model records. Check for syntax errors (indentation, colons, quotes). Validate with `python -c "import yaml; yaml.safe_load(open('data/capability_matrix.yaml'))"`. |
| BRT-REG-012 | Unknown model_id on update | `src/registry/capability_matrix.py::CapabilityMatrix.update_score / update_throughput` | The model_id does not exist in the matrix. Run discovery first (`POST /models/discover`) or check for typos in the model_id. |
| BRT-REG-013 | Zero or negative throughput_tps | `src/registry/cost.py::effective_cost` | A local model has an invalid throughput value (0 or negative). Run Get Model Info on the Models page to refresh throughput, or hand-edit `data/capability_matrix.yaml`. |
| BRT-REG-020 | No model meets requirements | `src/registry/router.py::select_model` | No **checked** model in the capability matrix satisfies the task_type + quality_requirement + context_length combination. Possible causes: no models are currently checked on the Models page (tick a checkbox); the Get Info pass has not been run so scores are at the default 5.0 (click "Get Model Info"); or the quality bar is too strict. Also raised by `src/orchestrator/engine.py` (`data_too_big_message`) for an answerer skill step (compare_and_rank, verify_claim) whose context data (an earlier step's output, inline items, or the documents it reads) plus its answer is larger than every checked model's context window, or than the default model it would fall back to: the step fails before any model call and re_plan sees the code (a raw lane step: code plus document ids). The data is never truncated. Give the step fewer or shorter entries. |
| BRT-REG-001 (connection) | Connection failure during discovery | `src/registry/discovery.py` | A model source was unreachable. LM Studio and Ollama failures are logged at DEBUG and silently skipped; OpenRouter, local OpenAI-compatible servers (`LOCAL_OPENAI_BASE_URLS`) and custom-key endpoints log BRT-REG-001 at WARNING and are skipped. Check that the server is running and the address is right. |
| BRT-REG-003 | OpenRouter rate limit | `src/registry/discovery.py::discover_openrouter` | HTTP 429 from OpenRouter. Wait and retry, or add an API key to get higher rate limits. |
| BRT-REG-004 | Invalid external API key | `src/registry/discovery.py::discover_openrouter / discover_custom_key` | HTTP 401 from the external provider. Check the API key in `.env` and the Keys page in the UI. |
| BRT-REG-030 | Scorer LLM call failed | `src/registry/scorer.py::_call_scoring_model` | HTTP / timeout / network error calling the scoring LLM. Check `SCORING_MODEL_ENDPOINT`, `SCORING_MODEL`, and `OPENROUTER_API_KEY` in `.env`. |
| BRT-REG-031 | Scorer response unparseable | `src/registry/scorer.py::_parse_scores` | The scoring LLM returned content that is not JSON. Most often happens when `SCORING_MODEL` is a model that cannot web-search. Switch to `perplexity/sonar-pro-search` or another search-enabled model. |
| BRT-REG-032 | curated_models.yaml missing or malformed | `src/registry/discovery.py::_load_curated_ids` | `data/curated_models.yaml` is absent, not a YAML list, or empty. Restore the sample file and list up to ~12 OpenRouter model IDs (`- id: <openrouter-id>`). |

> **Retired error codes:** `BRT-REG-014` and `BRT-REG-015` were emitted by the
> removed benchmark subsystem (judge-model and model-under-test failures) and
> no longer exist in the code.

## BRT-ENV — Envelope Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-ENV-001 | Invalid InboundEnvelope status | `src/envelopes/schema.py::InboundEnvelope` | `status` must be `"resolved"` or `"failed"`. The value `"unresolved"` is not permitted — agents must always commit to an outcome. |
| BRT-ENV-002 | Invalid envelope type | `src/envelopes/schema.py::WrappedTask.validate_type` | `type` must be one of: `orchestrate`, `answerer`, `apply_to_each`, `state_op`. Check the node definition in the orchestrator plan. |
| BRT-ENV-003 | current_depth exceeds max_depth | `src/envelopes/schema.py::WrappedTask.validate_depth` | The orchestrator tried to dispatch a child envelope at a depth greater than the allowed recursion ceiling. Increase `max_depth` in the run config or redesign the plan to be shallower. |
| BRT-ENV-004 | Empty instruction | `src/envelopes/schema.py::WrappedTask.validate_instruction` | `instruction` must be a non-empty string. Every envelope dispatched to a model must carry a concrete task description. |
| BRT-ENV-005 | Invalid SideEffectAction action | `src/envelopes/schema.py::SideEffectAction.validate_action` | `action` must be `"write"` or `"delete"`. Check the `scratch_writes` or `scratch_cleanup` list in the envelope. |
| BRT-ENV-010 | Answerer contract violation (schema bypass) | `src/envelopes/validator.py::validate_inbound` | The inbound envelope status is not `resolved` or `failed`. This should not occur if the schema layer is enforced; check for runtime schema bypasses. |
| BRT-ENV-011 | Inbound output missing required keys | `src/envelopes/validator.py::validate_inbound` | The model's output is missing one or more keys declared in the task's `output_schema`. Check the model's raw response in the run trace, or refine the prompt to better constrain the output shape. |
| BRT-ENV-012 | Failed envelope missing failure_reason | `src/envelopes/validator.py::validate_inbound` | When `status=failed`, `failure_reason` must explain what went wrong. Check the model's response — it may have returned an empty body or an unparseable result. |

## BRT-DB — Database Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-DB-001 | Database connection failure | `src/db/schema.py::create_all` | Cannot open or create the SQLite file. Check that the `data/` directory exists and the process has write permission. |
| BRT-DB-002 | Schema creation failure | `src/db/schema.py::create_all` | The CREATE TABLE statements failed. Check for SQLite version compatibility or corrupted database file. Delete the `.db` file and restart to re-create from scratch. |
| BRT-DB-003 | Constraint violation | `src/db/` modules | A NOT NULL, CHECK, or FK constraint was violated. Inspect the error message for the specific column. |
| BRT-DB-010 | Run not found | `src/db/state.py` | The run_id does not exist. Check that the run was created before it is referenced. |
| BRT-DB-011 | Envelope not found | `src/db/state.py::update_envelope_inbound` | The envelope_id does not exist. Call `save_envelope()` before `update_envelope_inbound()`. |
| BRT-DB-012 | Invalid run status value | `src/db/state.py::update_run_status` | Must be one of: `pending`, `running`, `completed`, `failed`. |
| BRT-DB-015 | Scratch write failed | `src/db/scratch.py::write_scratch` | SQLite insert to the scratch table failed. Check disk space and database file permissions. |
| BRT-DB-016 | Scratch read failed | `src/db/scratch.py::read_scratch_for_level` | SQLite query on the scratch table failed. The database file may be corrupt — check the WAL file. |
| BRT-DB-017 | Scratch delete failed | `src/db/scratch.py::delete_scratch_for_level` | SQLite delete on the scratch table failed. Check database file permissions. |

## BRT-PIP — Pipeline Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-PIP-001 | Run creation failed | `src/pipeline/runner.py::run_pipeline` | Could not insert a new run record. Check database connectivity. |
| BRT-PIP-002 | Orchestration failed | `src/pipeline/runner.py::run_pipeline` | The orchestration engine raised an error. See the nested error message. |
| BRT-PIP-003 | Run completion update failed | `src/pipeline/runner.py::run_pipeline` | Could not update the run status after completion. Check database connectivity. |
| BRT-PIP-004 | Empty user_instruction | `src/pipeline/runner.py::run_pipeline` | user_instruction must not be empty or whitespace-only. |
| BRT-PIP-005 | Run already in terminal state | `src/pipeline/runner.py` | The run is already completed or failed and cannot be re-run. |

## BRT-ORC — Orchestrator Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-ORC-010 | Fold-in check failed (DB error) | `src/orchestrator/fold_in.py::fold_in` | An unexpected DB error during the fold-in residue check. Check database connectivity. |
| BRT-ORC-011 | Child residue detected (informational) | `src/orchestrator/fold_in.py::fold_in` | The child agent left scratch rows behind. This is logged as a `child_residue` event. The residue is cleaned up automatically. Check which model_id is producing residue. |
| BRT-ORC-012 | Cleanup failed after residue detection | `src/orchestrator/fold_in.py::fold_in` | Could not delete residue rows. Check database file permissions. |
| BRT-ORC-001 | Orchestrator model call failed | `src/orchestrator/engine.py::_invoke_orchestrator_model` | HTTP error or timeout calling the orchestrator endpoint. Check that the model server is running. |
| BRT-ORC-002 | Orchestrator returned unparseable JSON | `src/orchestrator/engine.py::_extract_json` | The orchestrator model returned prose instead of JSON. Check the system prompt and model. |
| BRT-ORC-003 | Plan is empty or malformed | `src/orchestrator/engine.py::orchestrate_run` | The plan returned by the orchestrator has no nodes. The run falls back to compose mode. |
| BRT-ORC-004 | Node dispatch failed | `src/orchestrator/engine.py::_dispatch_one` | An individual node failed to dispatch or execute. The node is recorded as failed; the run continues. |
| BRT-ORC-005 | Maximum loop count exceeded | `src/orchestrator/engine.py::orchestrate_run` | The orchestrator looped more than 10 times without composing. Check for runaway re_plan cycles. |
| BRT-ORC-006 | Orchestrator returned action=fail | `src/orchestrator/engine.py::orchestrate_run` | The orchestrator decided the task cannot be completed. See the rationale in the run trace. |
| BRT-ORC-007 | No orchestrator model available | `src/orchestrator/engine.py::_select_orchestrator_model` | No checked model meets any quality bar for orchestration. Check that at least one model is ticked on the Models page, or set `orchestrator_endpoint` in config. |
| BRT-ORC-007-DEGRADED | Quality bar relaxed from 8.0 to 5.1 | `src/orchestrator/engine.py::_select_orchestrator_model` | The default 8.0 bar found no qualifying model, so the orchestrator fell back to the lowest-scored checked row just above the unresearched default (5.0). The run continues but model quality is unknown. Click "Get Model Info" on the Models page to refresh scores. |
| BRT-ORC-008 | Node build failed | `src/orchestrator/engine.py::_build_task_from_node` | A plan node could not be converted to a WrappedTask. Check the node definition in the run trace. |
| BRT-ORC-009 | Compose mode returned no response | `src/orchestrator/engine.py::_handle_compose_result` | Compose gave no final answer, also when asked again (BRT-ORC-029), and no step of the run finished, so there was nothing to salvage (BRT-ORC-030). Check the compose prompt and model. |
| BRT-ORC-013 | OpenRouter API key missing | `src/registry/model_call.py::resolve_model_call` | An OpenRouter row was selected but `OPENROUTER_API_KEY` is not set. Add it to `.env` or uncheck OpenRouter rows on the Models page. |
| BRT-ORC-014 | Model row has no endpoint | `src/registry/model_call.py::resolve_model_call` | The matrix row was never populated with an endpoint (likely left over from a previous install). Run `POST /models/discover` to repopulate. |
| BRT-ORC-015 | Default answerer selection failed; fell back to orchestrator model | `src/orchestrator/engine.py::orchestrate_run` | `select_model` could not find a default answerer row, so the orchestrator model was reused. Warning — the run continues. Check that at least one row at or above the configured quality bar is checked on the Models page. |
| BRT-ORC-016 | Envelope persistence failed | `src/orchestrator/engine.py::orchestrate_run` | `save_envelope` or `update_envelope_inbound` raised. Error logged; the run still completes but the trace may miss an envelope row. Check DB health (`data/borg_route.db`) and disk space. |
| BRT-ORC-017 | `depends_on` id not found in results | `src/orchestrator/engine.py::_build_task_from_node` | The plan referenced an upstream node id that is not part of the current plan (typo or hallucination). The dependent node dispatches anyway with whatever upstream outputs could be resolved. Check the orchestrator's rationale for the stray id. |
| BRT-ORC-019 | No model cleared the quality floor for a step; it ran on the default model | `src/orchestrator/engine.py::orchestrate_run` (decision from `src/registry/node_router.py::select_model_for_node`) | Warning; the run continues. No ticked model scored at or above the floor of the step's tier for its kind of work. The event carries the step id, kind of work, tier and floor. Tick a model that clears the floor, run Get Model Info so scores are not at the default 5.0, or lower `LEAF_QUALITY_LIGHT` / `_MID` / `_HEAVY`. |
| BRT-ORC-023 | Skill lookup failed while routing a step; routed without the skill's hints | `src/orchestrator/engine.py::_skill_for_node` | Warning, written as an `error` event with `"severity": "warning"`, the `node_id`, `skill_id`, `skill_version` and the library's own error (usually BRT-SKL-003 or BRT-SKL-004). The step is routed by its own `task_type`/`tier` labels, or `reasoning`/`mid`, instead of the skill's `recommended_tier` and `recommended_specialty`. Rendering the same skill then usually fails the step with BRT-ORC-008. Check the `skill_id` and `skill_version` the planner wrote against `data/skills.yaml`. |
| BRT-ORC-024 | A step waits on a cycle of steps and can never run | `src/orchestrator/engine.py::_fail_ref_cycles` | Within one plan, steps run in waves until none is ready; if steps are left and every one waits (through `inputs_ref`, a skill's `*_ref` args, `source` or `depends_on`) on another step left, the ones on a cycle fail with this code before re_plan, with no model call. The message names the cycle (`n1 -> n2 -> n1`); an `error` event carries the `node_id` and `cycle`. A step whose `depends_on` names a cycle member then fails with BRT-ORC-025; one that takes its items from it gets BRT-APE-005. On a raw lane step re_plan sees only `{"code", "doc"}`. Fix the plan so no step reads a step that reads it back. |
| BRT-ORC-025 | A step's `depends_on` step failed; the step was not run | `src/orchestrator/engine.py::_orchestrate_run` (`_failed_dependency`) | Any step (answerer, apply_to_each, skill, orchestrate, state_op) whose `depends_on` names a step of this run that failed, including one failed on a cycle (BRT-ORC-024), fails with this code before routing or any model call, so a chain of dependent steps stops paying at the first failure. The message leads with the guidance and ends with the failed step's id, cut to 40 characters (under 200 characters in all); an `error` event carries the `node_id` and `failed_step`. A step that also takes its items from that step (`inputs_ref`, `source`, a skill's `*_ref`) keeps BRT-APE-005. On a raw lane step re_plan sees only `{"code", "doc"}`. Re-plan without the step, or fix the failed one first. |
| BRT-ORC-026 | A re_plan step reused the id of a step that already ran; it was renamed | `src/orchestrator/engine.py::_rename_reused_ids` (called from `_orchestrate_run`) | Warning, written as an `error` event with `"severity": "warning"` and `"renamed": [{"from", "to"}]`. Planner ruling 20 (R1): a re-planned step whose id is already in the run's results (typically a retry named like the failed step) used to be taken as done and never ran. It now gets `<id>_2` (`_3` ... unique across the run) and runs. Other steps of the same re_plan that name the old id (`depends_on`, `inputs_ref`, `source`, `source_ref`, a skill's `*_ref`) are pointed at the new id, except where that would close a wait cycle in the batch (the renamed step waits on the referring step): that ref keeps the earlier result (R1-R4 review); the renamed step's own refs to its old id, and refs to ids not redefined in the batch, keep reading the earlier results, which keep their ids. Ids are compared as text, so `1` and `"1"` are one id. Nothing to fix unless the planner keeps reusing ids; read its rationale. |
| BRT-ORC-027 | Non-object entries dropped from a plan or re_plan step list | `src/orchestrator/engine.py::_object_nodes` | Warning, written as an `error` event with `"severity": "warning"`, the `mode` (`plan` or `re_plan`), up to ten `dropped` entries (repr, 80 characters) and the `dropped_count` / `kept_count`. Planner ruling 20 (R3): job C's re_plan ended `next_nodes` with the string `"rationale: "`, which crashed the run. The valid siblings now run. When nothing valid is left the reply counts as having no steps: a plan takes the existing no-plan path (compose, or BRT-ORC-003 with documents), a re_plan goes to compose; never an empty plan. A `nodes` value that is not a list is dropped whole. |
| BRT-ORC-028 | Compose reply in the wrong shape; its final text was used | `src/orchestrator/engine.py::_handle_compose_result` (`_compose_text`) | Informational, on an `orchestrator_decision` event with the `field` the text came from and any `compose_action`. Planner ruling 20 (R2, step 1): a compose reply whose final answer is not a plain string `response` (for example `{"action": "compose", "response": "..."}`, `direct_answer`, `final_answer`, `answer`, `text`, `content`, `output`, `result`, `message`, or a list/object under one of them, written out as indented JSON) is accepted with no second call; `rationale` is never taken as the answer. A reply that carries `action` or `next_nodes` gives its answer only from `response`, `direct_answer`, `final_answer` or `answer` (R1-R4 review); otherwise compose is asked again (BRT-ORC-029). The run's `answer_kind` is `composed`. Nothing to fix; if it is frequent, the compose model mixes up the modes. |
| BRT-ORC-029 | Compose reply had no final answer; compose was asked once more | `src/orchestrator/engine.py::_handle_compose_result` | Informational, on an `orchestrator_decision` event with the first reply's `compose_action`. Planner ruling 20 (R2, step 2): a compose reply with no final text and no `action: fail` (seen: `{"action": "compose"}` or `{"action": "continue", ...}` in all four BRT-ORC-009 runs of bench stage 1) is followed by one more compose call whose user message is `Mode: compose. Your last reply had no final answer. Reply with {"response": "<the final answer>"} only.` Its reply is read the same way; if it has no final text either (or the call fails), BRT-ORC-030. |
| BRT-ORC-030 | Compose gave no final answer twice, or the compose call failed; the finished steps' results were returned | `src/orchestrator/engine.py::_handle_compose_result`, `_orchestrate_run` (`_salvage_finished_results`) | Warning, written as an `error` event with `"severity": "warning"` and the finished `steps`. Also when the first compose call itself raises (BRT-ORC-001/002, a transport error; the event carries `err`), with no second call (the lead's overrule, R1-R4 review); with no finished step that run fails with the call's error. Planner ruling 20 (R2, step 3): the answer starts with a plain note that the final write-up failed, then one section per finished step (its answer text, or its output as JSON; an apply_to_each step lists each item's output and says how many failed). The run completes and `GET /runs/{id}` shows `run.answer_kind = "salvaged"` (a composed answer shows `composed`). In `raw_local` and `all_local` a raw step's output appears only if it passed the output guard, an item is shown only when it is a document part id, and a withheld step is named with no content. If no step finished, the run fails with BRT-ORC-009 as before. Check the compose model and its cap (`ORCHESTRATOR_MAX_TOKENS`). |
| BRT-ORC-031 | Compose declined to answer; the rationale is returned as the answer | `src/orchestrator/engine.py::_handle_compose_result` | Informational, on an `orchestrator_decision` event (`"code": "BRT-ORC-031"`, with the `rationale`). The compose reply was `action: fail`; the run completes, the answer is the rationale, and `GET /runs/{id}` shows `run.answer_kind = "declined"`. Replaces the old decision code `BUG-020-COMPOSE-DECLINED`. Not a fault: read the rationale; if the request was fine, check the compose model. |
| BRT-ORC-018 | Orchestrator response cut off by the token cap; retried once at double the cap | `src/orchestrator/engine.py::_invoke_orchestrator_model` | Informational. The first reply ended with `finish_reason=length`, so the call was repeated once with `max_tokens` doubled (ceiling 16000). Recorded on that call's `orchestrator_decision` event as `"length_retry": true, "code": "BRT-ORC-018"` (plus `retry_max_tokens`); the event's tokens are the sum of both attempts, `finish_reason` is the retry's and `first_finish_reason` is `length`. Every `orchestrator_decision` event (plan, re_plan, compose, the JSON-only retry) carries the call's `finish_reason` (planner ruling 20), so a cut-off reply can be found from the database. When it triggers: only when the first cap is below 16000. The first cap is `ORCHESTRATOR_MAX_TOKENS` (code default 2000), or for a reasoning-family model four times that with a minimum of 8000. So with the variable unset a reasoning model starts at 8000 and is retried at 16000; a plain model starts at 2000 and is retried at 4000. When it cannot trigger: the first cap is already 16000 or more, e.g. a reasoning model with `ORCHESTRATOR_MAX_TOKENS` of 4000 or more (the `.env.example` value 6000 gives 24000), or a plain model with 16000 or more. Raise `ORCHESTRATOR_MAX_TOKENS` if plans are still cut off. |

## BRT-APE — apply_to_each Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-APE-001 | Items not resolvable | `src/orchestrator/apply_to_each.py::_resolve_items` | `dependency_input` must contain `'items'` (list) or `'inputs_ref'` (list). Check the orchestrator's plan node. |
| BRT-APE-002 | Child envelope dispatch failed | `src/orchestrator/apply_to_each.py::_run_one` | Individual failures go to `failures[]`, not raised. Check the run trace for the specific item's failure_reason. |
| BRT-APE-003 | Invalid concurrency_limit | `src/orchestrator/apply_to_each.py::expand_and_run` | Must be a positive integer. Default is 20. |
| BRT-APE-004 | Depth limit exceeded | `src/orchestrator/apply_to_each.py::expand_and_run` | `current_depth >= max_depth`. Increase `max_depth` or flatten the plan structure. |
| BRT-APE-005 | No items to process | `src/orchestrator/apply_to_each.py` (marked by `src/orchestrator/engine.py::_build_task_from_node`) | Items list is empty. Also: `inputs_ref` / a skill's `*_ref` given as an empty list, or naming a step of this run that failed (the message ends with that step's name, cut to 40 characters). No model call is made. Check the upstream state_op or input reference. On a raw (on-premise lane) step re_plan sees only `{"code", "doc"}`; the message is in the local trace. |
| BRT-APE-006 | Ref names no step | `src/orchestrator/apply_to_each.py::_resolve_items` (marked by `src/orchestrator/engine.py::_build_task_from_node`) | An apply_to_each step (plan-written, or a skill such as `extract_fields`) has `inputs_ref` / `items_ref` (any skill `*_ref`, e.g. `failed_items_ref`) set to a name that is not the id of any step in this run, and carries no items of its own. A child run does not see its parent's step ids, so a child step naming a parent step fails here too. Also: the ref is neither a step id nor a list (a number, an object): "must be a step id or a list of items". The text leads with the guidance (a list from the user's message belongs in `items`; `inputs_ref`/`items_ref` must name a step of this run, not a parent's) and ends with `Not a step id: '<name, cut to 40 characters>'`, so all of it fits the 200 characters re_plan is shown. The step fails before any model call; re_plan sees it as an ordinary step failure (code+doc only on a raw lane step). Usually the list is in the operator's message: it belongs in the step's `items` (one string per item; for a skill, `items` in `skill_args` or the `*_ref` given as a list). A ref naming a step of the same plan is not this failure: the step waits for that step. `documents` is reserved for the run's documents only while the on-premise lane is on; with the lane off it is an ordinary name, so with no step of that id it fails here, and a step with id `documents` is waited for like any other. |

## BRT-TOOL — Runtime Tool Dispatcher (BRT-WEB-01)

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-TOOL-000 | `tool_request` references an unknown tool | `src/envelopes/validator.py::validate_outbound` | The plan named a tool not in `TOOL_REGISTRY`. Typo or orchestrator hallucination. Check the registered tool names (currently only `web_search`). Task fails before dispatch, caller handles via `re_plan`. |
| BRT-TOOL-001 | Tool registered but has no runnable provider | `src/tools/registry.py::_lookup_or_raise` | Every configured provider is missing credentials. For `web_search`, this means both `TAVILY_API_KEY` and `OPENROUTER_API_KEY` are absent. Add at least one key to `.env`. |
| BRT-TOOL-002 | Tool runtime failure after all providers exhausted | `src/tools/web_search.py::WebSearchTool.run` / `src/agents/executor.py` | The tool ran but every provider returned an error (HTTP, timeout, unparseable response). Check provider status, API keys, and network. The envelope returns `status=failed` and the orchestrator can re-plan with a different query. |
| BRT-TOOL-DEGRADED | Primary provider down, fallback answered | `src/tools/web_search.py::WebSearchTool._note_degraded` | Informational warning, logged once per process. The run continues normally with results from the fallback provider. Check primary provider's dashboard / status page. |

## BRT-SKL — Skills Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-SKL-001 | skills.yaml not found | `src/skills/library.py::SkillLibrary.load` | Ensure `data/skills.yaml` exists. It ships with the project — restore from git or zip backup if missing. |
| BRT-SKL-002 | Malformed skills.yaml | `src/skills/library.py::SkillLibrary.load` | The file must be a YAML list. Validate with `python -c "import yaml; yaml.safe_load(open('data/skills.yaml'))"`. |
| BRT-SKL-003 | Skill not found | `src/skills/library.py::SkillLibrary.get` | The skill_id does not exist in the loaded library. Check the orchestrator's skill invocation. |
| BRT-SKL-004 | Skill version not found | `src/skills/library.py::SkillLibrary.get` | The requested version does not exist for this skill. Check available versions in data/skills.yaml. |
| BRT-SKL-005 | Missing required arg | `src/skills/library.py::SkillLibrary.render` | A required arg for this skill was not provided. Check the skill's `required_args` section. |
| BRT-SKL-006 | Invalid envelope_type in skill | `src/skills/library.py::SkillLibrary.render` | The skill's `envelope_type` is not one of the valid types. Check data/skills.yaml. |
| BRT-SKL-007 | skill_usage_log write failed | `src/skills/library.py::log_usage` | SQLite write failed. Check disk space and database file permissions. |
| BRT-SKL-008 | Skill render incomplete | `src/skills/library.py::SkillLibrary.render`; `src/orchestrator/engine.py::_answerer_skill_data` | A `{name}` in any templated field of the skill (instruction, context, output_schema, state_query, inputs_ref, concurrency_limit, trigger_reason) is neither an arg nor derivable from one (`<arg>_formatted` from a non-empty `<arg>`, `<arg>_instruction` from `<arg>`), or the arg it derives from is empty (e.g. `fields: []`); a "derived from fields" schema meets a nameless, duplicate or empty field; a schema arg is not a JSON object; a list arg given as a comma string has an empty entry; `concurrency_limit` is not a positive whole number (12, 12.0 and "12.0" are 12; 2.5 is refused); a list arg given as a string that looks like JSON (starts with `[` or `{`) is not valid JSON, or is a JSON object for a list arg that is not `list[dict]`; an arg whose type hint has no `\| null` is null; or a step's `extra_fields` arg has a nameless or duplicate field, an entry that is neither a name nor an object, or is a string that looks like JSON but is not; or an answerer skill that reads data (compare_and_rank, verify_claim) has no inline items and its `*_ref` names no finished step (or a failed one). Render fails before any model call; the engine fails the step with BRT-ORC-008 and re_plan sees the reason. Check the step's `skill_args` against the template. |

## BRT-SOP — State Operators Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-SOP-001 | Unknown operator name | `src/orchestrator/state_ops.py::dispatch_state_op` | Must be one of: `filter`, `count`, `group_by`, `sample`. Check `dependency_input['op']` in the task. |
| BRT-SOP-002 | Missing required param | `src/orchestrator/state_ops.py::op_*` | Each operator requires specific params. `filter` needs `key` + `value`; `group_by` needs `key`; `sample` needs `n`. |
| BRT-SOP-003 | Items not found in dependency_input | `src/orchestrator/state_ops.py::_get_items` | The state_op task must have `dependency_input['items']` — a list of dicts to operate on. When the node names a `source` step, that step must already have produced a list (`items`, `results`, `list` or `result`); otherwise this error is raised. An empty list is a valid list and does not raise it. |
| BRT-SOP-004 | Invalid filter expression | `src/orchestrator/state_ops.py::op_filter` | The filter comparison raised an error. Check that the key and value types are compatible. |
| BRT-SOP-005 | group_by key not found in items | `src/orchestrator/state_ops.py::op_group_by` | One or more items in the list don't have the specified `key`. Ensure all items have the key or pre-filter. |

## BRT-ANS — Answerer Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-ANS-001 | Model endpoint not configured | `src/agents/executor.py::_execute_answerer` | The WrappedTask has no `model_endpoint`. Ensure the orchestrator sets `model_endpoint` when building answerer tasks (usually from the capability matrix). |
| BRT-ANS-002 | HTTP error calling model endpoint | `src/agents/executor.py::_call_model` | The model returned a non-2xx response. Check that the model server is running at the endpoint URL. |
| BRT-ANS-003 | Cannot build InboundEnvelope from model response | `src/agents/executor.py::_execute_answerer` | The model returned JSON that doesn't match the InboundEnvelope schema. Check the model's raw output in the run trace. |
| BRT-ANS-004 | No valid JSON in model response | `src/agents/executor.py::_extract_json` | The model returned prose instead of JSON. The task becomes a failed envelope; check the answerer prompt and model. |
| BRT-ANS-005 | Inbound output failed contract validation | `src/agents/executor.py::_execute_answerer` | The model's output was missing required keys from the output_schema. The task becomes a failed envelope. See BRT-ENV-011 for more detail. |
| BRT-ANS-006 | Model call timed out | `src/agents/executor.py::_call_model` | The model did not respond within 120 seconds. Check model server load or increase the timeout. |
| BRT-ANS-007 | Governance load failed | `src/agents/executor.py::_execute_answerer` | Could not read global_rules or admin_rules_ledger from the config table. Check database connectivity. |
| BRT-ANS-008 | Unexpected executor error | `src/agents/executor.py::execute` | An unexpected exception occurred. Check the logs for the full traceback. |
| BRT-ANS-009 | Answer cut off by the token cap, after one retry at double the cap or with no retry when the cap is already at the ceiling | `src/agents/executor.py::_execute_answerer` | The model stopped with `finish_reason=length` and the step fails with this code instead of the misleading BRT-ANS-004. The first cap is `ANSWERER_MAX_TOKENS` (code default 2000; for a reasoning-family model four times that, minimum 8000), raised to the step's `max_output_tokens` (default 4000) when that is larger. If the first cap is below 16000 the call was repeated once at double the cap (ceiling 16000) and was cut off again; the message reads "even after one retry at double the cap (A -> B)". If the first cap is already 16000 or more (e.g. a reasoning model with `ANSWERER_MAX_TOKENS=6000`, giving 24000, or a step with `max_output_tokens` of 20000) no retry is made and the message says "no retry". The `model_call` event's decision carries `retried`, `first_max_tokens` and `retry_max_tokens`. Remedies: raise `ANSWERER_MAX_TOKENS`, or split the step. A plan step may carry its own `max_output_tokens`, but the planner prompt does not mention that field, so the planner will not set it on its own (ruling 16-Nov-2026: the prompt is not changed for it); in practice the field is set only by hand-written plans. |
| BRT-ANS-010 | The model declined or reported failure | `src/orchestrator/engine.py::_stage_output_for_prompt` | The step's model returned `status: failed` with its own `failure_reason`. That text is the model's, so it is never read as a code: plan and re_plan see `{"code": "BRT-ANS-010", "message": <first 200 characters>}`, compose sees the code only. (On a raw lane step the code is BRT-LANE-004 and no message.) Read the step's model reply in the trace; reword the step or pick a stronger tier. |
| BRT-ANS-011 | Local connection pool exhausted | `src/agents/executor.py::_post_chat_completion`, `src/orchestrator/engine.py::_invoke_orchestrator_model` | A model call waited for a free connection in the shared HTTP pool (`POOL_MAX_CONNECTIONS` in `src/lane/outbound.py`) until its timeout and gave up (`httpx.PoolTimeout`). The model was not slow: too many calls were in flight at once. It is not BRT-ANS-006 (the model did not answer in time). `apply_to_each` caps its fan-out at `MAX_CONCURRENCY` (256), so one step cannot cause this; look for many parallel runs, or lower `concurrency_limit`. |

## BRT-LOG — Logging Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-LOG-001 | Invalid event_type value | `src/logging/writer.py::write_event` | Must be one of: `model_call`, `orchestrator_decision`, `child_residue`, `escalation`, `deferral`, `reorchestration`, `error`. |
| BRT-LOG-002 | Log write failed | `src/logging/writer.py::_write_event_task` | Printed to stderr (never re-raised). Check disk space and database file permissions. This is non-blocking — the caller continues regardless. |

## BRT-API — API Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-API-001 | Malformed request body | `src/api/models.py::add_key` | `name` and `api_key` are required fields. Check the request JSON body. |
| BRT-API-002 | Model not found in matrix | `src/api/models.py::set_model_checked` | The model_id does not exist in the capability matrix. Run `POST /models/discover` first, check the model_id for typos, or add the row via `data/curated_models.yaml`. |
| BRT-API-010 | Malformed chat completions request | `src/api/chat_completions.py` | The request body is not valid JSON or does not contain required fields (model, messages). Check the request format. |
| BRT-API-011 | Model not found (bypass mode) | `src/api/chat_completions.py::_bypass_to_model` | The specified model_id is not in the capability matrix. Run discovery first. |
| BRT-API-012 | Pipeline/bypass execution failed | `src/api/chat_completions.py` | The orchestration pipeline or bypass model call failed. Check the run trace for details. |
| BRT-API-013 | Bypass streaming relay failed | `src/api/chat_completions.py::_stream_bypass_relay` | The underlying model endpoint returned a non-200 status or disconnected during SSE relay. The error text is injected into the final delta chunk so the client still receives a valid SSE stream. Check the model endpoint is running and supports streaming. |
| BRT-API-020 | Malformed task request | `src/api/tasks.py::submit_task` | `instruction` is required and must not be empty. |
| BRT-API-021 | Run not found | `src/api/tasks.py::get_run_state` | The run_id does not exist. Check that the run was submitted first. |
| BRT-API-022 | Task submission failed | `src/api/tasks.py::submit_task` | Could not create the run record. Check database connectivity. |
| BRT-API-023 | Run fetch failed | `src/api/tasks.py::get_run_state` | Could not retrieve the run from the database. Check database connectivity. |
| BRT-API-030 | Invalid JSON-RPC request | `src/mcp/server.py::mcp_endpoint` | The request body is not valid JSON or is not a JSON-RPC 2.0 object. Check the request format. |
| BRT-API-031 | MCP method not found | `src/mcp/server.py::mcp_endpoint` | The JSON-RPC method is not supported. Valid: `initialize`, `tools/list`, `tools/call`. |
| BRT-API-032 | MCP tool not found | `src/mcp/server.py::_handle_tools_call` | The tool name is not in the catalogue. Use `tools/list` to see available tools. |
| BRT-API-033 | MCP tool execution failed | `src/mcp/server.py::_handle_tools_call` | The tool raised an error. Check the `message` field in the JSON-RPC error response. |
| BRT-API-034 | Missing required tool argument | `src/mcp/server.py::_tool_*` | A required argument was not provided. Check the tool's `inputSchema`. |
| BRT-API-024 | Resume failed | `src/api/tasks.py::resume_run_endpoint` | Could not resume the run. Check that the run exists and is not completed. A run that is still running is refused with 409; wait for it to finish or fail. |
| BRT-API-025 | Run list failed | `src/api/tasks.py::list_runs` | Could not fetch the list of runs from the database. Check database connectivity. |

## BRT-ORC — Orchestrator Module (Slice 3 additions)

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-ORC-020 | Max recursion depth reached — orchestrate node demoted to answerer | `src/agents/executor.py::execute` | The orchestrate node was at `max_depth`. The depth limit is fixed in `src/orchestrator/engine.py` (`MAX_DEPTH`) and cannot be changed from Admin; flatten the plan. |
| BRT-ORC-021 | Child run creation failed for orchestrate node | `src/agents/executor.py::execute` | Could not create a child run in the database. Check database connectivity. |
| BRT-ORC-022 | Child orchestration run failed | `src/agents/executor.py::execute` | The recursive orchestrate_run call raised an error. Inspect the child run trace for details. |

## BRT-DB — Database Module (Slice 3 additions)

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-DB-020 | Run cannot be resumed (already completed) | `src/db/state.py::resume_run` | Only `pending`, `running`, or `failed` runs can be resumed. Check the run status first. |

## BRT-ADM — Admin Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-ADM-001 | Missing required field in request body | `src/api/admin.py` | `key` (for config) or `text` (for rules) is required and must not be empty. |
| BRT-ADM-002 | Rule not found | `src/api/admin.py::retire_rule` | The rule_id does not exist in the ledger. Use `GET /config/rules` to see valid rule IDs. |
| BRT-ADM-003 | Rule already retired | `src/api/admin.py::retire_rule` | The rule has already been retired. No action needed. |
| BRT-ADM-004 | Config read failed | `src/api/admin.py` | SQLite read error. Check database file permissions and connectivity. |
| BRT-ADM-005 | Config write failed | `src/api/admin.py` | SQLite write error. Check database file permissions and disk space. |
| BRT-ADM-006 | Invalid setting value | `src/api/admin.py` | `POST /config` refused a value that is not valid for that setting (not a number, out of range, not one of the allowed choices) a setting that is read from `.env` only (`ORCHESTRATOR_MAX_TOKENS`, `ANSWERER_MAX_TOKENS`), a key that is not a setting, a planner endpoint that is not an http(s) address, or an `admin_rules_ledger` that is not a list of rule objects. Send a valid value, or edit `.env` and restart. |

## BRT-MEM — Memory Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-MEM-001 | Thread not found | `src/memory/window.py` | The thread_id does not exist. Create a thread first via `create_thread()`. |
| BRT-MEM-002 | Window extraction failed | `src/memory/window.py::extract_window` | SQLite error while reading messages. Check database connectivity. |
## BRT-API — API Module (Slice 5 additions)

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-API-026 | Invalid rating value | `src/api/tasks.py::rate_run` | rating must be 1 (thumbs up) or -1 (thumbs down). |
| BRT-API-027 | Run not found for rating | `src/api/tasks.py::rate_run` | The run_id does not exist or is malformed. Check the run was created before rating it. |
| BRT-API-028 | Stream run not found | `src/api/tasks.py::stream_run_state` | The run_id does not exist or is not a valid UUID. Submit the task first, then open the stream. |
| BRT-API-029 | Stream DB error | `src/api/tasks.py::stream_run_state._event_generator` | SQLite error during SSE polling. The event data will contain the error message. Check database connectivity. |
| BRT-API-040 | Logs fetch failed | `src/api/logs.py::list_logs` | SQLite error while reading logs. Check database connectivity. |
| BRT-API-041 | Entities fetch failed | `src/api/logs.py::list_entities` | SQLite error while reading log_entities. Check database connectivity. |
| BRT-API-042 | Cost summary failed | `src/api/logs.py::cost_summary` | SQLite aggregation error. Check database connectivity. |
| BRT-API-043 | Export failed | `src/api/logs.py::export_logs` | SQLite error during JSONL export. Check database connectivity. |
| BRT-API-044 | Outlier runs failed | `src/api/logs.py::outlier_runs` | SQLite error while ranking root runs by cost or duration. Check database connectivity. |
| BRT-API-045 | Admin token missing or wrong | `src/api/auth.py::require_admin` | 401. `BORG_ADMIN_TOKEN` is set on the server and the request sent no `Authorization: Bearer <token>` header, or a different token. Enter the token again on the Admin page, or send the header. |
| BRT-API-046 | Remote admin refused, no admin token set | `src/api/auth.py::require_admin` | 403. The server has no `BORG_ADMIN_TOKEN`, so admin routes answer only to this computer (127.0.0.0/8, ::1). Set `BORG_ADMIN_TOKEN` in `.env`, restart, then send it as a bearer token. uvicorn trusts `X-Forwarded-For` from 127.0.0.1, so a same-host proxy that sets the header is handled; a proxy that sends no such header or passes the client's own through makes every visitor look local. Set the token whenever the app is reachable from another machine. |
| BRT-API-047 | Admin token misconfigured | `src/api/auth.py::require_admin` | 500. `BORG_ADMIN_TOKEN` holds a character outside printable ASCII (an accent, a space). No header could match it. Use ASCII only, restart, and check `admin_token_valid` in `GET /keys/status`. |
| BRT-API-050 | Skills load failed | `src/api/skills.py` | Could not load skills.yaml. Check that data/skills.yaml exists. |
| BRT-API-051 | Skill not found | `src/api/skills.py` | The skill_id does not exist in the library. Check the skill_id. |
| BRT-API-052 | New-version write failed | `src/api/skills.py::create_skill_version` | Could not write the updated skills.yaml. Check file permissions. |
| BRT-API-053 | Invalid new-version payload | `src/api/skills.py::create_skill_version` | Required fields are missing or malformed. |

## BRT-LOG — Logging Module (Slice 5 additions)

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-LOG-010 | spaCy model not available | `src/logging/ner_worker.py::_load_nlp` | Run `python -m spacy download en_core_web_sm` to install the model. |
| BRT-LOG-011 | NER processing failed for a payload | `src/logging/ner_worker.py::process_batch` | A payload caused spaCy to error. The payload is marked processed and skipped. Check the payload content. |
| BRT-LOG-012 | DB write failed after NER | `src/logging/ner_worker.py::process_batch` | Could not insert entities/relations. Check disk space and database file permissions. |
| BRT-LOG-013 | NER worker poll error | `src/logging/ner_worker.py::run_ner_worker` | An unexpected error in the poll loop. Logged to stderr; worker continues. |

## BRT-MEM — Memory Module (Slice 4 additions)

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-MEM-003 | Model call failed during compression | `src/memory/compressor.py::compress_inactive_turns` | The compressor model endpoint is unreachable or returned a non-2xx response. Check that the model server is running at the configured endpoint. Also raised on timeout (120 s). |
| BRT-MEM-004 | Compression response parse failed | `src/memory/compressor.py::compress_inactive_turns` | The model returned an empty or structurally unexpected response. Check the model and its output. Re-run compression once the model is healthy. |
| BRT-MEM-005 | Config write failed after compression | `src/memory/compressor.py::compress_inactive_turns` | Could not persist the rolling summary to the config table. Check database file permissions and disk space. |

## BRT-LANE — On-premise lane (work order 2.2)

Design: `docs/design/on_premise_lane.md`. Every lane failure names codes, model ids, URLs, document ids and relative paths only, never document text or an absolute path. Where a step fails, what reaches re_plan, compose or a parent is `{"code": "BRT-LANE-...", "doc": "<document ids>"}`; the full error stays in the local log.

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-LANE-001 | Document path refused, documents root unset or unsafe, or a file unreadable | `src/lane/documents.py::load_documents`, `docs_root`, `check_path`, `read_verified` | Nothing is read and no run starts. Set `BORG_DOCS_ROOT` in `.env` to a folder that holds only documents (not `.`, not the application folder or a folder that contains it). Name documents by a path relative to that folder: absolute paths, `C:x.txt`, `\x.txt`, UNC (`\\server\share`) and `\\?\` / `\\.\` prefixes are refused, and so is any path, symlink or junction that resolves outside the root. A file replaced between the check and the read is refused; send the request again. The message names only the relative path you sent. |
| BRT-LANE-002 | No local model clears the floor for a step that must stay local (or, in `all_local`, for the planner) | `src/registry/node_router.py::select_model_for_node`, `src/orchestrator/engine.py::_select_orchestrator_model` | The step is not sent anywhere: there is no fallback to the default or a cloud model. Tick a local model (Ollama, LM Studio, or a server listed in `BORG_LOCAL_HOSTS`) that scores at or above the step tier's floor for its kind of work, run Get Model Info so its scores are real, or lower `LEAF_QUALITY_*`. In `all_local` the planner needs a local model too (the orchestrator bar degrades to 5.1, local models only). A `-cloud` Ollama tag, a row discovery marked `remote`, or a model left out of `lane_local_allowlist` never counts as local. A document larger than every local model's context also ends here. |
| BRT-LANE-003 | A call that must stay local was about to go to a non-local endpoint; blocked | `src/lane/outbound.py::send_outbound` (the call guard), raised through `src/agents/executor.py` and `src/orchestrator/engine.py::_invoke_orchestrator_model` | Nothing was sent. This indicates a defect: marking or routing chose a model for a raw step (or any step in `all_local`) whose real address is not local, or a pinned endpoint or a row's `endpoint` points elsewhere. Check the step's `routing` and the error event's `url` and `destination`. An unknown model id at a non-local address is judged by its address, never its label. |
| BRT-LANE-004 | A local step's output was withheld by the output guard | `src/lane/context.py::guard_envelope` (called from `src/agents/executor.py::_execute_raw_answerer`) | The step (or the item) fails; its output is never placed in another request. Either the serialised output was longer than `lane_max_output_chars` (default 1500) or it copied a run of `lane_max_verbatim_chars` (default 120) characters from the document. Ask the step for short structured fields instead of text, or raise the limits if the refusal is false. A step that reads a withheld output is raw itself and stays local. |
| BRT-LANE-005 | Not available in this lane mode | `src/lane/context.py::prepare_lane`, `src/lane/settings.py::load_lane_settings`, `src/agents/executor.py::_execute_answerer`, `src/orchestrator/engine.py`, `src/api/chat_completions.py`, `src/api/models.py` | One of: documents sent while `lane_mode` is `off` (turn the lane on); an invalid lane setting value (fix it in Admin or `.env`); tools (`tool_requests`), a direct-to-model (bypass) request, Discover or Get Model Info while `lane_mode` is `all_local`; documents with a bypass request; documents on an `orchestrate` step outside `all_local` (its child planner would read them). Nothing was sent. |
| BRT-LANE-006 | Document text found in a request to a non-local endpoint (content backstop); blocked | `src/lane/outbound.py::send_outbound` / `check_backstop` (checks from `src/lane/backstop.py::find_leak`) | Nothing was sent. The body shared a passage of `lane_max_verbatim_chars` characters with a document, covered more than `lane_max_ngram_share` of its word 8-grams, or held a short document whole. The event names the document id and the rule, never the text. Common cause: document text pasted into the prompt; send it by reference instead. A tool query or a planner request built from document text ends here too. |
| BRT-LANE-007 | The ledger row for an outbound request could not be written; request not sent | `src/lane/outbound.py::record_start` | With the lane on every request is recorded before it is sent, so a failed write blocks it. Check database health, disk space and file permissions (`outbound_requests` table). |
| BRT-LANE-008 | Resume refused for a run that read documents | `src/api/tasks.py::resume_run_endpoint` | A run that read documents (or a child of one) is never resumed: a resume starts a fresh run without the run's documents or their scope, under today's lane settings. Start a new run with the same documents instead. The resume is also refused when the documents lookup itself fails (it fails closed): try again, or start a new run. |
