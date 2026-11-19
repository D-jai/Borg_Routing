# Bench rounds 1, 2 and 3, side by side

Round 1: 17-Nov-2026, folder `bench/results/2026_11_17/`, product head `532fe0c`. Round 2: 18-Nov-2026, folder `bench/results/2026_11_18/`, freeze `f655890`, runs on `8a304f5`. Round 3: 18 and 19-Nov-2026, folder `bench/results/2026_11_19/`, freeze `7db5d68`, runs on `867ad31`. All three rounds are published as they fell. BORG_ROUTE won no job in rounds 1 and 2. In round 3 it won job B by the rule, on an input that is not fresh (see job B below), and no other job.

The development trial of 18-Nov-2026, [`trial32g_report.md`](trial32g_report.md), is **not a round**: it ran on old data, only the BORG arm, and no figure from it is cited anywhere. It is linked here only so a reader can find it.

The file keeps its old name so that existing links still work.

## What is the same and what is new

- Same rules (`bench/README.md`), same four jobs and prompts, same direct arm (`anthropic/claude-opus-5.5`), same win rule, same judge.
- **Round-2 data for jobs A, C and D is new**: regenerated from seed 4132800769 (the first 8 hex digits of the freeze hash read as a number), and every arm, direct included, ran on it.
- **Round-3 data for jobs A, C and D is new again**: regenerated from seed 2109068936, int('7db5d688', 16), and both arms ran on it.
- **Job B is not fresh in any round after round 1:** rounds 2 and 3 (and the trial) used round 1's input; the fixes were built after seeing results on it.
- Round 1 stopped after stage 1 by a written deviation (ruling 19). Round 2 stopped after stage 1 because no arm could win (ruling 31). Neither round ran stage 2; every cell of rounds 1 and 2 is one run.
- **Round 3 ran both stages.** Stage 2 (ruling 41) added repeats 2 and 3 to jobs A, D and B, so their figures are medians of three runs. Job C stayed at one run: its two extra repeats did not fit the US$6 cap, and the rule adds repeats by whole job only.
- **Round 3 is cloud side only** (ruling 40c): arms `direct` and `borg_cloud`, no `borg_local_workers`. Job D is therefore reported, not judged, and its `direct` arm is the README's on-request arm, outside D's win rule. The local model stayed in the pool; the start-of-stage probe measured it at 5.337 tok/s, and it was not used.
- The causes of the round-2 results are in `docs/design/bench_round2_measurements.md`. This page claims no cause beyond the plain observations in the tables.

## Per job and arm, rounds 1 and 2

"% of direct" is BORG's cost divided by the direct arm's cost for the same job. It is a fact about the bill, not a saving, and it is shown for rows where the answer failed or lost.

| Job | Arm | Round 1 cost (US$) | R1 % of direct | R1 quality | R1 verdict | Round 2 cost (US$) | R2 % of direct | R2 quality | R2 verdict |
|---|---|---|---|---|---|---|---|---|---|
| A | direct | 0.2085 | n/a | 200 of 200 | n/a | 0.2294 | n/a | 200 of 200 | n/a |
| A | borg_cloud | 0.3623 | 174% | no answer (failed at compose, BRT-ORC-009) | failed, no win | 0.2022 | 88% | 0 of 200 graded (malformed composed answer) | no win |
| A | borg_local_workers | 0.0447 | 21% | no answer (failed at compose, BRT-ORC-009) | failed, no win | not run | not run | not run | not run (neither pass nor fail) |
| B | direct | 0.3281 | n/a | 18 of 18 checks | n/a | 0.3189 | n/a | 6 of 6 checks that need no container passed; 12 skipped | n/a |
| B | borg_cloud | 0.3020 | 92% | 18 of 18 checks | no win (over 70%) | 0.2198 | 69% | holds none of the four files; failed 6 of 6 checks that need no container; 12 skipped | void (Docker not serving 11:19 to 11:26 Toronto); not fresh |
| C | direct | 0.7977 | n/a | n/a | n/a | 0.8001 | n/a | judge chose it | n/a |
| C | borg_cloud | 0.2119 | 27% | no answer ("'str' object has no attribute 'get'") | failed, direct wins by default | 0.2477 | 31% | judge chose direct; informational fact score 6 of 8 facts, 0 of 2 contradictions | no win |
| D | borg_cloud | 0.1755 | no direct arm | no answer (failed at compose, BRT-ORC-009); 120 canaries sent to cloud models | failed | 0.1183 | no direct arm | accuracy 60% on 60; 60 canaries sent to cloud models, 0 in the answer | no verdict (local arm not run) |
| D | borg_local_workers | 0.0589 | no direct arm | no answer (failed at compose, BRT-ORC-009); 0 canaries sent to cloud models | failed | not run | not run | not run (a stopped attempt: 12 of 32 local item calls, no result) | not run (neither pass nor fail) |

