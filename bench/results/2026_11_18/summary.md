# Bench round 2, 18-Nov-2026: summary

**Verdict: BORG_ROUTE won no job, and round 2 closed at stage 1 as lost (planner ruling 31).** BORG's answer failed or lost on jobs A, B and C. Job B is void under the rule. Job D has no verdict because its local arm was not run. Every job is a single run, so every number here is one run, not a median.

The rules are the ones in `bench/README.md`, word for word, as in round 1. Only the product code (rulings 20 to 28 and the two protected edits listed below) and the test data differ from round 1. Nothing in the rules was changed after seeing a number.

## What is new data and what is not

- Jobs A, C and D use new data, generated from a new seed (see the freeze below). All arms, direct included, ran on it.
- Job B is not fresh: same input as round 1; the fixes were built after seeing round 1's result on it. Its input is hand-written and was not regenerated. It was run and is reported under the same rules.

## Freeze, seed and invariant

- Freeze commit: `f655890174eacf280e7ae297bef38288c5fdb830` (BUILD.md and CHECKPOINT.md only).
- Seed: the first 8 hex digits of the freeze hash read as a number, int("f6558901", 16) = **4132800769**. Data commit `2c4d12b` (jobs A, C and D only).
- Every round-2 run used head `8a304f5`. Two later commits (`8c8f878`, `8a304f5`) are test-only fixes for row-order assumptions in two tests, made after CI failed on the data commit.
- Invariant, checked before the first round-2 call: `git diff f655890 HEAD --stat -- src data ui` printed nothing. `git diff f655890 HEAD --name-only -- bench tests` listed only `bench/make_data.py` (the SEED line, 1 line changed), 278 files under `bench/data/` (jobs A, C and D), `tests/registry/test_direct_floor.py` (the one pinned test), and test-only fixes for row order (`tests/orchestrator/test_engine_truncation_retry.py`, `tests/e2e/test_stage1_rootcause_bench.py`). That is more than the "at most the one pinned test" of ruling 29c; ruling 29b allows a test-only fix after a CI failure. CI on `8a304f5` (run 44342995355) was green.
- Protected-file edits approved by Deej before the freeze, listed here as changes since round 1:
  - Ruling 12: `b99f0af`, `classify_into_buckets` in `data/skills.yaml` gets `recommended_specialty: extraction` (was null).
  - Ruling 13: `0897977` and `27dfdb8`, seven lines in the planner prompt (`src/prompts/orchestrator.py`) listing each state_op's params. A drift test pins them to the code.
- The other product changes since round 1 are in `bench/results/round_1_vs_round_2.md`.

## The runs, split by a restart

Stage 1 was split by a restart of the ThinkPad.

