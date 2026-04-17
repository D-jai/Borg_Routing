# Message to Claude — BORG_ROUTE Handover
> Last updated: 2026-04-17 (Session 1)
> Purpose: AI agent onboarding. Read this FIRST, then follow the reading order below.

---

## Mission
TBD — Deej will define the mission in a follow-up session.

---

## Document Map

| Document | Purpose | When to read |
|----------|---------|--------------|
| `MESSAGE_TO_CLAUDE.md` (this file) | Onboarding, reading order, quick reference | Always first |
| `CHECKPOINT.md` | Current state, phase statuses, known gaps | After this file |
| `BUILD_PLAN.md` | Full phased plan with task-level detail | When planning work |
| `CODING_STANDARD_CORE.md` | Rules for every commit — always apply | Before writing any code |
| `CODING_STANDARD.md` | Full patterns + examples | When you need the detail |
| `docs/errors/error-registry.md` | All error codes and hints | When debugging |
| `docs/tests/test-plan.md` | Test strategy and coverage | When writing tests |
| `docs/user-guide.md` | End-user documentation | When understanding features |
| `docs/install-guide.md` | Operator / deployment docs | When deploying |
| `docs/presentation-content.md` | Slide content per feature | When updating portfolio |
| `.env.example` | All env vars with placeholder values | When deploying |

---

## Reading Order (strictly follow this)

**Phase 1 — Orientation (once per session)**
1. This file — read fully
2. `CHECKPOINT.md` — current state, pay attention to "Where I Left Off"
3. `CODING_STANDARD_CORE.md` — rules that apply to everything you write

**Phase 2 — Context for current task**
4. `BUILD_PLAN.md` — find the phase/task you're working on; read only that section
5. Relevant source files — read on demand as you work

**Phase 3 — Deep dives (only when needed)**
6. `CODING_STANDARD.md` — full patterns and examples, load per section
7. `docs/errors/error-registry.md` — when adding or debugging error codes

---

## Current State
> See `CHECKPOINT.md` for the live status. Quick summary below:

- **Phase:** Phase 0 — Scaffolding
- **What works:** Project structure created
- **What's next:** Define mission and tech stack; complete Phase 0 tasks
- **Known gaps:** Mission, tech stack, and Phase 1+ plan are TBD

---

## Deployment Architecture
TBD

---

## Tech Stack
| Layer | Technology |
|-------|-----------|
| TBD | TBD |

---

## Key Conventions (never violate)
- Error codes: `BRT-[MODULE]-[NNN]` — register every code in `docs/errors/error-registry.md`
- Soft delete only — `status` column, never hard DELETE
- Secrets in env vars only — never in code
- Adapters for all external services — never import concrete class in app code
- Follow `CODING_STANDARD_CORE.md` Parallel Commit Law on every `feat:` commit

---

## Session Start Checklist
```
1. Read CHECKPOINT.md (especially "Where I Left Off")
2. Start infrastructure (docker compose up -d / relevant services)
3. Run tests — expect 0 passing (Phase 0)
4. Pick up from where CHECKPOINT.md says
```

## Session End Checklist
```
1. All tests pass
2. CHECKPOINT.md updated with progress + "Where I Left Off" note
3. Commit and push
```
