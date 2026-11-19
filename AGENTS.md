# AGENTS.md: BORG_ROUTE

> Coding agents load this file at session start. Read it first.

---

## Project Status

BORG_ROUTE is an Adaptive Recursive Agent Router. The core build is complete and the project is in its showcase phase. The full suite runs in CI (GitHub's hosted runner); the last green run is recorded in `BUILD.md` (CI run 44437792274 on `e97bd70`, 18-Nov-2026). Sessions are maintenance, polish, or new features, not original-design work.

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

Every `feat:` or `fix:` commit ships together: code, error code (if a new failure mode), user-guide lines, a working-case test, a failing-case test, and a `.env.example` update (if a new env var). The deck is updated at release, not per commit. See `CONTRIBUTING.md`.

## Forbidden Without Architectural Review

- Changing either of the Two Laws (`DESIGN_PRINCIPLES.md` section 2).
- Editing the pre-written prompts, skills or matrix (above).
- Inventing new DAG node types beyond `orchestrate | answerer | apply_to_each | state_op`.
- Reintroducing the rejected Coach/critic.
- Adding frameworks outside the tech stack (FastAPI, aiosqlite, httpx, Pydantic, PyYAML, spaCy).

## Never Delete

Do not delete files or data. Move them to an archive folder outside the repository and record what moved, and only with the owner's agreement. Inside git, use `git rm` only after the file is archived.

## Session End Hygiene

1. Run only the tests for what you changed, locally (planner ruling 30d, Deej's machine rule of 18-Nov-2026). The full suite is the CI run, once per ready batch of changes, never a local run. Docker tests and Playwright never run on the ThinkPad. Say which tests do not pass.
2. Commit scoped, descriptive changes. One logical change per commit.
3. Do not push unless asked.
