# Coding Standard — Core (Always Loaded)
> Full reference: `C:\Users\Deejr\OneDrive\Documents\Claude\CODING_STANDARD.md` | Applies to all Aliud Inquisito projects

> **TODO — revisit this entire document.** The Engineering Guidelines section (G1-G7 + meta) at the bottom is a provisional addition from 2026-04-23, distilled from a bug-harvest on BORG_ROUTE. After applying them on 2-3 more projects, promote / demote / refine. The rest of this document predates those guidelines and has not been re-examined against them yet.

---

## THE PARALLEL COMMIT LAW
Every `feat:` commit ships all five artefacts together — no exceptions:

| # | Artefact | Where |
|---|----------|-------|
| 1 | Code | Source file |
| 2 | Error handling | Source file + error registry |
| 3 | User guide content | `docs/user-guide.md` |
| 4 | Presentation content | `docs/presentation-content.md` |
| 5 | Tests + factory/seed data | `tests/` + seed files |

**Checklist before every commit:**
```
[ ] Code works
[ ] Error code defined, registered, thrown with context
[ ] User guide section added (plain English, step-by-step, for non-technical user)
[ ] Presentation bullet added (what would a slide say about this?)
[ ] Happy path test written
[ ] Failure path test written (triggers the error code)
[ ] Factory/seed data exercises this feature
[ ] .env.example updated if new env vars added
```

---

## Error Codes
Format: `[PROJECT]-[MODULE]-[NNN]`

| Project | Prefix |
|---------|--------|
| Kare4u | `K4U` |
| KBPipeline | `KBP` |
| CtxMtg | `CTXMTG` |
| OpenClaw | `OCL` |

Common modules: `AUTH`, `PIPE`, `STG`, `ING`, `QRY`, `FRM`, `API`, `CFG`, `CLI`, `SYNC`

Every code gets an entry in `docs/errors/error-registry.md`: description, origin file+function, 3-5 troubleshooting hints.

Never swallow errors. Always re-throw with code + context data. API layer translates to structured JSON — stack traces never reach clients.

---

## Testing
- Naming: `TC-[MODULE]-[NNN] — Plain English description`
- Three levels per feature: Unit (mocked) → Integration (external mocked) → E2E (full system)
- Every error code has a test that triggers it
- Use factory functions — never hardcode fixtures inline

---

## Key Conventions (never violate)
- **Soft delete only** — `status` column, never hard DELETE (except GDPR)
- **UUIDs** for all public-facing IDs — never auto-increment
- **Adapters** for all external services — app code imports interface only, never concrete class
- **Secrets in env vars only** — `.env.example` committed, `.env` in `.gitignore`
- **Validate at boundaries** — Zod (TS) or Pydantic (Python) at every entry point
- **Structured JSON logging** — log IDs/codes, never passwords/tokens/PII
- **Never hardcode domains** — use env vars

---

## Git
- Format: `[type]: description` — types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `security`
- One logical change per commit. If you need "and" → split it.
- Every `feat:` commit follows the Parallel Commit Law above

---

## File Structure (this project)
```
docs/
  user-guide.md             ← updated with every feature
  install-guide.md          ← operator/IT + LLM-installer notes
  presentation-content.md   ← slide content (hand-maintained parallel doc)
  CHANGELOG.md              ← generated from .dev/ledger/
  user-guide.generated.md   ← generated from .dev/ledger/
  presentation.generated.md ← generated from .dev/ledger/
  errors/error-registry.md
.dev/
  ledger/                   ← one YAML per slice (Peak Context Ledger)
  render_*.py               ← regenerators for the .generated.md files
.env.example
AGENTS.md                   ← AI onboarding (Droid auto-loads)
CLAUDE.md                   ← Claude onboarding
DESIGN_PRINCIPLES.md        ← architectural invariants
CODING_STANDARD_CORE.md     ← this file
```

---

## AI Session Protocol
- **Start:** Read `AGENTS.md` / `CLAUDE.md` → `DESIGN_PRINCIPLES.md` → `CODING_STANDARD_CORE.md` → run tests
- **End:** All tests pass → commit scoped changes → remind Deej to say **"save to memory"**
- **Task cards must specify:** file paths, function names, DB tables, API routes, error codes, interfaces (not concrete classes), doc sections to update
- **Full reference:** `C:\Users\Deejr\OneDrive\Documents\Claude\CODING_STANDARD.md`

---

## Engineering Guidelines — Bug-Prevention Heuristics
> **These are GUIDELINES, not rules.** Distilled from a bug-harvest on BORG_ROUTE (2026-04-23) where ~15 integration bugs surfaced during end-to-end use. Apply where they help; skip where they would block honest work. **At worst they are no-ops; at best they catch integration bugs, silent failures, and contract drift early.** Revisit after using them on 2-3 more projects — promote to rules, refine, or demote.

### G1. Real-boundary verification
Every external integration (HTTP client, subprocess, file-system dependency on an external tool) should have at least one test that uses a **captured real-shape response**, not only hand-crafted mocks.

