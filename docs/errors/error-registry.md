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

## BRT-API — API Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-API-001 | Malformed request body | `src/api/models.py::add_key` | `name` and `api_key` are required fields. Check the request JSON body. |
| BRT-API-002 | Model not found in matrix | `src/api/models.py::run_benchmark` | The model_id does not exist in the capability matrix. Run `POST /models/discover` first, or check the model_id for typos. |
