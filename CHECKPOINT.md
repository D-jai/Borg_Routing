# CHECKPOINT: BORG_ROUTE

Where we left off. Updated by whoever worked last. Dates are Toronto time.

## Status (17-Nov-2026)

Lead session `borg-route-f1` (Opus 5.5) since 02:20 on 16-Nov-2026. Every work order below went through fresh review rounds; the build log (`BUILD.md` section 9) has each row.

| Work order | State |
|---|---|
| Phase 0, Phase 1 (1.1-1.12), Check 1 | Done and reviewed. Check 1 passed on attempt 3 |
| Phase 2 (2.1-2.4, incl. by_reference) | Done and reviewed (Check 2) |
| 3.1 pool, 3.2 bench | Done and reviewed; local model llama3.2:3b in Docker (`borg_ollama`) |
| **3.3 bench round 1 (Check 3)** | **Failed: BORG_ROUTE won no job** (5 of 6 BORG runs failed on product defects; B at 92% of direct cost). Published as it fell: `bench/results/2026_11_17/summary.md`; stage 2 skipped by ruling 19. Spend US$2.56 of US$20 |
| Stage-1 crash fixes R1-R4 (ruling 20) | Done and reviewed; replayed stage-1 replies now end "salvaged" |
| 4.1, 4.2, 4.3, 4.4 interface | Done and reviewed. Receipt baseline estimate switched off (ruling 18) |
| 5.7 MCP | Done and reviewed |
| 5.3 clean tree, 5.4 docs match the code, 5.5 claims checker (user guide; README and deck rows to come) | Done and reviewed |
| 5.6 CI | **Green** on `showcase` (run 44244966079, 18-Nov-2026: pytest on 3.11 and 3.13, strict UI check, claims check). The compose-in-replay job waits for 5.2 and 4.5. CI is paid from a small budget: see the CI rule below |
| 4.5 replay, 4.6 interface check, 5.1, 5.2, 5.5, 5.8, 6.1 | Open. 4.5, 5.1 numbers and 6.1 numbers depend on the round-2 decision |

If this session stopped abruptly: `git log` and `git status` show what landed; any uncommitted changes belong to an in-progress builder: read the diff, finish or stash it, record which in the build log.

**Bench round 2: Deej said yes (his ruling 11, 18-Nov-2026); the planner's ruling 27 sets the rules (BUILD.md section 8).** Same rules as round 1. Freeze the tree, then reseed jobs A, C and D with the first 8 hex digits of the freeze hash read as a number (`bench/make_data.py --seed`). Job B is hand-written and stays. No paid rehearsal; cap US$6. Results go to the planner first.

**CI rule (Deej, 17-Nov-2026):** Actions minutes now cost money (US$15 shared across D-jai repos until 30-Nov-2026, 7 pm Toronto). Test locally, push only when green and once per ready batch, never re-run a failed CI run, put `[skip ci]` on docs-only pushes, no manual full CI runs without Deej's go.

**Round-2 freeze (18-Nov-2026, 10:45, ruling 29):** this commit is the freeze. Everything before it was reviewed and the full suite passed on `01a72c4` (1964 passed). Rulings 12, 13 and 28 are in, and demo job 1 is recorded (run d04e0d8a). Next: data-only commit (the SEED line set to int(freeze hash[:8], 16), then regenerate bench/data/ for A, C and D), tests/bench, one push, CI green, the invariant check pasted into BUILD.md, then round 2 stage 1 (cap US$6). Report to the planner at the end of stage 1.

**Earlier next action (18-Nov-2026, 08:20):**
- Done: rulings 21-23, plus 25 and 26 (26e/f/g); commits up to `62e4ca0` are reviewed. The full local suite on `62e4ca0` passed 1916, 0 failed.
- Ruling 24 demo runs: job 2 and job 3 are recorded. The job 1 re-record was stopped at 08:16: the router put its 60 items on the CPU model, and they queued for about an hour each. The planner is choosing how job 1 should run.
- Waiting on the planner: go or hold for the round-2 freeze, and its two protected-file questions to Deej.
- After that: freeze commit, data-only reseed commit, one push, then the round-2 run.
- 4.5 replay and 5.2 carry on meanwhile. Open: 5 tests in `tests/agents/test_http_pool.py` fail when run after the lane, cost and bench files (they also fail on the base tree, pass alone, and pass in CI order).

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
10. One existing example line in the planner prompt changed so it agrees with ruling 9's paragraph (16-Nov-2026; Deej's yes relayed by the planner): the apply_to_each `inputs_ref` example now reads "id of the earlier step that produces the list (omit when you give items)".

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

