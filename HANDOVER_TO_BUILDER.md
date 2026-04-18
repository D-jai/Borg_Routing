# Handover to Builder Agent — BORG_ROUTE
> Written by: Planning agent (Session 1, 2026-04-17)
> For: The agent who will write the code
> Read this AFTER MESSAGE_TO_CLAUDE.md and CHECKPOINT.md

---

## What You Are Building

BORG_ROUTE is an Adaptive Recursive Agent Router. The full architecture is in
`MESSAGE_TO_CLAUDE.md`. The phased build plan is in `BUILD_PLAN.md`.
Do not redesign. Do not add features. Build exactly what is specified.

**Your starting point:** Phase 1 — Model Registry + Discovery.
The task list is in `BUILD_PLAN.md` under Phase 1. Start at P1-01.

---

## How to Work

### Before Every Session
1. Read `CHECKPOINT.md` — "Where I Left Off" tells you exactly where to start
2. Read only the current phase section in `BUILD_PLAN.md`
3. Run existing tests — note the count before you touch anything

### During a Session
- Build one task at a time. Complete it before starting the next.
- Follow `CODING_STANDARD_CORE.md` — especially the Parallel Commit Law:
  every `feat:` commit ships code + error handling + tests together.
- Register every new error code in `docs/errors/error-registry.md` as you create it.
- Add test cases to `docs/tests/test-plan.md` as you build.
- If you discover a design gap or ambiguity — stop. Note it in `CHECKPOINT.md`
  under "Known Gaps" and continue around it. Do not invent architecture.

### After Every Session — Non-Negotiable Housekeeping

**Step 1 — BUILD_PLAN.md**
Check off every task you completed. Do not check off partial work.

**Step 2 — CHECKPOINT.md**
Update ALL of the following:
- "Where I Left Off" — one paragraph. Be precise. Name the exact file and function
  the next agent should pick up from. Assume the next agent has never seen this project.
- Phase Status table — update status of the phase you worked on
- "What Works" — add any verified working feature
- "What Has Never Been Tested" — add anything you wrote but didn't fully verify
- "Known Gaps" — add anything you found but didn't fix
- Session History — add a row: date, session number, work done, test count

**Step 3 — Commit**
```bash
git add <specific files only — never git add .>
git commit -m "feat: <what this does>"
# or fix:, chore:, test:, docs: as appropriate
```

**Step 4 — Push**
```bash
git push
```
If no remote is set up yet, remind the user: "GitHub repo D-Jai/BORG_ROUTE needs to be
created and remote added before pushing."

---

## What the Planning Agent Expects to See

When the planning agent returns to review progress, it will read these files in order:
`CHECKPOINT.md` → `BUILD_PLAN.md` → source files.

It expects:
- `CHECKPOINT.md` "Where I Left Off" to be accurate and precise — not vague
- `BUILD_PLAN.md` task checkboxes to reflect true completion status
- Git log to have clean, meaningful commits — one per logical unit of work
- Tests to be passing — the count should be in CHECKPOINT.md
- No invented architecture — if something was unclear, it's in Known Gaps

**If the planning agent finds stale docs, vague checkpoints, or unchecked tasks for
completed work — it will assume the work is not done.**

---

## Architecture Constraints — Do Not Violate

These were decided by the planning agent and are not open for re-interpretation:

| Constraint | Rule |
|-----------|------|
| Answerer contract | `type=answerer` tasks MUST return `resolved` or `failed`. Never `unresolved`. Raise `BRT-ANS-001` if violated. |
| Global Rules | Every single model call goes through `build_system_prompt()` in `src/prompts/base.py`. No exceptions. |
| Logging | Every model call writes a log event. No silent model calls. |
| Depth hard cap | `MAX_RECURSION_DEPTH = 5` is a constant in code. Admin UI cannot exceed it. |
| Context isolation | Answerers receive only their distilled context slice. Never the full thread. |
| Composer context | Only the Composer receives the conversation window. Answerers do not. |
| run() uniformity | `src/agents/executor.py::execute()` handles all three types. No type-specific callers. |
| Secrets | In `.env` only. Never in code or committed to git. |

---

## Key Files Quick Reference

| File | Purpose |
|------|---------|
| `MESSAGE_TO_CLAUDE.md` | Full architecture — read before anything else |
| `BUILD_PLAN.md` | Task-level build plan — 7 phases |
| `CHECKPOINT.md` | Current state — update every session |
| `CODING_STANDARD_CORE.md` | Coding rules — always apply |
| `docs/errors/error-registry.md` | Register every BRT-* error code here |
| `docs/tests/test-plan.md` | Add test cases as you build |
| `.env.example` | Add every new env var here as you introduce it |
| `C:\Stage\memory\STAGE_MEMORY.md` | Stage-level memory — update when phase completes |

---

## When a Phase Is Complete

1. All tasks in that phase checked off in `BUILD_PLAN.md`
2. All tests for that phase passing
3. `CHECKPOINT.md` updated — phase marked ✅ Complete
4. `docs/errors/error-registry.md` — all new error codes registered
5. `docs/tests/test-plan.md` — all new test cases documented
6. `.env.example` — any new env vars added
7. Clean commit with message: `feat: complete Phase N — <phase name>`
8. Update `C:\Stage\memory\STAGE_MEMORY.md` — append a dated entry noting the phase completion
9. Update `C:\Stage\CLAUDE.md` Active Projects table — update BORG_ROUTE status

---

## Questions and Ambiguities

If you encounter a genuine architectural ambiguity not covered in `MESSAGE_TO_CLAUDE.md`:
- Do NOT invent a solution
- Add it to `CHECKPOINT.md` under "Known Gaps" with label **[NEEDS PLANNING AGENT]**
- Continue building around it
- The planning agent will resolve it in the next planning session

---

## Current Status When You Start

- Phase 0: ✅ Complete
- Phase 1: 🔲 Not started — **start here**
- No tests yet
- No source code yet
- GitHub remote: not yet set up (create D-Jai/BORG_ROUTE private repo, add remote)
