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
| BRT-REG-013 | Zero or negative throughput_tps | `src/registry/cost.py::effective_cost` | A local model has an invalid throughput value (0 or negative). Re-run `POST /models/benchmark/{id}` to re-measure. |
| BRT-REG-020 | No model meets requirements | `src/registry/router.py::select_model` | No model in the capability matrix satisfies the task_type + quality_requirement + context_length combination. Run `POST /models/discover` to find more models, or lower quality_requirement. |
| BRT-REG-001 (connection) | Connection failure during discovery | `src/registry/discovery.py` | A local or external model source was unreachable. This is logged at DEBUG level and silently skipped. Check that LM Studio / Ollama / OpenRouter is running. |
| BRT-REG-003 | OpenRouter rate limit | `src/registry/discovery.py::discover_openrouter` | HTTP 429 from OpenRouter. Wait and retry, or add an API key to get higher rate limits. |
| BRT-REG-004 | Invalid external API key | `src/registry/discovery.py::discover_openrouter / discover_custom_key` | HTTP 401 from the external provider. Check the API key in `.env` and the Keys page in the UI. |
| BRT-REG-014 | Judge model call failed during benchmark | `src/registry/benchmark.py::_judge_response` | The judge model endpoint is not reachable or returned an unparseable score. Check that the judge model is running and accessible at the configured endpoint. |
| BRT-REG-015 | Model under test unreachable during benchmark | `src/registry/benchmark.py::benchmark_model` | The model has no endpoint configured, or the endpoint is unreachable. Run discovery first to populate endpoints. |

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

## BRT-API — API Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-API-001 | Malformed request body | `src/api/models.py::add_key` | `name` and `api_key` are required fields. Check the request JSON body. |
| BRT-API-002 | Model not found in matrix | `src/api/models.py::run_benchmark` | The model_id does not exist in the capability matrix. Run `POST /models/discover` first, or check the model_id for typos. |
