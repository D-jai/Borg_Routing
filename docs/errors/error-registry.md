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
| BRT-REG-020 | No model meets requirements | `src/registry/router.py::select_model` | No **checked** model in the capability matrix satisfies the task_type + quality_requirement + context_length combination. Possible causes: no models are currently checked on the Models page (tick a checkbox); the Get Info pass has not been run so scores are at the default 5.0 (click "Get Model Info"); or the quality bar is too strict. |
| BRT-REG-001 (connection) | Connection failure during discovery | `src/registry/discovery.py` | A local or external model source was unreachable. Logged at DEBUG level and silently skipped. Check that LM Studio / Ollama / OpenRouter is running. |
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
| BRT-ORC-009 | Compose mode returned no response | `src/orchestrator/engine.py::orchestrate_run` | The orchestrator returned an empty `response` in compose mode. Check the compose prompt and model. |
| BRT-ORC-013 | OpenRouter API key missing | `src/registry/model_call.py::resolve_model_call` | An OpenRouter row was selected but `OPENROUTER_API_KEY` is not set. Add it to `.env` or uncheck OpenRouter rows on the Models page. |
| BRT-ORC-014 | Model row has no endpoint | `src/registry/model_call.py::resolve_model_call` | The matrix row was never populated with an endpoint (likely left over from a previous install). Run `POST /models/discover` to repopulate. |
| BRT-ORC-015 | Default answerer selection failed; fell back to orchestrator model | `src/orchestrator/engine.py::orchestrate_run` | `select_model` could not find a default answerer row, so the orchestrator model was reused. Warning — the run continues. Check that at least one row at or above the configured quality bar is checked on the Models page. |
| BRT-ORC-016 | Envelope persistence failed | `src/orchestrator/engine.py::orchestrate_run` | `save_envelope` or `update_envelope_inbound` raised. Error logged; the run still completes but the trace may miss an envelope row. Check DB health (`data/borg_route.db`) and disk space. |
| BRT-ORC-017 | `depends_on` id not found in results | `src/orchestrator/engine.py::_build_task_from_node` | The plan referenced an upstream node id that is not part of the current plan (typo or hallucination). The dependent node dispatches anyway with whatever upstream outputs could be resolved. Check the orchestrator's rationale for the stray id. |

## BRT-APE — apply_to_each Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-APE-001 | Items not resolvable | `src/orchestrator/apply_to_each.py::_resolve_items` | `dependency_input` must contain `'items'` (list) or `'inputs_ref'` (list). Check the orchestrator's plan node. |
| BRT-APE-002 | Child envelope dispatch failed | `src/orchestrator/apply_to_each.py::_run_one` | Individual failures go to `failures[]`, not raised. Check the run trace for the specific item's failure_reason. |
| BRT-APE-003 | Invalid concurrency_limit | `src/orchestrator/apply_to_each.py::expand_and_run` | Must be a positive integer. Default is 20. |
| BRT-APE-004 | Depth limit exceeded | `src/orchestrator/apply_to_each.py::expand_and_run` | `current_depth >= max_depth`. Increase `max_depth` or flatten the plan structure. |
| BRT-APE-005 | No items to process | `src/orchestrator/apply_to_each.py` | Items list is empty. Check the upstream state_op or input reference. |

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

## BRT-SOP — State Operators Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-SOP-001 | Unknown operator name | `src/orchestrator/state_ops.py::dispatch_state_op` | Must be one of: `filter`, `count`, `group_by`, `sample`. Check `dependency_input['op']` in the task. |
| BRT-SOP-002 | Missing required param | `src/orchestrator/state_ops.py::op_*` | Each operator requires specific params. `filter` needs `key` + `value`; `group_by` needs `key`; `sample` needs `n`. |
| BRT-SOP-003 | Items not found in dependency_input | `src/orchestrator/state_ops.py::_get_items` | The state_op task must have `dependency_input['items']` — a list of dicts to operate on. |
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
| BRT-API-024 | Resume failed | `src/api/tasks.py::resume_run_endpoint` | Could not resume the run. Check that the run exists and is not completed. |
| BRT-API-025 | Run list failed | `src/api/tasks.py::list_runs` | Could not fetch the list of runs from the database. Check database connectivity. |

## BRT-ORC — Orchestrator Module (Slice 3 additions)

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-ORC-020 | Max recursion depth reached — orchestrate node demoted to answerer | `src/agents/executor.py::execute` | The orchestrate node was at `max_depth`. Increase `max_recursion_depth` in Admin config, or flatten the plan. |
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
