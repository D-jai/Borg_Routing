# Changelog

> Generated from `.dev/ledger/` at 2026-04-23 04:33 UTC.
> Do not hand-edit — re-run `python .dev/render_changelog.py`.

## scoring

### Added

- **Replace benchmark subsystem with LLM-research scorer + checkbox curation** — `BRT-SC-01` _**BREAKING**, demo_ (2026-04-22)
  The old benchmark subsystem — which sent 5 prompts per task_type to every model and scored outputs via a hardcoded local judge LLM — is gone. In its place: a "Get Model Info" button that asks a web-search LLM to research each model's capabilities using published benchmarks, then writes relative 0-10 task scores to the matrix.
  Model intake is now operator-curated. The OpenRouter discovery flood (300+ models) is replaced by a 12-model curated list in data/curated_models.yaml. Every row gets a checkbox in the Models UI. Only checked rows are considered by the router — so the operator controls the "available pool" directly.
  Unresearched rows default to a flat 5.0 on every task_type. The orchestrator softens its 8.0 quality bar to 5.1 when no model qualifies, logging BRT-ORC-007-DEGRADED, so the system still routes during a cold start or after a matrix wipe.
  New surface:
  - `endpoint` `POST /models/research` — Run the Get Info pass on the full matrix or a subset
  - `endpoint` `PATCH /models/{model_id:path}/checked` — Toggle whether a model is in the routing pool
  - `field` `ModelInfo.checked` — Boolean gate for the router
  - `file` `data/curated_models.yaml` — Operator-curated OpenRouter model pool
  Error codes: `BRT-REG-030`, `BRT-REG-031`, `BRT-REG-032`, `BRT-ORC-007-DEGRADED`
  Env vars: `SCORING_MODEL`, `SCORING_MODEL_ENDPOINT`, `OPENROUTER_API_KEY`


## slice-1

### Added

- **Model discovery across LM Studio, Ollama, OpenRouter, and custom endpoints** — `BRT-S1-06` _demo_ (2026-04-18)
  BORG_ROUTE can now auto-discover every model running on the user's machine and on OpenRouter without any manual config. Hit one button and the capability matrix is populated with model IDs, context lengths, and pricing. This is the "point it at your lineup" moment — after this, the router has something to route to.
  New surface:
  - `endpoint` `POST /models/discover` — Scan providers and populate capability matrix
  - `endpoint` `GET /models/matrix` — Return current matrix
  Error codes: `BRT-REG-001`, `BRT-REG-003`, `BRT-REG-004`
  Env vars: `OPENROUTER_API_KEY`


## slice-2

### Added

- **OpenAI-compatible chat-completions gateway** — `BRT-S2-13` _demo_ (2026-04-18)
  Any tool that speaks the OpenAI chat-completions protocol — Cursor, Continue, Droid, custom scripts — can now point at BORG_ROUTE and get orchestrated routing for free. Model aliases like borg_route:light and borg_route:coding steer the router. This is the primary product surface: invisible infrastructure behind existing tools.
  New surface:
  - `endpoint` `POST /v1/chat/completions` — OpenAI-compatible chat gateway
  - `alias` `borg_route` — Full orchestration, system decides
  - `alias` `borg_route:{tier}` — Tier hint — light/mid/heavy
  - `alias` `borg_route:{specialty}` — Specialty hint — coding/creative/etc
  - `alias` `borg_route:local` — Force LOCAL_FIRST strategy
  Error codes: `BRT-API-010`, `BRT-API-011`, `BRT-API-012`


## slice-3

### Added

- **Admin rules ledger — additive governance with audit trail** — `BRT-S3-04` (2026-04-18)
  Admins can now append new global rules that inject into every model call's governance layer (e.g. "Respond only in English"). Old rules can be retired but remain visible in the ledger for audit. This implements Law 1 (Uncompacted Governance) at the UI layer — rules are additive, never silently edited.
  New surface:
  - `endpoint` `GET /config` — Read current config + rules ledger
  - `endpoint` `POST /config` — Update settings (depth, strategy, etc.)
  - `endpoint` `POST /config/rules` — Append new rule
  - `endpoint` `POST /config/rules/{id}/retire` — Retire a rule (kept for audit)
  Error codes: `BRT-ADM-001`, `BRT-ADM-002`


## slice-4

### Added

- **Rolling summary compression for long conversations** — `BRT-S4-02` _internal_ (2026-04-18)
  When a conversation gets long, BORG_ROUTE compresses the older turns into a concise summary (3-8 sentences) so the orchestrator always has useful context without overloading its prompt window. The summary is produced by a model call, logged for auditability, and stored per thread. On the next run in that thread the summary is auto-loaded.
  New surface:
  - `python_api` `src.memory.compressor.compress_inactive_turns` — Compress older turns into rolling summary
  Error codes: `BRT-MEM-003`, `BRT-MEM-004`, `BRT-MEM-005`


## slice-5

### Added

- **spaCy NER worker for log-payload entity and relation extraction** — `BRT-S5-05` (2026-04-18)
  A background worker reads every text payload the system logs — prompts, outputs, decisions, errors — and pulls out structured facts: named entities (PERSON, ORG, GPE, DATE, MONEY, ...) and simple subject-verb-object triples. Results land in log_entities and log_relations and surface in the Logs UI. Parked by default (not auto-started); turn on when richer log analytics are worth the cost.
  New surface:
  - `python_api` `src.logging.ner_worker.run_ner_worker` — Poll log_payloads and extract entities + relations
  Error codes: `BRT-LOG-010`, `BRT-LOG-011`, `BRT-LOG-012`, `BRT-LOG-013`
  Dependencies: `spacy`, `en_core_web_sm`


## slice-6

### Added

- **Live SSE run stream — watch a run unfold in real time** — `BRT-S6-01` _demo_ (2026-04-18)
  The Run Trace page now shows envelopes appearing and updating live as the orchestrator dispatches them. A green pulsing dot indicates the stream is active; an apply_to_each counter ticks upward as parallel subtasks launch. No polling, no refresh. Closes with a "Completed" or "Failed" badge when the run finishes.
  New surface:
  - `endpoint` `GET /runs/{id}/stream` — Server-sent-events feed of envelope status changes
  Error codes: `BRT-API-028`, `BRT-API-029`