- **GitHub Actions billing (17-Nov-2026): Deej added a US$15 budget, so CI can run again within the CI rule above.** The first CI run (work order 5.6, `.github/workflows/ci.yml`) did not start: "The job was not started because recent account payments have failed or your spending limit needs to be increased" (Billing & plans in the D-jai account). Until that is fixed, 5.6 cannot go green and full test runs stay local. The run: https://github.com/D-jai/Borg_Routing/actions/runs/44192378709

- **Cold-start defaults after the 17-Nov rescore.** The research route now scores Kimi K2 Thinking at 8.7 reasoning, below the 9.0 floor the shipped defaults must meet (`tests/registry/test_cold_start_defaults.py`). The shipped matrix keeps the April scores for now; the rescored pool lives in `bench/pool_matrix.yaml`. Options: keep as is, or pick a new second default (e.g. Gemini 3.1 Pro 9.7 or Grok 4.20 9.4 by the new scores) and update the test.

- **Local model on this machine (17-Nov-2026): resolved by planner ruling 13, attempt 3.** `llama3.2:3b` serves from Ollama in Docker (`borg_ollama`, kept loaded, 16k context); the local arms run. It is a small CPU-only model (reasoning 4.6), so mid-tier raw steps fail by design. A stronger local model needs better hardware (native Ollama, or the DGX Spark). The container stays running; `docker rm -f borg_ollama` removes it (the `borg_ollama` volume keeps the models).

- **Spend control.** Anyone who can reach the app can start paid runs (`/v1/task`, chat, MCP, "Run again"); there is no notion of who owns a run, so anyone can also read every run. The admin lock covers settings and sensitive logs only. Options: leave it (single-operator install), lock all paid entry points behind the token, or a daily spend cap. Raised by the 4.4 reviewer, 16-Nov-2026.

## Needs planner

- **Check 3: BORG_ROUTE won no job in bench stage 1 (17-Nov-2026).** Spend US$2.56 of US$20 (incl. US$0.07 scoring). Results in `bench/results/2026_11_17/` (run databases in `dbs/`, git-ignored). Direct arm completed A, B, C. BORG: B completed (US$0.302 vs direct US$0.328 = 92%, not <= 70%, quality equal on checks); A cloud, A local, C cloud, D cloud, D local FAILED. Stage 2 NOT started (it would re-run a crashing path); nothing tuned. Diagnosis (no fix made):
  1. **A cloud:** the 200 items were classified correctly and cheaply (two apply_to_each, about US$0.19 on gemini-3.1-flash-lite). The planner (grok-4.20) then asked for state_ops the engine lacks (`join`; `filter` without `key`), those failed, and the compose call returned the re_plan shape (`{"action": "compose", "rationale": ...}`) instead of `{"response": ...}` → BRT-ORC-009, no retry for a wrong-shape compose reply. The work was done; the run died at the last step.
  2. **D cloud and both local arms:** same final failure (BRT-ORC-009) after classification steps.
  3. **C cloud:** engine crash `'str' object has no attribute 'get'` during orchestration (a type bug).
  4. Bench reporting: per-step `dollars` in result files show 0.0 for apply_to_each steps although the envelopes carry dollars (US$0.098, US$0.093); totals come from logged rows and are right.
  Question for the planner: are these product defects to fix before a new dated run (the ones above are crashes and a missing compose-shape recovery, not floors or prompts), or do the stage-1 results stand as the proof result? Options: (a) fix the defects (compose-shape recovery, state_op vocabulary or a clear error the planner sees, the type crash), review, then a fresh full stage 1 in a new dated folder, keeping these results published as "first run"; (b) results stand: README and deck report that BORG did not win and why.
- **Answered 16-Nov-2026** (planner rulings 1 and 2 in `BUILD.md` section 8; new work order 2.4). The question was kept below for the record.
- **How does the planner hand a list that lives in the operator's message to a step? (16-Nov-2026, for 1.10.)** The planner prompt says `inputs_ref` "is resolved from run state" and never says that data in the user's message must go in the node's inline `items`, or be produced by an earlier step. Options the lead sees:
  - **A (lead's recommendation):** add guidance to `src/prompts/orchestrator.py` (and the `items_ref` wording for skills): "a list that is in the user's message goes in `items` inline; `inputs_ref` names an earlier step's id only". A protected-file edit beyond 1.3, so it needs the planner.
  - **B:** a reserved ref (for example `"input"`) meaning the operator's message, split by the runtime. Fragile: the runtime must guess item boundaries.
  - **C:** a runtime fallback that runs the step once over the operator's message. Tried (`a78f6cc`), reverted (`cd9d910`): it covered only the plain path, and it fired early for references to steps in the same plan.
  - Independently of the choice: an `inputs_ref` naming a step in the same plan should count as a dependency (the ready check waits only on `depends_on`); found by the review of `a78f6cc`.