- **Heuristic:** Store `tests/fixtures/<service>_<operation>.json` captured from the real service. Header comment cites the command and date. Use in at least one test per call site.
- **Bug class caught:** Payload-shape drift, auth gaps, URL-encoding issues, protocol quirks. Any bug that would say *"but the mock passed."*

### G2. Config round-trip testing
Any value the user can set via UI / env var / config API should have one test that: writes through the surface → re-reads runtime state → confirms behaviour changed.

- **Heuristic:** For every `POST /config` field or `.env` key, assert the knob actually moves something. If it only changes a stored value without affecting runtime behaviour, it's a cosmetic bug.
- **Bug class caught:** "I set it but nothing happened" — config surfaces that write but the runtime never reads.

### G3. Co-designed contract matrix (multi-layer systems)
When a feature spans multiple layers (prompt + engine, client + server, config + runtime, UI + storage + runtime), keep an explicit cross-reference of fields/behaviours between layers. Review **both sides** when editing **either**.

- **Heuristic:** Maintain a `docs/protocol-matrix.md` (or similar) listing *"what layer A promises"* vs *"what layer B implements."* Review before changing either side.
- **Bug class caught:** Contract drift — layer A advertises a feature, layer B never implemented it; or layer A has a capability, layer B's UI/prompt never teaches it.
- **BORG_ROUTE-specific note:** The prompt files (`src/prompts/orchestrator.py`, `src/prompts/answerer.py`), the engine dispatch paths (`src/orchestrator/engine.py`, `src/orchestrator/apply_to_each.py`), and the data schemas (`data/skills.yaml`, capability-matrix schema) are one contract. The "Pre-Written Artefacts — DO NOT REGENERATE" rule in `AGENTS.md` protects these files from careless edits, but the scrutiny should happen at **planning time** for any feature that touches any layer, not only when an edit is proposed.

### G4. Loud failures preferred over silent fallbacks
`dict.get(key, fallback)` on semantic response data is a smell. If the key is genuinely optional, document why. Otherwise, raise with context: the keys received, the keys expected, the surface that produced the bad shape.

- **Heuristic:** Before committing, grep diff for `.get(`. Each occurrence should be either trivially defaulted (e.g. `get("color", "blue")` for cosmetic UI) or converted to a raise with a diagnostic.
- **Bug class caught:** Invisible degradation — the system "succeeds" with garbage output because the real error got swallowed by a fallback. Most expensive class of bugs to debug.

### G5. Deployment-context-explicit defaults
Magic-number defaults (timeouts, cost constants, retry counts, context windows) should carry a one-line comment naming the **primary deployment context** they were tuned for, with a pointer to the override mechanism for other contexts.

- **Heuristic:** `_DEFAULT_X = value  # Primary: <context>. Override: <env var or config key>.`
- **Bug class caught:** Defaults chosen for one deployment type (e.g. prod SaaS) silently break another (local dev). The comment forces the decision to be explicit at write-time, not discovery-time.
- **BORG_ROUTE-specific note:** Primary deployment context is **single developer running locally** (LM Studio / Ollama + optional OpenRouter key). Secondary: small team shared dev server. Tertiary: production. Defaults in `src/registry/cost.py`, timeouts, and retry counts should be tuned for the primary context.

### G6. Happy-path and unhappy-path in every test file
Every new error code deserves a test that triggers it with the right diagnostic. Every feature ships with at least one "it misfires" test alongside the "it works" tests.

- **Heuristic:** Structure new test files as `# Happy path` and `# Unhappy path` sections. The second section is not optional — if you can't write a meaningful unhappy test, your error handling is probably incomplete.
- **Bug class caught:** Silent-fallback and default-value bugs that are invisible under happy-path-only testing.

### G7. Seed data from live shape, not imagination
Test fixtures and seeded data files should come from the actual output of the relevant code path (discovery function, rendering function, external API), not hand-typed from assumption.

- **Heuristic:** No fixture committed by hand-typing the expected shape. Capture from the real code / service once, commit with a header comment citing origin + date.
- **Bug class caught:** Hand-typed fixtures drift from real output. Code works against the fiction, fails against reality. Especially bad for ID formats, nested shapes, optional fields.
- **BORG_ROUTE-specific note:** The 7 hand-seeded LM Studio rows in `data/capability_matrix.yaml` (committed in cc8ed64) violated this principle — the IDs didn't match what `GET /v1/models` actually returns. Slice BRT-DS-01 will fix this by regenerating seeds from live discovery output.

### G-Meta. End-to-end smoke every few slices
Every 2-3 features, run the full system end-to-end against fresh-install state with real external services. Not "all unit tests pass" — a full human-observable interaction: submit a realistic query, inspect the output, confirm no silent fallbacks or mis-shapes.

- **Heuristic:** Keep a `scripts/smoke.py` or `make smoke` target that wipes runtime state, re-discovers, runs a known query, asserts a non-fallback response. Run it as a checkpoint every few slices.
- **Bug class caught:** Contract drift between layers that each pass their own unit tests. ~10 minutes of cheap insurance against an entire class of integration failures.
- **Future slice:** The smoke script is not yet written. Flagged for a future slice (could be BRT-OPS-01 or similar) once the current HIGH-priority fix batch lands.
