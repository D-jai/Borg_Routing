# Bench rounds 1 and 2, side by side

Round 1: 17-Nov-2026, folder `bench/results/2026_11_17/`, product head `532fe0c`. Round 2: 18-Nov-2026, folder `bench/results/2026_11_18/`, freeze `f655890`, runs on `8a304f5`. Both rounds are published as they fell. BORG_ROUTE won no job in either round.

## What is the same and what is new

- Same rules (`bench/README.md`), same four jobs and prompts, same direct arm (`anthropic/claude-opus-5.5`), same win rule, same judge.
- **Round-2 data for jobs A, C and D is new**: regenerated from seed 4132800769 (the first 8 hex digits of the freeze hash read as a number), and every arm, direct included, ran on it.
- **Job B is not fresh:** same input as round 1; the fixes were built after seeing round 1's result on it.
- Round 1 stopped after stage 1 by a written deviation (ruling 19). Round 2 stopped after stage 1 because no arm could win (ruling 31). Neither round ran stage 2. Every cell is one run.
- The causes of the round-2 results are being investigated in `docs/design/bench_round2_measurements.md` (not yet written). This page claims no cause beyond the plain observations in the table.

## Per job and arm

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

## Product changes between the rounds

Everything below is in the product between round 1's head (`532fe0c`) and the round-2 freeze. Each line is one ruling with its commits. Rulings 14 to 19 and 24 made no product change that the bench saw (14 staged repeats: before round 1; 15 to 17 and 19: rulings about publishing and measuring; 24: demo recordings, with one product fix inside it listed under 25 and 26).

- **Ruling 18, baseline estimate off by default:** the receipt's old "same steps on the top model" figure is no longer shown (`5892ba6`, `d2f3e4f`, in the 4.2 workspace build).
- **Ruling 20, stage-1 crash fixes R1 to R4:** R1 reused ids are renamed and logged, R2 a wrong-shape or failed compose is recovered or salvaged (`answer_kind` "salvaged"), R3 non-object plan nodes are dropped, R4 a step's own `extra_fields` are honoured in skill rendering (`ac0960b`, `bce32aa`, `3b1b21b`, `b4fdcfd`, `6a93cc6`; final `eac69dc`, `35f2d2f`, `2952456`, `9ade7d2`, `d25aa12`, `c1ce2c8`).
- **Ruling 21 (with 21a, 21b), rows placed by code:** compose can name a finished list-holding step with a marker and the engine places the stored rows as one JSON array, in input order (`8fd4c23`; fixes `82a79aa`; final `d9d7e89`; design `1a25229`, `f469105`).
- **Ruling 22 (with 22a), the receipt's comparison is a floor:** `direct_floor_dollars`, labelled "at least", replaces the old baseline meaning (`8b6acc5`; fixes `3188bc0`; final `c9c2f33`).
- **Ruling 23 (with 23a to 23g), item packing:** small apply_to_each items are packed into one cloud call, up to 20 per call, cloud only, never to a local host (`40ee228`; items `5aefd2a`; fixes `c7b18c0`; final `d29c891`; design `ea575ab`, `773bae1`).
- **Rulings 25 and 26 (with 26e, 26f, 26g), state ops and "no step behind the answer":** one shared field lookup for the four state_ops, `key` and `group_by` accepted, BRT-SOP-006 names an op's params; an answer with no resolved step is marked `planner_only` (BRT-ORC-033), and documents with no resolved document step fail (BRT-ORC-034); a child run that ended `planner_only` is a failed step in its parent (`1071659` null `items_ref` no longer drops a node, found in the ruling 24 demo recording; `ac1c932`, `24d4be5`, `82d3da4`, `62e4ca0`).
- **Ruling 28 (with 28h), a local model's speed is measured and an unmeasured one is never free:** one timed generation on Discover, tick and run start, one probe per model at a time under a 180 s deadline, failures remembered (BRT-REG-016); an unmeasured local model can win a step only when no priced model qualifies (`633b5b7`, `22654e0`, `209f1c9`; lead's final `01a72c4`).
- **Deej's ruling 12, protected file `data/skills.yaml`:** `classify_into_buckets` gets `recommended_specialty: extraction` (`b99f0af`).
- **Deej's ruling 13, protected file `src/prompts/orchestrator.py`:** seven lines listing each state_op's params, with a drift test against the code (`0897977`, `27dfdb8`; pin `ca0e02a`).

Changes since round 1 that are not product code and are not listed above: CI and test fixes (`9adee24`, `5fbc74c`, `7657bad`, and the two row-order test fixes `8c8f878`, `8a304f5`), documentation, the demo recording script, the data regeneration (`2c4d12b`), and the freeze (`f655890`).

## Spend

Round 1: US$2.5595 as recorded in the ledger (the OpenRouter key's own counter showed US$2.4346). Round 2: US$2.14 as recorded (US$2.1423); it understates by about US$0.011 because the stopped D local run's entry was recovered at US$0.00 after its database was archived (the database held one planning call of US$0.0112). `bench/results/spend.json` is left as written.
