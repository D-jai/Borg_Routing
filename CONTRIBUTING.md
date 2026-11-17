# Contributing to BORG_ROUTE

Read `DESIGN_PRINCIPLES.md` first: the Two Laws and the architectural invariants are not up for change in a normal pull request.

## Commit law

Every `feat:` or `fix:` commit ships together:

- the code
- an error code, when the change adds a new failure mode (`docs/errors/error-registry.md`)
- the user-guide lines (`docs/user-guide.md`)
- a test for the working case and a test for the failing case
- `.env.example`, when the change adds an environment variable

One logical change per commit. If the message needs "and", split it. Commit only the files the change touched.

## Commit messages

`type: description`, where type is one of `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `security`.

## Tests

Run the tests for the code you changed. Run the whole suite (`python -m pytest`) before a pull request or at the end of a phase of work, not after every edit. Do not run the same full set twice on one tree.

## Never delete

Do not delete files or data. Move them to an archive folder outside the repository and record what moved, and only with the owner's agreement. Inside git, use `git rm` only after the file is archived.

## Rules that do not bend

- Four step types only: `orchestrate`, `answerer`, `apply_to_each`, `state_op`. New behaviour is an attribute, never a new type.
- No Coach or critic role.
- Stack: FastAPI, aiosqlite, httpx, Pydantic, PyYAML, spaCy. No new runtime framework. Test-only tools (Playwright) are allowed as an optional extra.
- Protected files: `src/prompts/*.py` and `data/skills.yaml`. `data/capability_matrix.yaml` changes only through the app's own Discover, Get Model Info and checkbox calls. If a change to one looks necessary, stop and raise it first.
- Keys live in `.env` only. Never print, log or commit a key.
- Structured logging: log IDs and codes, never secrets or personal data.
