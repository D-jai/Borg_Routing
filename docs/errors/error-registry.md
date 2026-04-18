# Error Registry — BORG_ROUTE
> Format: `BRT-[MODULE]-[NNN]`
> Modules: AUTH, API, STG, ING, QRY, CFG, CLI — add more as needed

---

## BRT-REG — Registry Module

| Code | Description | Origin | Hints |
|------|-------------|--------|-------|
| BRT-REG-001 | Invalid model source | `src/registry/models.py::ModelInfo.validate_source` | Must be one of: `lmstudio`, `ollama`, `openrouter`, `custom_key`. Check the `source` field in your YAML or request body. |
| BRT-REG-002 | Negative pricing value | `src/registry/models.py::Pricing.no_negative_pricing` | `input_per_token` and `output_per_token` must be ≥ 0. Check `data/capability_matrix.yaml` or the model record being upserted. |
