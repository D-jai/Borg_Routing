# CHECKPOINT: BORG_ROUTE

Where we left off. Updated by whoever worked last. Dates are Toronto time.

## Status (16-Nov-2026)

Build under way, run by the lead session (`borg-route-f1`, Opus 5.5) since 02:20 on 16-Nov-2026. The Fable sitting ended cleanly; all its commits were reviewed by fresh agents, their defects fixed, and re-reviewed with no objection.

| Work order | State |
|---|---|
| 0.1, 0.2, 0.3 | Done and reviewed (0.1 fixed in `3c62112`) |
| 1.1, 1.2, 1.3, 1.4 | Done and reviewed (fixes `917cba3`, `30a3071`, `e1861e2`) |
| 1.5, 1.6, 1.7 (+R14), 1.8, 1.9 | Done and reviewed (see build log) |
| 1.10 Check 1 | **Passed on attempt 3** (after planner rulings 1-3 and ruling 9). Skill placeholder bug found in the trace; fix in progress |
| 1.11 failure reasons to re_plan; ruling 1a inline items and refs | Done and reviewed |
| 4.4 Admin lock | Done and reviewed |
| 2.1 on-premise lane design | Done: attacked by a fresh reviewer (24 findings), revised `8dee12c`, `13a1799` |
| 2.2, 2.3 lane build and privacy test | **Done** after two review rounds (Check 2). Full suite **919 passed** at `99bba7d` |
| 4.1 shared interface assets | Done and reviewed |
| 5.7 MCP | Done and reviewed |
| 4.3 Admin pages | Done after two review rounds |
| Everything else | Open |

If this session stopped abruptly: `git log` and `git status` show what landed; any uncommitted changes in `src/` or `ui/` belong to the in-progress builders above: read the diff, finish or stash it, record which in the build log.

**Next action for the lead:** skill placeholder fix (in progress), then the Phase 1 phase-end full run and push, then 2.4 (by_reference documents), then 3.1 (Ollama in Docker, pool), 3.2, 3.3. Waiting on Deej via the planner: one protected-line change to the apply_to_each shape example (`inputs_ref` comment). Work that needs proof results (3.x, 4.2 receipt numbers, 4.5 replay, 5.1 README numbers, 6.1 deck numbers) waits for Check 1, which waits for the planner's answer. A 5-hour usage limit stopped agents at about 07:00 on 16-Nov-2026; it reset at 07:10.

## Start the build (Deej)

1. Open a new Claude Code session in `C:\Stage\BORG_ROUTE`.
2. Set the model to **Opus 5.5**, effort **medium**, auto mode on.
3. Paste this one line:

   > You are the lead for the BORG_ROUTE showcase build. Read `CHECKPOINT.md`, then `BUILD.md`, and run the work orders in order from the first open one. Do not ask me design questions; the design is settled in `BUILD.md`.

Keep the window open and the machine awake. A new session can pick up at any time from this file and the build log in `BUILD.md` section 9.

## What this build is

BORG_ROUTE (Brokered Orchestration, Retrieval and Generation) takes a prompt, lets a planner model break it into steps, and is meant to send each step to the cheapest model that can do it. The review on 15-Nov-2026 found that it does not do that yet: one model does every step, and cost is never recorded. The build:

| Phase | What | Check that closes it |
|---|---|---|
| 0 | Baseline: settle the uncommitted work, count the tests | counts in the build log |
| 1 | Make the claim true: per-step routing, cost, reasons recorded | claim test and a live run with two models and a real bill |
| 2 | On-premise lane: raw documents only to local models | privacy test |
| 3 | Proof: four real jobs both ways, US$20 ceiling | results published; at least one job won, or the planner is called |
| 4 | Interface: Workspace and Admin, replay mode | interface check and screenshot review |
| 5 | Package: README, licence, one-command install, CI, claims check | a stranger's clean install from the README |
| 6 | Presentation and video script | claims check on the deck |

After the build, on Deej's word only: make the repo public, deploy the replay demo on the VPS, LinkedIn post, video.

## Deej's rulings (15-Nov-2026)

1. Audience: companies that want AI on their own machines; client data stays in the office.
2. A small additive edit to the protected `src/prompts/orchestrator.py` is allowed (work order 1.3 only).
3. Proof spend ceiling: US$20 on OpenRouter; stop and report if reached.
4. Hosted watch-only replay demo on the VPS: yes, on his go-ahead.
5. Build first, post later.
6. Deej spends no time testing; every check is done by the build.
7. Opus 5.5 and Sonnet 5.5, both at medium effort, do the work. Fable plans only.
8. LM Studio removed (evening of 15-Nov-2026). The local model runs as Ollama in Docker; see `BUILD.md` section 8.
9. A second small additive edit to the planner prompt, for the list hand-over, work order 1.10 only (16-Nov-2026; Deej's yes given in the planner's session and relayed by the planner): one `items` line in the apply_to_each JSON shape and one "Where the items come from" paragraph in `src/prompts/orchestrator.py`.

