# Session Handoff — 2026-04-18 (Planning → First Build Session)

## Context At Write Time
- Written by: Planning agent (Session 2, complete)
- For: The first builder session
- Model to use: **Claude Sonnet 4.6** (set via `/model` or Shift+Tab → Settings)
- Reasoning effort: **High** by default, **Max** for Builder: Heavy tasks

## Where I Left Off

Planning is complete. No code has been written yet. Next session is the
first building session. Start at **BUILD_PLAN.md S1-00** (create
`pyproject.toml` + `main.py`).

## Just Completed (Planning Session 2)

- Architecture reassessed end-to-end; DESIGN_PRINCIPLES.md written
- Orchestrator + answerer + base prompts written into `src/prompts/`
- 10 skills hand-crafted in `data/skills.yaml`
- Capability matrix seeded in `data/capability_matrix.yaml` (8 local + 5 OpenRouter)
- BUILD_PLAN.md rewritten with extreme-spec tasks for Slices 1 and 2
- B (OpenAI-compat gateway) and D (MCP server) chosen as product surfaces
- Context handoff regime designed (this document, + AGENTS.md)

## In Progress
None — clean slate for the builder.

## Next Task (specific)

**BUILD_PLAN.md Slice 1, task S1-00.**

First concrete actions for the builder:
1. Read `AGENTS.md` and this file (you're doing it).
2. Read `DESIGN_PRINCIPLES.md` in full (once; it's the only time you'll need to).
3. Read `BUILD_PLAN.md` Slice 1 section only.
4. Create `pyproject.toml` per S1-00 spec.
5. Create `main.py` per S1-00 spec.
6. Run `python -m pip install -e .[dev]` (or `uv sync` if using uv).
7. Run `uvicorn main:app --reload` — verify `GET /health` returns `{"status":"ok"}`.
8. Update `docs/user-guide.md` + `docs/install-guide.md` with install steps.
9. Write first pytest: `tests/test_health.py`.
10. Commit: `feat: scaffold FastAPI app with health endpoint (S1-00)` per Parallel Commit Law.
11. Update `BUILD_PLAN.md` — check S1-00.
12. Update `CHECKPOINT.md` — log session progress.
13. Create zip backup per HANDOVER_TO_BUILDER.md Step 4.
14. Proceed to S1-01 (verify pre-written artefacts exist).

## Files To Read In New Session (minimal set)

In this order:
1. `AGENTS.md`
2. This `SESSION_HANDOFF.md`
3. `MESSAGE_TO_CLAUDE.md` (first session only — once you know it, reference briefly)
4. `DESIGN_PRINCIPLES.md` §§ 1, 2, 3 at minimum (thesis + laws + patterns)
5. `CODING_STANDARD_CORE.md` (the Parallel Commit Law rules)
6. `BUILD_PLAN.md` — **only Slice 1** for this first session
7. `CHECKPOINT.md`

DO NOT read source files yet — there are none.

## Open Issues / Gotchas

- `spacy` is listed in pyproject dependencies, but the model file
  `en_core_web_sm` needs a separate download step:
  `python -m spacy download en_core_web_sm`. Include this in install-guide.md.
- `gh` CLI is not installed per user's environment info. GitHub remote is
  deferred; use zip backups.

## Tests Status
- Total: 0
- Will be created starting in S1-00 (test_health.py)

## Instruction To Future Builder

Read the files listed above in order. Continue from "Next Task (specific)".
The planning agent front-loaded all architectural decisions — your job is
disciplined execution with Parallel Commit Law on every feat commit.

**When YOUR context reaches ~65%, or you complete a slice:**
1. Commit + zip backup
2. Update CHECKPOINT.md
3. OVERWRITE this file (`SESSION_HANDOFF.md`) using the template in AGENTS.md
4. Tell the user: "Context at 65%. Start a new Droid session and type:
   `Read AGENTS.md and SESSION_HANDOFF.md, then continue.`"
5. Stop and wait.

The bootstrap chain: this planning handoff → builder session 1 →
builder session 2 → ... → project complete. Each session hands off
to the next cleanly via this file.

**Good luck. Build exactly what is specified. Do not redesign.**
