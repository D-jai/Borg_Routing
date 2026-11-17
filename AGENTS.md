# AGENTS.md: BORG_ROUTE

> Coding agents load this file at session start. Read it first.

---

## Project Status

BORG_ROUTE is an Adaptive Recursive Agent Router. The core build is complete and the project is in its showcase phase. Last full-suite run recorded in `BUILD.md`: 1296 passed on 17-Nov-2026 (the one failure that run showed was a test, since fixed). Run `python -m pytest` for the current figure. Sessions are maintenance, polish, or new features, not original-design work.

The web pages live in `ui/`: `ask.html`, `run.html`, `runs.html`, `admin_overview.html`, `models.html`, `keys.html`, `rules.html`, `settings.html`, `skills.html`, `logs.html`.

## First-Read Order (new session)

1. **`AGENTS.md`** (this file)
2. **`DESIGN_PRINCIPLES.md`**: Two Laws and architectural invariants
3. **`CONTRIBUTING.md`**: commit law and conventions
4. **`docs/user-guide.md`**: current operator-facing behaviour
5. **`docs/install-guide.md`**: install and LLM-installer notes (if installing)
6. Source files relevant to the request

## Pre-Written Artefacts: DO NOT REGENERATE

These are prompt-engineering-dense and breaking them breaks the system:

- `src/prompts/base.py`, `src/prompts/orchestrator.py`, `src/prompts/answerer.py`, `src/prompts/__init__.py`
- `data/skills.yaml`
- `data/capability_matrix.yaml` (seeded matrix; changes only through the app's own Discover, Get Model Info and checkbox calls)

If a change looks necessary: stop, explain why, escalate before editing.

## Commit Discipline

Every `feat:` or `fix:` commit ships together: code, error code (if a new failure mode), user-guide lines, a working-case test, a failing-case test, and a `.env.example` update (if a new env var). See `CONTRIBUTING.md`.

## Forbidden Without Architectural Review

- Changing either of the Two Laws (`DESIGN_PRINCIPLES.md` section 2).
- Editing the pre-written prompts, skills or matrix (above).
- Inventing new DAG node types beyond `orchestrate | answerer | apply_to_each | state_op`.
- Reintroducing the rejected Coach/critic.
- Adding frameworks outside the tech stack (FastAPI, aiosqlite, httpx, Pydantic, PyYAML, spaCy).

## Session End Hygiene

1. Tests pass (`python -m pytest`), or say which do not.
2. Commit scoped, descriptive changes. One logical change per commit.
3. Do not push unless asked.
