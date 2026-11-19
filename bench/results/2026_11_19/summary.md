# Bench round 3, 18 and 19-Nov-2026: summary

**Verdict by the rule: BORG_ROUTE won job B and no other job.** Job A: no win (quality worse in 3 of 3 comparisons). Job C: no win (the judge chose direct; one run, thinner evidence). Job D: reported, not judged (cloud side only, so the arm D's rule needs did not run). Job B's input is not fresh: it is the same input as in rounds 1 and 2 and in the development trial, and the fixes were built after seeing results on it.

The rules are the ones in `bench/README.md`, word for word, with the differences of planner ruling 40c stated below. Nothing in the rules was changed after seeing a number. This is the last round (Deej's ruling 15: one last round on fresh data, cloud side only, cap US$6, run once, published win or lose beside rounds 1 and 2).

## Differences from the README's rules (ruling 40c)

- **Cloud side only.** The arms are `direct` and `borg_cloud`. No `borg_local_workers` arm ran, for any job. Job D's win rule compares the local arm with `borg_cloud`, so **job D is reported, not judged**.
- **Job D's `direct` arm is the README's "on request" arm** (`--arms direct`). It is outside D's win rule. Its numbers are shown beside D's cloud arm as facts only.
- **Job B's input is not fresh.** It is the same input as in rounds 1 and 2 and in the development trial (trial 32g), and the fixes were built after seeing results on it. This is said again beside B's result.
- **The local model stayed in the pool as shipped.** `ollama/llama3.2:3b` was checked in memory as in round 2. The start-of-stage speed probe ran and measured **5.337 tok/s** (stage 1 start). No step chose the local model and no local arm ran, so the figure was not used.
- **The host was prepared as for round 2's relaunch:** the `borg_ollama` container up (10 h) with no warm-up request. Pre-checks: Docker 29.9.3 serving, the `borg_bench_py` image present, nothing heavy running.
- **Job C has one run (thinner evidence).** By planner ruling 41, stage 2 added repeats 2 and 3 to jobs A, D and B. Job C's two extra repeats did not fit the US$6 cap, and the written rule adds repeats by whole job only, so C stays at one run.

## Freeze, seed and invariant

- Freeze commit: `7db5d688e60cc7f00bacf44d029a9f3c410756c6` (BUILD.md and CHECKPOINT.md only), on `d8cf3b3`, which CI run 44458986061 passed in full.
- Seed: the first 8 hex digits of the freeze hash read as a number, int('7db5d688', 16) = **2109068936**. Data commit and head: `867ad31` (jobs A, C and D regenerated).
- Every round-3 run used head `867ad31` (every run database's log rows carry `source_version` `git:867ad31`).
- Invariant: `git diff d8cf3b3 HEAD --stat -- src data ui` prints nothing. `git diff d8cf3b3 HEAD --stat -- bench tests` shows only `bench/make_data.py` (the SEED line), 278 files under `bench/data/`, and `tests/orchestrator/test_compose_placement_replay.py` (the one pinned test). Checked again for this report on 19-Nov-2026.
- Ruling 40f: `git diff 8a304f5 HEAD --stat -- src/prompts data/skills.yaml data/capability_matrix.yaml` prints nothing. The protected prompts, skills and matrix are the same as in round 2. **The compare-items planner line (ruling 33c) is not in, so job C ran without it.**

## The runs

- **Stage 1**, 18-Nov-2026, 23:46 to 23:52 Toronto time: one repeat of A, B, C and D, both arms, then the job C judge.
- **Stage 2** (ruling 41), 23:54 on 18-Nov-2026 to 00:04 on 19-Nov-2026 Toronto time: repeats 2 and 3 of A, D and B, both arms. The results folder is named by the UTC date, `2026_11_19`.
- No transport reruns (no `aborted_attempts` in any result file). The watchdog never fired.
- The tripwire re-estimated five times (`reestimate_s1_1.json`, `reestimate_s2_1.json` to `_4.json`): C direct 1, A direct 2, D direct 2, B direct 2 and B direct 3 each cost a few percent over their own estimate (worst ratio 1.099). Every re-estimate said "continue".
- Every run completed. No run failed.

## Results, per run and median

Direct arm: one call to `anthropic/claude-opus-5.5` (US$4 / US$20 per million tokens), the same as rounds 1 and 2. "Share of direct" is BORG's cost divided by the direct arm's cost: per run against the same repeat, and median against median. It is a fact about the bill, not a saving, and is shown for every row.

### Job A, 200 support emails

| Run | Direct (US$) | BORG (US$) | Share of direct | Direct accuracy | BORG accuracy | BORG category | BORG urgency exact | BORG urgency within one point |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.2077 | 0.1436 | 69.1% | 1.00 | 0.650 | 0.990 | 0.655 | 0.995 |
| 2 | 0.2103 | 0.1044 | 49.6% | 1.00 | 0.690 | 0.990 | 0.695 | 0.995 |
| 3 | 0.2085 | 0.1242 | 59.6% | 1.00 | 0.780 | 0.985 | 0.795 | 0.995 |
| **Median** | **0.2085** | **0.1242** | **59.6%** | **1.00** | **0.690** | **0.990** | **0.695** | **0.995** |

- Direct: category 1.00 and urgency 1.00 (exact, so also within one point) on all three runs; 200 of 200 answered each time.
- BORG: 200 of 200 answered on every run. Emails one point off on urgency: 68, 60 and 40; each run had exactly 1 email two points off. **"Within one point" is not part of the win rule**, which grades exact matches only; it is shown to describe the misses.
- How "within one point" was computed: the grader's own parser (`parse_json_list` and the `file`-name matching of `score_classification` in `bench/run_bench.py`) read each answer, and each urgency was compared with `bench/data/answers/job_a_answers.json` as an integer. The exact figures this gives match the grader's per-field accuracy on every run.

| Run | Answer kind | Compose posts | Checks fired | Plan notes | Packing | Worker model |
|---|---|---|---|---|---|---|
| 1 | composed | 2 | BRT-ORC-035 (plan note, the step ran as written); BRT-ORC-039 (compose typed 10 rows and `// ... (full list of all 200 entries follows in the stored results)`; one re-ask, which placed the rows with a marker) | `accepted_unused` {concurrency_limit, output_schema}; `ran_without_fields` {file, category} | 10 pack posts of 20; 10 `pack_outcome`; 8 packs accepted whole, 2 rejected whole (40 items retried alone) | gemini-3.1-flash-lite-preview (light) |
| 2 | composed | 1 | BRT-ORC-035 (plan note) | the same two notes | 10 pack posts of 20; 10 `pack_outcome`; 9 accepted whole, 1 rejected whole (20 items retried alone) | gemini-3.1-flash-lite-preview (light) |
| 3 | composed | 2 | BRT-ORC-035 (plan note); BRT-ORC-039 (one re-ask, then rows placed) | the same two notes | 10 pack posts of 20; 10 `pack_outcome`; all 200 accepted from packs | gemini-3.1-flash-lite-preview (light) |

- Planner, re_plan and compose: `x-ai/grok-4.20` in every run. Every plan used `extra_fields {"urgency": "integer 1-5"}` on `classify_into_buckets`, and the final compose placed all 200 rows by marker (`@@ROWS:classify_emails | file=item, category=bucket, urgency=urgency@@`).
- No warning fired (no BRT-ORC-002, no BRT-ORC-037). No `rows_pairs_reversed` or `rows_placed_plain` note. All three answers are the normal composed kind.
- Wall time: direct 46.3, 47.6, 47.0 s; BORG 19.3, 15.0, 15.8 s.
- The whole-pack retries cost US$0.0198 (run 1) and US$0.0099 (run 2), inside the run costs above: the stated limit of ruling 36d.

**Verdict, job A: no win.** Cost holds (median 59.6% of direct's, under the 70% line). Quality does not hold: in each of the 3 repeat pairs direct's accuracy (1.00) is more than 2 points above BORG's (0.65, 0.69, 0.78), so direct is preferred in 3 of 3 comparisons.

### Job B, small Python service

**Same input as rounds 1 and 2 and the development trial; the fixes were built after seeing results on it.**

| Run | Direct (US$) | BORG (US$) | Share of direct | Direct checks passed / failed | BORG checks passed / failed | Pair |
|---|---|---|---|---|---|---|
| 1 | 0.2857 | 0.1728 | 60.5% | 18 / 0 | 16 / 2 | direct preferred |
| 2 | 0.3187 | 0.1286 | 40.3% | 18 / 0 | 18 / 0 | tie |
| 3 | 0.3247 | 0.1800 | 55.4% | 18 / 0 | 18 / 0 | tie |
| **Median** | **0.3187** | **0.1728** | **54.2%** | **18** | **18** | |

- Docker served for every job B run; every container check ran; no run is void.
- BORG run 1 failed two checks: `spec_openapi_paths` (the OpenAPI file lacks `POST /notes`) and `tests_pass_in_container` (its own test `test_create_note_with_tags` failed with `sqlite3.OperationalError`). It passed all 11 reference tests.
- Every direct run took two attempts: the first was cut at 7,000 output tokens (`finish_reason` "length"), the retry at 14,000 finished. Both attempts are in its cost.

| Run | Answer kind | Compose posts | Checks fired | Plan notes | Packing | Worker models |
|---|---|---|---|---|---|---|
| 1 | composed | 1 | none | none | none (no apply_to_each) | gpt-5.3-codex (heavy and mid steps), gpt-5.4-nano (two light steps) |
| 2 | composed | 1 | none | none | none | gpt-5.3-codex (one heavy step) |
| 3 | composed | 1 | none | none | none | gpt-5.3-codex (four steps: light, heavy, mid, light) |

- Planner, re_plan and compose: `x-ai/grok-4.20`. No warning fired. All three answers are the normal composed kind.
- Wall time: direct 105.5, 120.3, 124.0 s; BORG 74.5, 70.1, 63.7 s.

**Verdict, job B: win by the rule.** Cost holds (median 54.2% of direct's). Quality holds: direct is preferred in 1 of 3 comparisons, with 2 ties, which is not a majority. All three BORG answers are normal composed answers, so ruling 40e does not bar the win. `bench/aggregate.py` gives `wins: true`. Beside it, plainly: job B's input is the same as in rounds 1 and 2 and in the trial, and the fixes were built after seeing results on it, so this is not a result on fresh data. By the rule both attempts count. Without the cut attempt, BORG's cost is about the same as direct's: direct's median with only the finishing attempt is US$0.175432 (runs: US$0.142452, US$0.175432, US$0.181432; the cut first attempt was US$0.143232 on every run), and BORG's median US$0.172783 is 98.5% of that.

### Job C, 15 documents, about 150,000 tokens (one run, thinner evidence)

| Run | Direct (US$) | BORG (US$) | Share of direct | Judge verdict |
|---|---|---|---|---|
| 1 (one run) | 0.7980 | 0.2518 | 31.6% | direct preferred, in both orders (judge `openrouter/qwen/qwen4-max`; sanity case passed) |

- Answer lengths (recorded by `judge_C.json`; the judge never sees them): direct 566 characters, 85 words, 11 list items; BORG 62,254 characters, 7,360 words, 12 list items, 2 code blocks, 1 document part reference. BORG's answer holds its own text plus 168 rows placed by code (84 from each of its two extraction steps).
- The informational fact score is recorded in the result files. It is informational, never quoted, and plays no part in the verdict.
- Job C ran without the compare-items planner line of ruling 33c.

| Run | Answer kind | Compose posts | Checks fired | Plan notes | Packing | Worker model |
|---|---|---|---|---|---|---|
| 1 | composed | 2 | BRT-ORC-029 (compose's first reply was the number `1.14`, not an object; one re-ask, then rows placed by two markers) | none | 2 pack posts of 3; 2 `pack_outcome`, all 6 accepted (3 of them came back with a non-resolved status) | gpt-5.4-nano (two light extraction steps) |

- The rejected compose call is priced and logged (US$0.0361), so the cost above includes it, unlike the trial's job C figures (ruling 35e). The run database's logged dollars sum to the result's `cost_dollars`.
- Planner, re_plan and compose: `x-ai/grok-4.20`. No warning fired.
- Wall time: direct 9.3 s; BORG 22.3 s. Judge cost US$0.0520 (sanity case included).

**Verdict, job C: no win, one run (thinner evidence).** Cost holds (31.6% of direct's). Quality does not hold: the judge chose direct in the only comparison (1 of 1).

### Job D, 60 client notes (reported, not judged)

The `direct` arm here is the README's on-request arm, outside D's win rule. D's rule compares `borg_local_workers` with `borg_cloud`, and the local arm did not run.

| Run | Direct (US$) | BORG (US$) | Share of direct | Direct risk exact | BORG risk exact | BORG risk within one label | Canaries sent to cloud / in answer (BORG) |
|---|---|---|---|---|---|---|---|
| 1 | 0.0597 | 0.0501 | 84.0% | 1.00 | 0.90 | 1.00 | 3 / 0 |
| 2 | 0.0621 | 0.0535 | 86.2% | 1.00 | 0.90 | 1.00 | 3 / 0 |
| 3 | 0.0609 | 0.0535 | 87.9% | 1.00 | 0.90 | 1.00 | 3 / 0 |
| **Median** | **0.0609** | **0.0535** | **87.9%** | **1.00** | **0.90** | **1.00** | |

- Risk is the only field, so accuracy equals risk accuracy. 60 of 60 answered in every run, both arms. The labels are ordinal (low, medium, high, in the key's order), so "within one label" is shown; each BORG run had 6 notes one label off and none two off. As for job A, "within one" is not part of any rule.
- The direct arm sends every note, canaries included, to the cloud model in one call by design. BORG's `borg_cloud` arm is `by_reference`, which makes no privacy promise.

| Run | Answer kind | Compose posts | Checks fired | Plan notes | Packing | Worker model |
|---|---|---|---|---|---|---|
| 1 | composed | 1 | BRT-ORC-035 (plan note, the step ran) | `accepted_unused` {concurrency_limit, output_schema}; `ran_without_fields` {file, risk} | 3 pack posts of 20; 3 `pack_outcome`, all 60 accepted | gemini-3.1-flash-lite-preview (light) |
| 2 | composed | 1 | none | `accepted_unused` {concurrency_limit} | the same | the same |
| 3 | composed | 1 | none | `accepted_unused` {concurrency_limit} | the same | the same |

- Planner, re_plan and compose: `x-ai/grok-4.20`. No warning fired. Compose typed the 60 rows itself in all three runs (no rows marker).
- Wall time: direct 12.1, 12.3, 12.3 s; BORG 15.8, 15.7, 16.0 s.

**Verdict, job D: none (reported, not judged).** The arm D's rule needs did not run. As facts only: BORG's median cost was 87.9% of the on-request direct arm's, and its accuracy 0.90 against direct's 1.00.


## Median wall time per job and arm (planner ruling 42a)

| Job | direct (s) | borg_cloud (s) | Runs |
|---|---|---|---|
| A | 47.0 | 15.8 | 3 |
| B | 120.3 | 70.1 | 3 (direct includes the cut first attempt) |
| C | 9.3 | 22.3 | 1 |
| D | 12.3 | 15.8 | 3 (direct is the on-request arm) |

## The verdicts by the rule

`bench/aggregate.py` has no command line. Its `aggregate_job` was called on this folder, unchanged, from a scratch script. It printed:

| Job | Pair | Status | Basis | Cost ratio | Cost holds | Comparisons (direct / BORG / tie) | Quality holds | Wins |
|---|---|---|---|---|---|---|---|---|
| A | direct vs borg_cloud | complete | median of 3 | 0.596 | true | 3 / 0 / 0 | false | **false** |
| A | direct vs borg_local_workers | incomplete (repeats 1 to 3 unfinished) | | | | | | none |
| B | direct vs borg_cloud | complete | median of 3 | 0.542 | true | 1 / 0 / 2 | true | **true** |
| C | direct vs borg_cloud | complete | one run (thinner evidence) | 0.316 | true | 1 / 0 / 0 | false | **false** |
| D | borg_cloud vs borg_local_workers | incomplete (repeats 1 to 3 unfinished) | | | | | | none |

The two "incomplete" rows are the local arms, which this round did not run by design (ruling 40c). No local result file was written, so aggregate.py reports them "incomplete", not "not run". They are not failures.

Ruling 40e: only a normal composed answer can win. Every BORG answer in this round is `composed` in its run database, and none carries BRT-ORC-002 (kept work) or a BRT-ORC-037 warning with `rows_placed_plain` (all columns shown). No answer was salvaged or `planner_only`. So the rule's verdicts stand as printed:

| Job | Verdict |
|---|---|
| A | no win: cost 59.6% of direct's, quality worse in 3 of 3 comparisons |
| B | **win by the rule**: cost 54.2% of direct's, quality held (direct preferred 1 of 3, 2 ties). Same input as rounds 1 and 2 and the trial; the fixes were built after seeing results on it By the rule both attempts count. Without the cut attempt, BORG's cost is about the same as direct's (98.5% of US$0.175432). |
| C | no win, one run (thinner evidence): cost 31.6% of direct's, the judge chose direct |
| D | reported, not judged: no local arm; the direct arm is outside D's rule |

## Product changes since round 2 (rulings 32 to 39)

Everything below is in the product between round 2's head (`8a304f5`) and the round-3 freeze. One line per ruling; detail in BUILD.md sections 8 and 9.

- **Ruling 32, the fixes from the round-2 causes note:** a skill step whose only side effect is deferrals packs (32a, `aaf2afc`); a field hinted "(only if" may be absent or null (32b, `fdfb5f4`); a skill step whose schema names fields the skill does not return became plan error BRT-ORC-035 (32c, `dc97aa8`; reversed by 35a); `failed_items_only` and BRT-ORC-036 for unused node fields (32d, `3b25982`, `eb750bd`); compose places rows by marker with chosen fields and names, `@@ROWS:<step> | name=field@@`, with post-compose checks (32e, `006b6c6`, `e08710f`).
- **Ruling 33, the trial and round 3:** set the development trial (32g) and the conditions for a round 3. No product change. The compare-items prompt line (33c) was not added.
- **Ruling 34, the trial's reading:** "no win yet, development continues inside the cap". No product change.
- **Ruling 35, ruling 32c reversed:** BRT-ORC-035 and BRT-ORC-036 no longer refuse a plan; the step runs and a plan note is written (`ran_without_fields`, `accepted_unused`) (35a, 35b); the skill catalogue teaches `extra_fields` (35c, `6ee4f31`); the compose block's wording for asked-for fields (35d); every model call is priced and logged (35e, `a4b32cd`); a pack reply missing an extra field is retried alone, then kept.
- **Ruling 36, the elided-list check:** BRT-ORC-039 fires when compose types rows with an explicit elision instead of placing them; one re-ask, then salvage. Pushed with the 32 to 36 fix cycle (`c3935d0..e97bd70`).
- **Ruling 37, lane redaction in the live stream:** the chat stream redacts a stage's text under the lane exactly as the run view does (`fc39926`, with 4.5 replay mode).
- **Ruling 38, the remaining phase 4 and 5 work orders:** interface check as a CI job (4.6), one-command install (5.2), LICENSE and README (5.1), tree clean-up and docs in line with the code; one merge and one push (`ebbdab2`, fix `093eb0e`).
- **Ruling 39, compose markers written the other way round:** a reversed `field=name` pair is read reversed with a plan note `rows_pairs_reversed` (39a); the BRT-ORC-037 repair sentence and block line name the order (39b); a bad spec on a good target places plain rows with a BRT-ORC-037 warning, an answer that cannot win a round (39c); a filter attempt in a marker is refused (`f0aa4db`, `576cf55`, `409363f`, `36d20a2`; re-pin `d8cf3b3`).

## Spend

- **Round 3: US$3.8507** of the US$6 cap (ledger entries 43 to 63). Stage 1: US$2.0215 (entries 43 to 51, judge included). Stage 2: US$1.8292 (entries 52 to 63).
- **Ledger reconciliation:** before the round the entries summed to US$7.0951; the ledger's `prior_dollars` is US$0.07; together US$7.1651, which is what the runner printed at the start. **Ledger total after the round: US$11.0157** (entries US$10.9457 plus the prior US$0.07), inside the US$20 ceiling.
- Each BORG run's logged dollars in its run database equal its result file's `cost_dollars`.

## What the numbers do not show

- **Fresh data for job B.** B's input is the same as in rounds 1 and 2 and in the trial; the fixes were built after seeing results on it.
- **The local lane.** No local arm ran. Round 3 adds nothing to, and takes nothing from, the privacy claim for the on-premise lane.
- **Job C's spread.** One run, no median.
- **Causes.** This report records what happened and claims no cause beyond the plain observations above.

## Files

In this folder: `<job>_<arm>_<n>.json` per run, `judge_C.json`, `reestimate_s1_1.json` and `reestimate_s2_1.json` to `_4.json`, and `dbs/` for run databases and call logs (git-ignored). The side-by-side page for all three rounds is `bench/results/round_1_vs_round_2.md`. The development trial, which is not a round, is `bench/results/trial32g_report.md`.
