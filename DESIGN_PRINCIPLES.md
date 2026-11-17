# Design Principles — BORG_ROUTE (Cheatsheet)
> Compacted from the original 668-line design doc (archived out of the repo).
> This file captures the architectural invariants any future maintainer must respect.

---

## 1. Product Thesis (one sentence)

The orchestrator is a project manager, not a worker — it spends intelligence on **structuring** work (building a good WBS and routing each step to the cheapest capable model), not on **doing** it. Direct-mode answering is the default; decomposition fires only when it materially improves quality or cost.

---

## 2. The Two Laws (non-negotiable)

### Law 1 — Uncompacted Governance
Compression applies to content, never to governance. Global Rules, the original user instruction, task-specific invariants, and Admin-UI system instructions pass through every cascade **verbatim and additive** — never replacing, never diluting.

- **Enforcement point:** `src/prompts/base.py::build_system_prompt()`
- **Governance (uncompacted):** Global Rules, original user instruction, Admin-UI rules, task-specific invariants.
- **Content (compressible):** conversation history, intermediate outputs, raw data.

### Law 2 — No Forgotten Scratch (Fold-in Pattern)
Every SQLite write has an owner. Children self-clean before returning; parents verify at fold-in and log any residue as a diagnostic signal. **Residue = misbehaving agent**, not normal state.

- Child reads own scratch → digests into structured output → deletes own writes.
- Parent sweeps `WHERE level = {child_level}` after fold-in. Residue → `child_residue` event + trust adjustment + cleanup.
- Scratch is ephemeral; structured envelope outputs persist.

**Status (16-Nov-2026).** The rule stands. Its machinery (the `scratch` table, cleanup directives, fold-in residue check) exists, but no current step writes scratch: `write_scratch` has no caller in `src/`, and an answerer's `scratch_writes` are recorded, not stored. Law 2 is therefore not exercised by any current run and is outside the showcase's claims.

---

## 3. Core Patterns (mental model)

| Pattern | One-liner |
|---|---|
| **Wrapper-as-Compression Contract** | The inbound schema IS the compression spec. "Return me this shape" ≡ "compress to fit this shape." |
| **Progressive Distillation** | Compression is a by-product of work, not a separate step. Every answerer emits already-compressed structured output. |
| **Cascading Distillation** | If one pass isn't enough, cascade another. Bounded by `MAX_RECURSION_DEPTH = 5`. |
| **Context Bucket** | Multi-resolution context assembled at Composer time: high-res (governance + question) + structured evidence + compact history + state snapshots. |
| **Orchestrator = Composer** | Same prompt, different `current_mode` flag (`plan \| dispatch \| re_plan \| compose`). No handover → no information loss. |

---

## 4. Execution Model

### DAG Node Types (the only four — do not invent new ones)
- `orchestrate` — spawn child run for a sub-problem.
- `answerer` — leaf model call with envelope I/O.
- `apply_to_each` — runtime expansion: one DAG node → N envelopes over a list. Best-effort aggregation; failures are data, not catastrophe.
- `state_op` — deterministic (no-model) operator: `filter`, `count`, `group_by`, `sample`.

### Four Shared-State Mechanisms (enable emergent patterns)
1. `state_query` on envelope inputs — parameterised SQL executed before dispatch, injected as context.
2. `side_effects` on envelope outputs — declared, structured writes to named tables.
3. Orchestrator re-invocation mid-run — invoked with current state digest whenever re-planning is needed.
4. State operators — deterministic manipulation between model stages.

### Router Signature (specialty-primary, not tier-primary)
```python
select_model(task_type, est_input_tokens, quality_req, strategy)
# candidates = matrix.query(task_type) filtered by score>=quality_req and context>=tokens
# sorted by effective_cost; tier is derived
```

---

## 5. Cost Model (one formula)

```
effective_cost =
    (tokens_in × input_rate + tokens_out × output_rate)     # $ (external)
  + (tokens_total / throughput_tps) × time_cost_per_sec     # time→$ (local)
```

- External: first term dominates; rates from OpenRouter `/api/v1/models`.
- Local: first term = 0; throughput_tps measured during benchmark.
- `time_cost_per_sec` is an Admin UI knob (default ≈ $30/hr).

---

## 6. Observability Essentials

| Artefact | Purpose |
|---|---|
| `logs` + `log_payloads` | every model call + full text |
| `trigger_reason` on envelopes | causal graph via one WHERE clause (`initial_plan`, `escalation_from:X`, `retry:X`, ...) |
| `orchestrator_decision` event | verbatim reasoning + plan per invocation |
| `child_residue` event | fold-in sweep hit → misbehaviour signal per model |
| `run_digests` | one row per finished run: cost, latency, token-by-model, counts, rating |
| `user_rating` on runs | ground-truth signal from thumbs-up/down |
| NER worker (optional) | spaCy entity + SVO extraction over log_payloads — parked, enable on demand |

**Rejected (do not reintroduce):** a scheduled Coach/critic model. The system self-regulates via Direct-mode bias + quality-by-floor lineup.

---

## 7. Skill Library (the orchestrator's toolbox)

The orchestrator's core skill is **designing good inbound schemas.** The Skill Library provides 10 reusable task templates; the orchestrator's system prompt includes them and prefers invocation over cold-crafting.

- Catalogue: `data/skills.yaml` (versioned)
- Loader: `src/skills/library.py`
- Telemetry: `skill_usage_log` (outcome, tokens, latency, composer_accepted, residue_count)
- UI: `/ui/skills.html` — per-skill stats + "Edit → New Version" bumps version; old stats preserved.

**This is not the Coach.** The Coach policed decisions; the Skill Library sharpens tools.

---

## 8. Product Surfaces (API-first)

| Surface | Path | Use case |
|---|---|---|
| **Primary** | `POST /v1/chat/completions` | OpenAI-compatible gateway (Cursor, Continue, Droid, custom scripts). Streaming via SSE. |
| **Secondary** | `POST /v1/task` → `GET /runs/{id}/stream` | Long-running async jobs; 202 + poll/SSE. |
| **Tertiary** | `/mcp` JSON-RPC | MCP tools for Claude Desktop, Continue. |
| **Reference client** | browser UI (`/chat`, `/models`, `/admin`, `/dashboard`, `/run/{id}`, `/logs`, `/skills`) | Dogfoods the public API — no privileged backdoor. |

Gateway model aliases: `borg_route`, `borg_route:{tier}`, `borg_route:{specialty}`, `borg_route:local`, or any raw `model_id` (bypass).

---

## 9. Forbidden Without New Architectural Review

- Changing either of the Two Laws (§2).
- Editing the pre-written prompts (`src/prompts/*.py`), skills (`data/skills.yaml`), or seeded matrix (`data/capability_matrix.yaml`) — these are prompt-engineering-dense and breaking them breaks the system.
- Inventing new DAG node types beyond the four in §4.
- Reintroducing the rejected Coach/critic.
- Adding frameworks outside the tech stack (FastAPI + SQLite/aiosqlite + httpx + Pydantic + PyYAML + spaCy).

If any of these look necessary: stop, document why, and escalate before coding.

---

## Further Reading
- `CONTRIBUTING.md`: commit law and conventions.
- `docs/user-guide.md` — operator-facing behaviour.
- `docs/install-guide.md` — install + LLM-installer notes.
