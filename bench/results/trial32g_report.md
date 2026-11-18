# Trial 32g, 18-Nov-2026: development trial on old data; not a round; no figure here is cited anywhere

This page records an end-to-end live trial of the cloud BORG arm after the ruling-32 fixes (planner rulings 32g and 33a, Deej's ruling 14: cap US$5). It is not a bench round. The data is round 1's and round 2's data, now development data. No README line, deck number, claim or post cites any figure on this page.

## What ran

- Head `d88931f` (ruling 32 fixes 32a to 32e built and reviewed; not pushed). The planner-prompt line of ruling 33c was not in: Deej had not answered.
- Arm: `borg_cloud` only. No direct re-runs: each job is compared with the stored direct result for the same data. The files `*_direct_1.json` in the trial folders are copies of `bench/results/2026_11_18/` (round-2 data) and `bench/results/2026_11_17/` (round-1 data), put there so the job C judge has its pair.
- No local-model runs. Job D therefore has no comparison: by rule it compares its local arm with its cloud arm.
- Part 1, round-2 data (`bench/data` as committed): jobs A, B, C, D, folder `trial32g_r2data`, 16:22 to 16:27 Toronto time. Docker served, so job B's container checks ran.
- Part 2, round-1 data (seed 20261117, regenerated into a scratch folder outside the repo, read with `--data-root`): jobs A and C, folder `trial32g_r1data`, 16:27 to 16:28.
- Speed probe at each start: llama3.2:3b 5.389 tok/s (part 1), 7.107 tok/s (part 2).
- Spend: **US$1.1292** of the US$5 cap (ledger entries 26 to 33). No watchdog stop. Every run is listed below; none was dropped or re-run.

## Results, one run per row

The trial win rule is the round's rule on one run: cost at most 70% of the stored direct cost for that data, quality not worse by the job's own grader or judge, and a normal composed answer. **No job won.**

| Data | Job | Cost (US$) | Direct (US$) | Share of direct | Quality | Answer kind | Compose posts | Checks and codes | Plan notes | Packing | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| round 2 | A | 0.3316 | 0.2294 | 145% | 200 of 200 answered; accuracy over all items 0.0 (category 0.935, urgency 0); over items with a result: category 0.935, no urgency produced | composed | 2 | BRT-ORC-035 (first plan), 041 once (re-ask), then clean | accepted_unused: classify_emails {concurrency_limit, output_schema}, classify_emails_2 {output_schema}; 035 ran_without_fields on classify_emails_2 {file, category, urgency} | 10 packs of 20 (one accepted 19) | no win: cost over the line, urgency not produced |
| round 2 | B | 0.1288 | 0.3189 | 40% | 17 of 18 checks (container checks ran); failed ref_test_bad_tag_is_422 (an uppercase tag accepted). The same input's direct answer passed 18 of 18 (round 1; round 2's direct was container-void) | composed | 1 | none | none | none (one coding step) | no win: one check worse (one model's slip on one run; no product fault) |
| round 2 | C | 0.2491 | 0.8001 | 31% | the judge chose direct (1-0); informational facts 6 of 8, contradictions 0 of 2 | composed | 2 | BRT-ORC-029 once | none | 2 packs of 2 | no win: judge chose direct |
| round 2 | D | 0.0605 | none | n/a | 60 of 60 answered; accuracy over all items 1.00; over items with a result 1.00 (round 2: 0.60). Canaries: 3 sent to cloud, 0 in the answer | composed | 1 | BRT-ORC-036 (an `instruction` inside skill_args) | accepted_unused: classify_notes {concurrency_limit, output_schema}; 036 ran_without_fields on classify_notes_2 {instruction} | 3 packs of 20 | cannot win cloud-only (no direct arm by rule) |
| round 1 | A | 0.1027 | 0.2085 | 49% | 200 of 200 answered; accuracy over all items 0.0 (category 0.935, urgency 0) | composed | 1 | BRT-ORC-035 (first plan) | accepted_unused: {output_schema} on both nodes; 035 ran_without_fields on classify_emails_2 | 10 packs of 20 (one accepted 19) | no win: urgency not produced |
| round 1 | C | 0.2417 | 0.7977 | 30% | the judge chose direct (1-0); informational facts 5 of 8, contradictions 0 of 2 | composed | 3 | BRT-ORC-029 once, 032 twice | none | none | no win: judge chose direct |

**Job C's costs are understated by one compose call each** (planner ruling 35e). Both C runs' first compose reply was valid JSON but not an object. Its log row was never written: a `try/except` around the logging swallowed the error, so that call's dollars are missing from both figures above. The cause was found and fixed in ruling 35e. Each missing call is about the size of the run's other compose posts (estimated US$0.03 to 0.04).

## Plain reading

- What the fixes changed: packing (A's 200 items in 10 calls, D's 60 in 3), the optional "(only if" field (D's 31 failures gone, D 0.60 to 1.00), the plan checks 035 and 036 (they caught the bad schema and the stray key), and compose's check 041 (it caught a guessed value and the re-ask gave an honest answer). B's answer now holds its files.
- Job A loses because no step produced urgency. Correction after the evidence (causes note, "trial 32g"): in the round-2-data run the planner's FIRST plan already added urgency through `skill_args.extra_fields`. BRT-ORC-035 refused it only because the schema also named `file` and `category`, renames of the skill's `item` and `bucket`. The 200-character message cut the word `urgency`, and the re_plan dropped the field. In the round-1-data run the planner never added it. In the round-2-data run its two later answerer attempts failed, and their US$0.127 put A over direct. Category at 0.935 would also lose: the rule needs accuracy within 2 points of direct's 1.00.
- Job C loses on the judge: no call sees two documents together (ruling 33c's question to Deej).
- Job B loses by one reference test.
- The evidence on job A is in `docs/design/bench_round2_measurements.md`, section "trial 32g".

## Files

- `trial32g_r2data/` and `trial32g_r1data/`: the `*_borg_cloud_1.json` result files, `judge_C.json`, and the copied direct files. Run databases are under `dbs/` (git-ignored).
- The launcher: `demo/runs/trial_32g.ps1` (untracked); its log is `demo/runs/trial_32g.log.watch`.
