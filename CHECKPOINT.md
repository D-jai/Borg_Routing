# CHECKPOINT: BORG_ROUTE

Where we left off. Updated by whoever worked last. Dates are Toronto time.

## Status (15-Nov-2026)

Build under way. Phase 0 and the hard parts of Phase 1 are done, and the on-premise lane is designed. This was built by Fable on the evening of 15-Nov-2026 at Deej's request while the Opus weekly limit was spent. **The Fable sitting has ended; nothing is in progress and the tree is clean.**

| Work order | State |
|---|---|
| 0.1, 0.2 | Done (`13f4f4f`, `ff4398c`). Baseline: 391 tests passed |
| 0.3 permissions preflight | Open (lead) |
| 1.1 claim test, 1.2 per-step routing | Done (`38efc23`), not yet reviewed |
| 1.4 Law 1 repair | Done (`30af83d`), not yet reviewed |
| 1.3 planner labels | Done (`87f1a8e`), not yet reviewed |
| 1.5 record why, 1.6 record cost, 1.7 demo-path bugs, 1.8 local addresses, 1.9 Law 2 note, 1.10 Check 1 | Open. All routine except 1.10 |
| 2.1 on-premise lane design | Written: `docs/design/on_premise_lane.md`. Not yet attacked by a fresh reviewer |
| 2.2, 2.3 and everything after | Open |

Last full test run (at `87f1a8e`): **437 passed, 2 expected failures** (the cost and streaming parts of the claim test, which work orders 1.6 and 1.7 turn green).

**Next action for the lead (Opus 5.5):**

1. Send each Fable commit (`13f4f4f`, `38efc23`, `30af83d`, `87f1a8e`) to a fresh reviewer agent with the brief in `BUILD.md` section 4. Fix what they find before building on it.
2. Send `docs/design/on_premise_lane.md` to a fresh reviewer with the question at the end of that file.
3. Read the builder's notes under the build log (`BUILD.md` section 9).
4. Carry on from 0.3, then 1.5.

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
