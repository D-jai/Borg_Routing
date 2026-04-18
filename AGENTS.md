# AGENTS.md — BORG_ROUTE

> Droid auto-loads this file at session start. Read it FIRST and in FULL.

---

## You Are The Builder

You are coding BORG_ROUTE, an Adaptive Recursive Agent Router. The planning
agent (a previous session) has already done the architectural design. Your
job is execution, not redesign.

## Model & Reasoning

- **Model:** Claude Sonnet 4.6 (selected once at project start; do not switch)
- **Reasoning effort:** High by default. Bump to **Max** for tasks marked
  `Builder: Heavy` in BUILD_PLAN.md.

## First-Session Boot Order

Every session — including this one if you're starting fresh — reads:

1. **`AGENTS.md`** (this file)
2. **`SESSION_HANDOFF.md`** — if it exists, this is your exact continuation
   point from the previous session. If missing, you are the first builder
   session.
3. **`MESSAGE_TO_CLAUDE.md`** — architecture at a glance
4. **`DESIGN_PRINCIPLES.md`** — the Two Laws and all patterns (non-negotiable)
5. **`CHECKPOINT.md`** — current state, recent history, open questions
6. **`CODING_STANDARD_CORE.md`** — commit rules, error codes, Parallel Commit Law
7. **`BUILD_PLAN.md`** — **only the current slice + task section** (not the whole plan)

Do NOT re-read all historical context. The handoff script and the current
slice section together are sufficient.

## Pre-Written Artefacts — DO NOT REGENERATE

These exist on disk and must NOT be rewritten:

- `src/prompts/base.py`, `src/prompts/orchestrator.py`, `src/prompts/answerer.py`, `src/prompts/__init__.py`
- `data/skills.yaml` (10 skills)
- `data/capability_matrix.yaml` (8 local + 5 OpenRouter seeded)

The surrounding code imports and uses them. If you think they need changes,
STOP and note in CHECKPOINT.md under "Open Questions" with `[NEEDS PLANNING AGENT]`.

## The Context Handoff Regime (65% Rule)

Long-running builder sessions will eventually fill context. Droid shows your
context usage somewhere in the UI (status line / settings panel). The rule:

**When you reach ~65% context usage OR you finish a slice (whichever comes first):**

1. Finish your current atomic task if close to done; otherwise pause cleanly.
2. Commit all work (git add specific files, git commit).
3. Create a zip backup (see HANDOVER_TO_BUILDER.md Step 4).
4. Update `CHECKPOINT.md` with where you left off (one precise paragraph).
5. **Overwrite `SESSION_HANDOFF.md`** using the template below — fresh file,
   information-dense, self-contained.
6. Tell the user: "Context approaching 65%. Recommend starting a new Droid
   session and typing: `Read AGENTS.md and SESSION_HANDOFF.md, then continue.`"
7. Wait for the user. Do not start new tasks.

**In the new session:** the new builder reads AGENTS.md (this file),
then SESSION_HANDOFF.md, picks up exactly where the previous session left off,
and will eventually repeat this regime when IT hits 65%. Bootstrapping.

## SESSION_HANDOFF.md Template

Use exactly this structure when you write the handoff:

```markdown
# Session Handoff — {ISO timestamp}

## Context At Write Time
- Usage: ~65%
- Model: Claude Sonnet 4.6
- Reasoning: High (or Max for that session)

## Where I Left Off (one precise paragraph)
{Exact file and function the next builder should open and the immediate
 action to take. Assume the next builder has never seen this project except
 through AGENTS.md + this handoff.}

## Just Completed (this session)
- Task {id}: {one line}
- Task {id}: {one line}

## In Progress (if any)
- Task {id}: {file path}, {line range or function name}, {state}
- If I stopped mid-task, note the exact cliff: "file X line Y, need to
  implement function Z with signature ...".

## Next Task (specific)
- BUILD_PLAN.md Slice {n}, task {id}
- First concrete action: {exact command or file to create/edit}

## Files To Read In New Session (minimal set)
1. AGENTS.md
2. This SESSION_HANDOFF.md
3. DESIGN_PRINCIPLES.md §{relevant section only, not the whole file}
4. BUILD_PLAN.md §Slice {n} Task {id} (only that task)
5. {Any specific source files you were editing}

Do NOT read historical docs you don't need.

## Open Issues / Gotchas Discovered This Session
- {bug, ambiguity, or note — none if clean}

## Tests Status
- Total: {N} passing
- Failing: {list if any, with short reason}

## Instruction To Future Builder
Read files above in order. Continue from "Next Task". When YOUR context
reaches ~65%, repeat this regime: overwrite SESSION_HANDOFF.md using this
template, commit, zip, hand off.
```

## Commit Discipline (Parallel Commit Law)

Every `feat:` commit ships together:
- Code
- Error handling + new error codes registered in `docs/errors/error-registry.md`
- User-guide section in `docs/user-guide.md`
- Presentation bullet in `docs/presentation-content.md`
- Happy-path test + failure-path test (hitting the error code)
- `.env.example` update if any new env var

See `CODING_STANDARD_CORE.md` for the full checklist.

## Forbidden Without Planning Agent Approval

- Changing the Two Laws (see DESIGN_PRINCIPLES.md §2)
- Editing pre-written prompts/skills/matrix (see above)
- Inventing new DAG node types
- Adding new slices or reordering existing ones
- Introducing frameworks outside the tech stack in MESSAGE_TO_CLAUDE.md

If you think any of these are needed: STOP, write it into CHECKPOINT.md
"Open Questions" with `[NEEDS PLANNING AGENT]`, and continue around it.

## Go

You should now read `SESSION_HANDOFF.md` if it exists, then continue at
the point it specifies. If it does not exist, you are the first session —
proceed to read `CHECKPOINT.md` and begin at BUILD_PLAN.md **S1-00**.