- **Part 1, 11:15 to 12:37 Toronto time:** direct arms of A, B and C; `borg_cloud` of B and D; the re-estimate. D `borg_local_workers` was started and stopped (below). The start-of-stage speed probe of the local model failed (BRT-REG-016), so the local model was given a fallback price. The part 1 result files show the local speed as "not measured, BRT-REG-016".
- **Part 2, 14:10 after the restart:** `borg_cloud` of C, then A. The probe measured `llama3.2:3b` at 7.062 tok/s on this side.
- **Stopped run:** D `borg_local_workers` was stopped by the operator after about 70 minutes for a machine restart, no result. It had answered 12 of 32 local item calls and no subtask had finished. Its database was moved to `C:\Stage\_archive\2026-11-18-borg_route_r2_stopped_run\`. It was not rerun, because ruling 31 closed the round.
- **Not run:** A `borg_local_workers` and D `borg_local_workers`. These are neither a pass nor a fail. Reasons (ruling 31a): A's local arm shares the composition path that failed in A's cloud run; D's local run could not finish inside the 7,200 s cap at the stopped run's rate (12 of 32 item calls in about 67 minutes, with the part 1 host also stalling); and Deej's machine rule of 18-Nov-2026 that heavy work does not run on the ThinkPad. Host for the local arm: the ThinkPad CPU.
- **Stage 2 not run:** no arm could win after stage 1.

## Results, one run per cell

Direct arm: one call to `anthropic/claude-opus-5.5` (US$4 / US$20 per million tokens), unchanged from round 1. "% of direct" is BORG's cost divided by the direct arm's cost for the same job. It is a fact about the bill, not a saving: it is quoted for every row, including rows where the answer failed or lost.

| Job | Arm | Cost (US$) | % of direct | Quality | Calls | Input / output tokens | answer_kind | Verdict |
|---|---|---|---|---|---|---|---|---|
| A, 200 support emails | direct | 0.2294 | n/a | accuracy 100% (200 of 200 graded) | 1 | 16,659 / 8,139 | n/a | n/a |
| A | borg_cloud (by_reference) | 0.2022 | 88% | **0 of 200 graded**; accuracy 0% | 201 | 395,578 / 14,417 | composed | no win: the composed answer is malformed (its own list plus code-placed rows in the skill's raw fields, not one JSON list) |
| A | borg_local_workers | not run | not run | not run | not run | not run | not run | not run (neither pass nor fail) |
| B, small Python service | direct | 0.3189 | n/a | 6 of 6 checks that need no container passed; 12 container checks skipped (Docker not serving) | 1 (two attempts: the first was cut at 7,000 output tokens) | 1,616 / 15,623 | n/a | n/a |
| B | borg_cloud | 0.2198 | 69% | answer held none of the four files; 0 of 6 checks that need no container passed (6 failed); 12 container checks skipped | 1 | 32,860 / 13,115 | composed | **void** under the rule (see below); no verdict given |
| C, 15 documents, about 150k tokens | direct | 0.8001 | n/a | chosen by the pairwise judge | 1 | 197,313 / 541 | n/a | n/a |
| C | borg_cloud (by_reference) | 0.2477 | 31% | judge chose direct; informational fact score 6 of 8 facts, 0 of 2 contradictions (direct: 8 of 8, 2 of 2) | 164 | 592,264 / 23,693 | composed | no win: the judge (`openrouter/qwen/qwen4-max`) chose direct (1 of 1 comparisons) |
| D, 60 client notes | borg_cloud (by_reference) | 0.1183 | no direct arm in stage 1 | accuracy 60% (60 of 60 answered) | 89 | 193,765 / 7,114 | composed | no verdict: D compares its local arm with its cloud arm, and the local arm was not run |
| D | borg_local_workers | not run | not run | not run (the stopped run: 12 of 32 local item calls answered, no result) | not run | not run | not run | not run (neither pass nor fail) |

Wall times: A direct 55.8 s, A borg_cloud 24.6 s; B direct 119.0 s, B borg_cloud 183.4 s; C direct 12.1 s, C borg_cloud 19.1 s; D borg_cloud 67.1 s.

Job C's fact score is informational only and plays no part in any verdict; the judge decided.

### Job D canary counts

Job D plants canary strings in the notes to show what reaches cloud models. In the `borg_cloud` run, **60 canaries were sent to cloud models** and **0 appeared in the answer**. Round 1's `borg_cloud` run sent 120. The local arm, which is the arm meant to send none, was not run in round 2, so round 2 has no new measurement of the on-premise lane on a live run.

## Win rule, per job

The rule (`bench/README.md`): BORG wins a job when its median cost is at most 70% of the direct arm's and quality is not worse. A BORG run counts toward a win only when its answer_kind is the normal composed one (ruling 27); all four BORG runs here are "composed" (read from each run database). A low cost does not make an answer that failed or lost a win.

| Job | Verdict | Why |
|---|---|---|
| A | no win | cost was 88% of direct's, over the 70% line, and quality was worse: 0 of 200 graded against 200 of 200 |
| B | void | Docker was not serving the runner from 11:19 to 11:26 Toronto time, so the container checks were skipped in both arms. Under the rule every job B result is void for both arms and no verdict is given. No rerun (ruling 31b) |
| C | no win | cost was 31% of direct's, but the judge chose the direct answer, so quality was worse |
| D | no verdict | the local arm was not run |

### Job B, plain facts beside the void

- Direct passed 6 of 6 checks that need no container, at US$0.319 in two attempts.
- BORG's answer described the four files and held none of them. It failed all 6 of those checks, at US$0.220. Its cost was 69% of direct's, which is not a saving: the answer did not contain the work.
- This is a void result with a plain observation beside it, not a loss by the rule and not a win.
- Same input as round 1; the fixes were built after seeing round 1's result on it.

## What the numbers do not show

- **Causes.** The causes of the failures above are being investigated in `docs/design/bench_round2_measurements.md` (not yet written). This report records what happened and claims no cause beyond the plain observations above.
- **One run per cell.** No medians; a different run could land differently.
- **Job B's quality.** Its container checks were skipped in both arms, so the 18-check comparison of round 1 does not repeat here.
- **The local lane.** Both local runs were not run. Nothing in round 2 adds to or subtracts from the privacy claim for the on-premise lane.
- **Fact score on C.** It is informational only.

## Spend

Ledger (`bench/results/spend.json`): round 2 is entries 17 to 25, **US$2.14** (US$2.1423), against a round cap of US$6 inside the US$20 ceiling. The nine entries are seven completed runs (direct arms of A, B, C; `borg_cloud` arms of A, B, C, D), the stopped D local run, and the job C judge (US$0.0058).

The ledger understates the round by about US$0.011. The stopped run's entry was recovered at US$0.00 after its database was archived; that database held one grok-4.20 planning call of US$0.0112. `spend.json` is left as written (ruling 31c).

The whole ledger total is US$5.4781, which also holds the US$0.07 prior spend, round 1, the demo recordings and the packing live check.

## Files

Result files are in this folder: `*_1.json` per run, `judge_C.json`, `reestimate_s1_1.json`, and `dbs/` for run databases and call logs. `ruling23_live_check.json` is the packing live check from before the freeze and is not a round-2 run. Round 1 is untouched in `bench/results/2026_11_17/`. The side-by-side page is `bench/results/round_1_vs_round_2.md`.
