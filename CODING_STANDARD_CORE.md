# Coding Standard — Core (Always Loaded)
> Full reference: `C:\Users\Deejr\OneDrive\Documents\Claude\CODING_STANDARD.md` | Applies to all Aliud Inquisito projects

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

## File Structure (every project has these from day one)
```
docs/
  user-guide.md           ← updated with every feature
  install-guide.md        ← operator/IT docs
  presentation-content.md ← slide content, updated with every feature
  errors/error-registry.md
  tests/test-plan.md
.env.example
MESSAGE_TO_CLAUDE.md      ← AI onboarding
CHECKPOINT.md             ← current state + "where I left off"
BUILD_PLAN.md
CODING_STANDARDS_CORE.md  ← this file (copied from C:\Stage\)
```

---

## AI Session Protocol
- **Start:** Read `CHECKPOINT.md` → `MESSAGE_TO_CLAUDE.md` → start infra → run tests
- **End:** All tests pass → update `CHECKPOINT.md` → commit + push → remind Deej to say **"save to memory"**
- **Task cards must specify:** file paths, function names, DB tables, API routes, error codes, interfaces (not concrete classes), doc sections to update
- **Full reference:** `C:\Users\Deejr\OneDrive\Documents\Claude\CODING_STANDARD.md`
