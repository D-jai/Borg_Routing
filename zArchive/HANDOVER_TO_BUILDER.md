# Handover to Builder Agent — BORG_ROUTE
> Written by: Planning agent (Session 2 reassessment + peak-context pass, 2026-04-18)
> For: The agent(s) who will write the code
> Read this AFTER `MESSAGE_TO_CLAUDE.md`, `DESIGN_PRINCIPLES.md`, and `CHECKPOINT.md`

---

## Pre-Written Artefacts — DO NOT REGENERATE

The following files were written during Session 2 while the planning agent
had peak context. They encode prompt engineering and architectural subtleties
that took many turns to converge on. **They are complete. Import them.
Do not rewrite them.**

| File | Purpose |
|------|---------|
| `src/prompts/__init__.py` | Package exports |
| `src/prompts/base.py` | `build_system_prompt()` — enforces Law 1 (governance) |
| `src/prompts/orchestrator.py` | `ORCHESTRATOR_SYSTEM_PROMPT` — unified plan/re_plan/compose |
| `src/prompts/answerer.py` | `ANSWERER_SYSTEM_PROMPT` — leaf agent contract |
| `data/skills.yaml` | 10 hand-crafted skills for the Skill Library |
| `data/capability_matrix.yaml` | Seeded with 8 local + 5 OpenRouter models |

These are the highest-leverage artefacts in the project. Changing them
without a full architectural review breaks the system.

---

## What You Are Building

BORG_ROUTE is an Adaptive Recursive Agent Router. The product thesis, laws,
and patterns live in `DESIGN_PRINCIPLES.md`. The slice-by-slice task list is
in `BUILD_PLAN.md` (extreme-spec for Slices 1 + 2).

**Do not redesign. Do not add features. Build exactly what is specified.**

**Your starting point:** Slice 1 — Registry Foundation. Start at S1-00
(pyproject.toml + main.py).

## Model Selection — ONE Model For The Whole Build

**Use Claude Sonnet 4.6, reasoning effort High, for every session.**
Do not switch models during the build. Per Factory's docs this model gets
Max reasoning at a 1.2× price multiplier — the right combination of depth
and cost for BORG_ROUTE.

Set it once via `/model` (or Shift+Tab → Settings → Model) at the start of
your first session. It persists across sessions.

## Per-Task "Builder: Heavy/Mid/Light" Annotations

