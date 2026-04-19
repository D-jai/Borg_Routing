# AGENTS.md — BORG_ROUTE

> Droid auto-loads this file at session start. Read it FIRST.

---

## Project Status

BORG_ROUTE is an Adaptive Recursive Agent Router. The initial build is **complete**: Slices 0-6 shipped, 245 tests passing. Subsequent sessions are maintenance, polish, or user-directed new features — not original-design work.

Build history and the full planning trail live under `zArchive/`. Do not re-read it unless specifically needed.

## First-Read Order (new session)

1. **`AGENTS.md`** (this file)
2. **`DESIGN_PRINCIPLES.md`** — Two Laws + architectural invariants (cheatsheet)
3. **`CODING_STANDARD_CORE.md`** — commit + error-code + test conventions
4. **`docs/user-guide.md`** — current operator-facing behaviour
5. Source files relevant to the user's request

Consult `zArchive/` only if a question genuinely requires historical context.

## Pre-Written Artefacts — DO NOT REGENERATE

These are prompt-engineering-dense and breaking them breaks the system:

- `src/prompts/base.py`, `src/prompts/orchestrator.py`, `src/prompts/answerer.py`, `src/prompts/__init__.py`
- `data/skills.yaml` (10 skills)
- `data/capability_matrix.yaml` (seeded matrix)

If a change looks necessary: stop, explain why, escalate before editing.

## Commit Discipline (Parallel Commit Law)

Every `feat:` commit ships together: code + error code (if new failure mode) + user-guide section + presentation bullet + happy-path test + failure-path test + `.env.example` update (if new env var). See `CODING_STANDARD_CORE.md` for the full checklist.

## Forbidden Without Architectural Review

- Changing either of the Two Laws (`DESIGN_PRINCIPLES.md §2`).
- Editing pre-written prompts/skills/matrix (above).
- Inventing new DAG node types beyond `orchestrate | answerer | apply_to_each | state_op`.
- Reintroducing the rejected Coach/critic.
- Adding frameworks outside the tech stack (FastAPI + aiosqlite + httpx + Pydantic + PyYAML + spaCy).

## Session End Hygiene

1. All tests pass (`python -m pytest`).
2. Commit scoped, descriptive changes. One logical change per commit.
3. Don't push unless the user asks.
