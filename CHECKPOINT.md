# CHECKPOINT: BORG_ROUTE

Where we left off. Updated by whoever worked last. Dates are Toronto time.

## Status (16-Nov-2026)

Build under way, run by the lead session (`borg-route-f1`, Opus 5.5) since 02:20 on 16-Nov-2026. The Fable sitting ended cleanly; all its commits were reviewed by fresh agents, their defects fixed, and re-reviewed with no objection.

| Work order | State |
|---|---|
| 0.1, 0.2, 0.3 | Done and reviewed (0.1 fixed in `3c62112`) |
| 1.1, 1.2, 1.3, 1.4 | Done and reviewed (fixes `917cba3`, `30a3071`, `e1861e2`) |
| 1.8 local addresses | Built `8501113`; review fixes in progress (builder) |
| 1.9 Law 2 note | Done `13cf807` |
| 1.6 record cost | In progress (builder) |
| 1.5 record why, 1.7 demo-path bugs (+ R14), 1.10 Check 1 | Open |
| 2.1 on-premise lane design | Done: attacked by a fresh reviewer (24 findings), revised `8dee12c`, `13a1799` |
| 2.2, 2.3 and everything after | Open |

If this session stopped abruptly: `git log` and `git status` show what landed; any uncommitted changes in `src/` belong to the in-progress builders above: read the diff, finish or stash it, record which in the build log.

**Next action for the lead:** 1.8 fixes and 1.6 review, then 1.5, 1.7, 1.10 (Check 1, live OpenRouter run), phase-end full run, push.

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

(none)

## Needs Deej

(none)

## Needs planner

(none)