Each task in BUILD_PLAN.md has a `**Builder:**` line. These are NOT model
switch instructions (we're using one model). They are **care-level hints**:

- **Heavy** — architecturally subtle. Before writing: re-read the relevant
  DESIGN_PRINCIPLES section. After writing: re-check against the Two Laws.
  Consider bumping reasoning effort to **Max** for this task only (via
  `/model` → Reasoning effort). Tasks: orchestrator engine S2-11,
  executor S2-06, fold-in S2-10, registry validators, skill library,
  API gateway, MCP server.
- **Mid** — structured but non-trivial. Follow spec exactly. Reasoning High.
- **Light** — wiring and boilerplate. Fast execution. Reasoning High still,
  but you can move quickly.

## The Context Handoff Regime — MANDATORY

Sessions fill context. When they do, you must hand off cleanly. This is not
optional; it's how the project makes multi-session progress without
re-reading everything.

### The 65% Rule

**When your context reaches ~65% OR you finish a slice (whichever first):**

1. Finish your current atomic task if close; otherwise pause cleanly.
2. Commit all work (specific files, not `git add .`).
3. Create a zip backup (Step 4 below).
4. Update `CHECKPOINT.md` with precise "Where I Left Off" paragraph.
5. **Overwrite `SESSION_HANDOFF.md`** using the template in `AGENTS.md`.
6. Tell the user: "Context at 65%. Recommend starting a new Droid session
   and typing: `Read AGENTS.md and SESSION_HANDOFF.md, then continue.`"
7. Wait for the user. Do NOT start the next task.

### What The Next Session Does

New session reads `AGENTS.md` (auto-loaded by Droid) then `SESSION_HANDOFF.md`.
These two files plus the specific source files the handoff lists are
**all** the context needed. The new session does NOT re-read
MESSAGE_TO_CLAUDE / DESIGN_PRINCIPLES / BUILD_PLAN in full — just the
relevant sections the handoff points to.

This is recursive bootstrapping: each session hands off to the next via the
same file. The chain continues until the project is built.

### How To Check Your Context Usage

Droid exposes context usage in the UI — typically in the status line or
settings panel. If you can't find it, ask the user to tell you. When in
doubt, hand off early rather than late — a short session with a clean
handoff is better than a session that ran out mid-task.

---

## How to Work

### Before Every Session
1. Read `CHECKPOINT.md` — "Where I Left Off" tells you exactly where to start
2. Read `DESIGN_PRINCIPLES.md` — the laws and patterns are non-negotiable
3. Read only the current slice section in `BUILD_PLAN.md`
4. Run existing tests — note the count before you touch anything

### During a Session
- Build one task at a time. Complete it before starting the next.
- Follow `CODING_STANDARD_CORE.md` — especially the Parallel Commit Law:
  every `feat:` commit ships code + error handling + tests + doc entries together.
- Register every new error code in `docs/errors/error-registry.md` as you create it.
- Add test cases to `docs/tests/test-plan.md` as you build.
- If you discover a design gap or ambiguity — stop. Note it in `CHECKPOINT.md`
  under "Open Architectural Questions" and continue around it. Do not invent
  architecture; the planning agent will resolve it.

### The Two Laws — Never Violate
1. **Uncompacted Governance.** Global Rules, user instructions, and admin-added
   system instructions pass verbatim through every cascade. Enforce at
   `src/prompts/base.py::build_system_prompt()`.
2. **No Forgotten Scratch.** Child agents clean their own scratch. The executor
   verifies at every fold-in boundary; residue → `child_residue` event + sweep.

### After Every Session — Non-Negotiable Housekeeping

**Step 1 — BUILD_PLAN.md**
Check off every task you completed. Do not check off partial work.

**Step 2 — CHECKPOINT.md**
Update ALL of the following:
- "Where I Left Off" — one paragraph. Be precise. Name the exact file and
  function the next agent should pick up from. Assume the next agent has never
  seen this project.
- Slice Status table — update status of the slice you worked on
- "What Works" — add any verified working feature
- "What Has Never Been Tested" — add anything you wrote but didn't fully verify
- "Open Architectural Questions" — add anything you found but didn't resolve
- Session History — add a row: date, session number, work done, test count

**Step 3 — Commit**
```bash
git add <specific files only — never git add .>
git commit -m "feat: <what this does>"
# or fix:, chore:, test:, docs: as appropriate
```

**Step 4 — Backup (no GitHub yet)**
GitHub remote is deferred. Use zip snapshots for backup instead:

```bash
# At the end of each session, before stopping:
# Replace {slice}/{session} with current slice number + session number
powershell Compress-Archive -Path C:\stage\borg_route\* `
  -DestinationPath C:\stage\borg_route_backups\borg_route_s{slice}_sess{session}_$(Get-Date -Format yyyyMMdd).zip `
  -Force
```

When a slice is completed, create a named snapshot:
`borg_route_SLICE{N}_COMPLETE_yyyyMMdd.zip`

GitHub setup can happen later; until then, zip snapshots at session end are
the source of truth for rollback. Keep at least the last 5 snapshots plus
every slice-complete snapshot.

---

## Architecture Constraints — Do Not Violate

These were locked in during the Session 2 reassessment:

| Constraint | Rule |
|-----------|------|
| Answerer contract | `type=answerer` tasks MUST return `resolved` or `failed`. Never `unresolved`. Raise `BRT-ANS-001` if violated. |
| Governance | Every single model call goes through `build_system_prompt()` in `src/prompts/base.py`. Governance is uncompactable and additive. No exceptions. |
| Logging | Every model call writes a log event. No silent model calls. |
| Depth hard cap | `MAX_RECURSION_DEPTH = 5` is a constant in code. Admin UI cannot exceed it. |
| Context isolation | Answerers receive only their distilled context slice. Never the full thread. |
| Unified brain | The orchestrator prompt is ALSO the composer prompt. One file: `src/prompts/orchestrator.py`. Mode flag (`plan|dispatch|re_plan|compose`) distinguishes turns. No separate `composer.py`. |
| run() uniformity | `src/agents/executor.py::execute()` handles all four types (`orchestrate`, `answerer`, `apply_to_each`, `state_op`). No type-specific callers. |
| Children self-clean | Agent prompts explicitly instruct scratch cleanup. Parents verify via fold-in sweep. Residue → `child_residue` event, then swept. |
| Secrets | In `.env` only. Never in code or committed to git. |

---

## Key Files Quick Reference

| File | Purpose |
|------|---------|
| `MESSAGE_TO_CLAUDE.md` | Architecture at a glance — read first every session |
| `DESIGN_PRINCIPLES.md` | Laws, patterns, rationale — essential depth |
| `BUILD_PLAN.md` | Slice-based task list — 6 slices |
| `CHECKPOINT.md` | Current state — update every session |
| `CODING_STANDARD_CORE.md` | Coding rules — always apply |
| `docs/errors/error-registry.md` | Register every `BRT-*` error code here |
| `docs/tests/test-plan.md` | Add test cases as you build |
| `.env.example` | Add every new env var here as you introduce it |
| `C:\Stage\memory\STAGE_MEMORY.md` | Stage-level memory — update when a slice completes |

---

## When a Slice Is Complete

1. All tasks in that slice checked off in `BUILD_PLAN.md`
2. All tests for that slice passing
3. `CHECKPOINT.md` updated — slice marked ✅ Complete
4. `docs/errors/error-registry.md` — all new error codes registered
5. `docs/tests/test-plan.md` — all new test cases documented
6. `.env.example` — any new env vars added
7. Clean commit: `feat: complete Slice N — <slice name>`
8. Update `C:\Stage\memory\STAGE_MEMORY.md` — append a dated entry
9. Update `C:\Stage\CLAUDE.md` Active Projects table — update BORG_ROUTE status

---

## Questions and Ambiguities

If you encounter a genuine architectural ambiguity not covered in
`DESIGN_PRINCIPLES.md` or `MESSAGE_TO_CLAUDE.md`:
- Do NOT invent a solution
- Add it to `CHECKPOINT.md` under "Open Architectural Questions" with label
  **[NEEDS PLANNING AGENT]**
- Continue building around it
- The planning agent will resolve it in the next planning session

The currently-known open questions are already listed in
`DESIGN_PRINCIPLES.md §9` and `CHECKPOINT.md`. Some may affect your work on
Slice 2 specifically. If so, surface them before coding that slice.

---

## Current Status When You Start

- Slice 0: ✅ Complete
- Slice 1: 🔲 Not started — **start here at S1-00**
- No tests yet
- No functional Python code yet (prompts as string constants count as artefacts, not runnable code)
- GitHub remote: **deferred**. Use zip snapshots (see Step 4 above) for backup.
- `data/capability_matrix.yaml` already seeded with the user's local lineup
  (8 LM Studio models) and 5 recommended OpenRouter externals. Scores are
  honest pre-benchmark estimates and will be refined during S1-07 benchmark runs.
- `data/skills.yaml` already has the initial 10 skills. Slice 2 uses them as-is.
- `src/prompts/*` already written. The S2-06 executor and S2-11 engine
  import from these, don't regenerate.