Standing permissions: local commits on `showcase`; pushes of `showcase` to the private `origin`; merge of its pull request into `master` when CI is green and the reviewer has no objection.

## Where things are

| Thing | Where |
|---|---|
| Work orders, rules, rulings log, build log | `BUILD.md` |
| Interface look (open in a browser) | `docs/design/ui_reference.html` |
| Presentation outline | `docs/design/deck_outline.md` |
| Two Laws and invariants | `DESIGN_PRINCIPLES.md` |
| Key | `.env` (git-ignored). OpenRouter key checked working 15-Nov-2026; the key's own limit is US$30, nothing used. |
| Remote | `origin` = `github.com/D-jai/Borg_Routing`, private |

## State of the tree when planning ended

- Branch `showcase`, created from `master` at `f41da41`.
- Ten modified files and one new ledger from April (the token-retry work) are still uncommitted. Work order 0.1 settles them. `NOTES.md` is untracked.
- Last recorded test counts disagree across docs (256, 296, 388, 391). Work order 0.2 gets the real number.

## Blocked

(none open; the entry below is resolved: Check 1 passed on attempt 3, 16-Nov-2026)

- **Resolved: 1.10 Check 1 (16-Nov-2026).** Both live runs (temp DBs `borg_smoke_vksjf2ab`, `borg_smoke_uegi1bfr` under `%TEMP%`) failed the same way: the planner points an `apply_to_each` (attempt 1: `inputs_ref: "reviews"`) or a skill step (attempt 2: `extract_fields` with `items_ref: "reviews_list"`) at a list that exists only inside the operator's message. Nothing resolves it, the step runs no model, its dependants never run, and re_plan composes the whole answer on the planner model. Routing per step works (route decisions chose `gpt-5.4-nano` for the extraction step); the steps just never get their items. **When resuming:** `scripts/smoke_live.py` must check its cheap models (`deepseek-v3.2`, `gpt-5.4-nano`, both live on OpenRouter) in a temporary copy of the matrix; the shipped `data/capability_matrix.yaml` keeps only the two cold-start defaults checked (`tests/registry/test_cold_start_defaults.py`).

## Needs Deej

- **Answered: yes (ruling 9, relayed by the planner, 16-Nov-2026); edit made.** The original item, kept for the record: yes or no on the planner's protected-prompt edit (ruling 1b). If yes, add to `src/prompts/orchestrator.py`, apply_to_each section only, additions only:
  - After the `inputs_ref` line in the JSON shape: `"items": ["only when the list is in the user's message: the items themselves, one string each"],` (check brace doubling: it sits inside a `{{ }}` block).
  - After "You handle the failures in re_plan.": "Where the items come from. If the list is in the user's message, copy the items into `items`, one string per item, and leave `inputs_ref` out. If an earlier step produces the list, set `inputs_ref` to that step's id. `inputs_ref` is always the id of a step; a name you make up resolves to nothing. The same holds for a skill's `items_ref`."
  - Then a prompt test, then Check 1 attempt 3 (smoke prompt unchanged).

## Questions for Deej after the build (not blocking)

- **Spend control.** Anyone who can reach the app can start paid runs (`/v1/task`, chat, MCP, "Run again"); there is no notion of who owns a run, so anyone can also read every run. The admin lock covers settings and sensitive logs only. Options: leave it (single-operator install), lock all paid entry points behind the token, or a daily spend cap. Raised by the 4.4 reviewer, 16-Nov-2026.

## Needs planner

- **Answered 16-Nov-2026** (planner rulings 1 and 2 in `BUILD.md` section 8; new work order 2.4). The question was kept below for the record.
- **How does the planner hand a list that lives in the operator's message to a step? (16-Nov-2026, for 1.10.)** The planner prompt says `inputs_ref` "is resolved from run state" and never says that data in the user's message must go in the node's inline `items`, or be produced by an earlier step. Options the lead sees:
  - **A (lead's recommendation):** add guidance to `src/prompts/orchestrator.py` (and the `items_ref` wording for skills): "a list that is in the user's message goes in `items` inline; `inputs_ref` names an earlier step's id only". A protected-file edit beyond 1.3, so it needs the planner.
  - **B:** a reserved ref (for example `"input"`) meaning the operator's message, split by the runtime. Fragile: the runtime must guess item boundaries.
  - **C:** a runtime fallback that runs the step once over the operator's message. Tried (`a78f6cc`), reverted (`cd9d910`): it covered only the plain path, and it fired early for references to steps in the same plan.
  - Independently of the choice: an `inputs_ref` naming a step in the same plan should count as a dependency (the ready check waits only on `depends_on`); found by the review of `a78f6cc`.