Note on job C in round 1: BORG gave no answer, so the judge made no call and direct won by default. In round 2 the judge was called and chose direct.

## Per job and arm, round 3

Cost is the median of three runs for A, B and D, and the one run for C. "% of direct" is median against median. Full per-run figures: `bench/results/2026_11_19/summary.md`.

| Job | Arm | Round 3 cost (US$) | R3 % of direct | R3 quality | R3 answer kind | R3 verdict |
|---|---|---|---|---|---|---|
| A | direct | 0.2085 (median of 3) | n/a | accuracy 1.00 on all three runs (200 of 200) | n/a | n/a |
| A | borg_cloud | 0.1242 (median of 3) | 59.6% | accuracy 0.65, 0.69, 0.78 (median 0.69); category median 0.99; urgency exact median 0.695, within one point 0.995 (within one is not part of the rule) | composed, all three | no win: quality worse in 3 of 3 comparisons |
| A | borg_local_workers | not run (cloud side only) | not run | not run | not run | not run (neither pass nor fail) |
| B | direct | 0.3187 (median of 3) | n/a | 18 of 18 checks on all three runs (each run's first attempt was cut at 7,000 output tokens; the retry finished) | n/a | n/a |
| B | borg_cloud | 0.1728 (median of 3) | 54.2% | 16 of 18, 18 of 18, 18 of 18 checks (run 1 failed the OpenAPI path check and its own tests) | composed, all three | **win by the rule** (direct preferred 1 of 3, 2 ties); **not fresh**: same input as rounds 1 and 2 and the trial, and the fixes were built after seeing results on it |
| C | direct | 0.7980 (one run) | n/a | judge chose it | n/a | n/a |
| C | borg_cloud | 0.2518 (one run) | 31.6% | judge chose direct (1 of 1, both orders); ran without the compare-items prompt line of ruling 33c | composed | no win, one run (thinner evidence) |
| D | direct (on request, outside D's rule) | 0.0609 (median of 3) | n/a | accuracy 1.00 on all three runs | n/a | n/a |
| D | borg_cloud | 0.0535 (median of 3) | 87.9% of the on-request direct arm | accuracy 0.90 on all three runs (within one label 1.00); 3 canaries sent to cloud models per run, 0 in the answer | composed, all three | reported, not judged (D's rule needs the local arm) |
| D | borg_local_workers | not run (cloud side only) | not run | not run | not run | not run (neither pass nor fail) |

Job B, round 3: By the rule both attempts count. Without the cut attempt, BORG's cost is about the same as direct's: direct's median with only the finishing attempt is US$0.175432 (runs: US$0.142452, US$0.175432, US$0.181432; the cut first attempt was US$0.143232 on every run), and BORG's median US$0.172783 is 98.5% of that.

No round-3 BORG answer was salvaged, `planner_only`, kept work (BRT-ORC-002) or all columns shown (BRT-ORC-037 warning), so ruling 40e bars none of them.

## Verdicts, three rounds

| Job | Round 1 | Round 2 | Round 3 |
|---|---|---|---|
| A | failed, no win | no win | no win (cost held at 59.6%; quality worse) |
| B | no win (92%, over 70%) | void (Docker not serving) | **win by the rule** (54.2%, quality held), on an input that is not fresh. By the rule both attempts count. Without the cut attempt, BORG's cost is about the same as direct's (98.5% of direct's US$0.175432) |
| C | failed, direct wins by default | no win (judge chose direct) | no win (31.6%; judge chose direct; one run) |
| D | failed | no verdict (local arm not run) | reported, not judged (cloud side only) |

## Product changes between rounds 2 and 3

Everything below is in the product between round 2's head (`8a304f5`) and the round-3 freeze (`7db5d68`). One line per ruling; the commits are in BUILD.md section 9. The protected prompts, `data/skills.yaml` and `data/capability_matrix.yaml` did not change between the two rounds (`git diff 8a304f5 HEAD --stat` on them prints nothing).

- **Ruling 32:** deferral-only skill steps pack (32a); a field hinted "(only if" may be absent (32b); BRT-ORC-035 for a schema naming fields the skill does not return (32c, reversed by 35a); `failed_items_only` and BRT-ORC-036 (32d); compose places rows by marker with chosen fields and names, with post-compose checks (32e).
- **Ruling 33:** the development trial and round-3 conditions; no product change; the compare-items prompt line (33c) not added.
- **Ruling 34:** the trial read as "no win yet"; no product change.
- **Ruling 35:** BRT-ORC-035 and 036 no longer refuse a plan; the step runs with a plan note; the skill catalogue teaches `extra_fields`; every model call is priced and logged (35e).
- **Ruling 36:** BRT-ORC-039, the elided-list check on compose (one re-ask, then salvage).
- **Ruling 37:** the live chat stream redacts lane text exactly as the run view does (with 4.5 replay mode).
- **Ruling 38:** the remaining phase 4 and 5 work orders (interface check in CI, one-command install, LICENSE, README, docs), one merge and one push.
- **Ruling 39:** compose markers written the other way round are read reversed with a plan note; the BRT-ORC-037 repair wording; a bad spec on a good target places plain rows with a 037 warning (cannot win a round); filter attempts refused.

## Product changes between rounds 1 and 2

Everything below is in the product between round 1's head (`532fe0c`) and the round-2 freeze. Each line is one ruling with its commits. Rulings 14 to 19 and 24 made no product change that the bench saw (14 staged repeats: before round 1; 15 to 17 and 19: rulings about publishing and measuring; 24: demo recordings, with one product fix inside it listed under 25 and 26).

- **Ruling 18, baseline estimate off by default:** the receipt's old "same steps on the top model" figure is no longer shown (`5892ba6`, `d2f3e4f`, in the 4.2 workspace build).
- **Ruling 20, stage-1 crash fixes R1 to R4:** R1 reused ids are renamed and logged, R2 a wrong-shape or failed compose is recovered or salvaged (`answer_kind` "salvaged"), R3 non-object plan nodes are dropped, R4 a step's own `extra_fields` are honoured in skill rendering (`ac0960b`, `bce32aa`, `3b1b21b`, `b4fdcfd`, `6a93cc6`; final `eac69dc`, `35f2d2f`, `2952456`, `9ade7d2`, `d25aa12`, `c1ce2c8`).
- **Ruling 21 (with 21a, 21b), rows placed by code:** compose can name a finished list-holding step with a marker and the engine places the stored rows as one JSON array, in input order (`8fd4c23`; fixes `82a79aa`; final `d9d7e89`; design `1a25229`, `f469105`).
- **Ruling 22 (with 22a), the receipt's comparison is a floor:** `direct_floor_dollars`, labelled "at least", replaces the old baseline meaning (`8b6acc5`; fixes `3188bc0`; final `c9c2f33`).
- **Ruling 23 (with 23a to 23g), item packing:** small apply_to_each items are packed into one cloud call, up to 20 per call, cloud only, never to a local host (`40ee228`; items `5aefd2a`; fixes `c7b18c0`; final `d29c891`; design `ea575ab`, `773bae1`).
- **Rulings 25 and 26 (with 26e, 26f, 26g), state ops and "no step behind the answer":** one shared field lookup for the four state_ops, `key` and `group_by` accepted, BRT-SOP-006 names an op's params; an answer with no resolved step is marked `planner_only` (BRT-ORC-033), and documents with no resolved document step fail (BRT-ORC-034); a child run that ended `planner_only` is a failed step in its parent (`1071659` null `items_ref` no longer drops a node, found in the ruling 24 demo recording; `ac1c932`, `24d4be5`, `82d3da4`, `62e4ca0`).
- **Ruling 28 (with 28h), a local model's speed is measured and an unmeasured one is never free:** one timed generation on Discover, tick and run start, one probe per model at a time under a 180 s deadline, failures remembered (BRT-REG-016); an unmeasured local model can win a step only when no priced model qualifies (`633b5b7`, `22654e0`, `209f1c9`; lead's final `01a72c4`).
- **The owner's ruling 12, protected file `data/skills.yaml`:** `classify_into_buckets` gets `recommended_specialty: extraction` (`b99f0af`).
- **The owner's ruling 13, protected file `src/prompts/orchestrator.py`:** seven lines listing each state_op's params, with a drift test against the code (`0897977`, `27dfdb8`; pin `ca0e02a`).

Changes since round 1 that are not product code and are not listed above: CI and test fixes (`9adee24`, `5fbc74c`, `7657bad`, and the two row-order test fixes `8c8f878`, `8a304f5`), documentation, the demo recording script, the data regeneration (`2c4d12b`), and the freeze (`f655890`).

## Spend

Round 1: US$2.5595 as recorded in the ledger (the OpenRouter key's own counter showed US$2.4346). Round 2: US$2.14 as recorded (US$2.1423); it understates by about US$0.011 because the stopped D local run's entry was recovered at US$0.00 after its database was archived (the database held one planning call of US$0.0112). `bench/results/spend.json` is left as written.

Round 3: US$3.8507 as recorded (ledger entries 43 to 63) of its US$6 cap; stage 1 US$2.0215. Before round 3 the ledger's entries summed to US$7.0951, plus its `prior_dollars` of US$0.07, giving US$7.1651, which is what the runner printed. The ledger total after round 3 is US$11.0157.

The development trial (not a round) spent US$1.358 of its own US$5 cap; see [`trial32g_report.md`](trial32g_report.md).
