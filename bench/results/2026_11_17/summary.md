# Bench round 1, 17-Nov-2026: summary

**Verdict: BORG_ROUTE won no job.** Five of six BORG runs failed on product defects; the one that completed (job B) did not meet the cost rule. Every job is a single run (see the deviation below), so every number here is one run, not a median, and the evidence is thin.

The rules were fixed in `bench/README.md` before any paid run. Nothing in them, the data or the pool was changed after seeing these results.

## Deviation from the pre-registered stage 2 rule

Deviation from the pre-registered stage 2 rule, decided after stage 1 on 17-Nov-2026. The rule selected all four jobs for repeats. Five of six BORG runs failed on product defects that repeat on every run, so repeats would have spent about US$5.25 to reproduce known crashes. No repeats were run for any job. Every job is reported as a single run, and the verdict recorded is the one least favourable to BORG_ROUTE: no win on any job.

## Results, one run per cell

Direct arm: one call to `anthropic/claude-opus-5.5` (US$4 / US$20 per million tokens). BORG arms: the engine routes each step over the checked pool (`bench/pool_matrix.yaml`, 10 models including the local `llama3.2:3b`).

| Job | Arm | Status | Cost (US$) | Wall time | Input / output tokens | Quality |
|---|---|---|---|---|---|---|
| A, 200 support emails | direct | completed | 0.2085 | 48 s | 16,693 / 7,085 | accuracy 100% (200 of 200) |
| A | borg_cloud (by_reference) | **failed** (BRT-ORC-009 at compose) | 0.3623 | 42 s | 755,735 / 24,784 | 0 answered |
| A | borg_local_workers (raw_local) | **failed** (BRT-ORC-009 at compose) | 0.0447 | 16 s | 34,250 / 749 | 0 answered |
| B, small Python service | direct | completed | 0.3281 | 128 s | 1,616 / 16,084 | 18 of 18 checks |
| B | borg_cloud | completed | 0.3020 | 234 s | 42,334 / 20,740 | 18 of 18 checks |
| C, 15 documents, about 150k tokens | direct | completed | 0.7977 | 11 s | 195,460 / 791 | wins the pair by default (BORG gave no answer; the judge made no call) |
| C | borg_cloud (by_reference) | **failed** ("'str' object has no attribute 'get'") | 0.2119 | 38 s | 40,420 / 13,421 | no answer |
| D, 60 client notes | borg_cloud (by_reference) | **failed** (BRT-ORC-009 at compose) | 0.1755 | 48 s | 304,213 / 9,322 | 0 answered; 120 canaries sent to cloud models |
| D | borg_local_workers (raw_local) | **failed** (BRT-ORC-009 at compose) | 0.0589 | 4,646 s | 128,838 / 10,467 | 0 answered; **0 canaries sent to cloud models** |

## Win rule, per job

The rule (`bench/README.md`): BORG wins a job when its cost is at most 70% of the direct arm's and quality is not worse.

| Job | Verdict | Why |
|---|---|---|
| A | no win | both BORG arms failed |
| B | no win | BORG cost 92% of direct (US$0.302 vs US$0.328); quality equal (18 of 18 checks each) |
| C | no win | BORG failed, so the direct answer wins the pair by default; no judge call was needed |
| D | no win | both BORG arms failed (job D has no direct arm by design; with no completed run there is no verdict, recorded here as no win) |

## What the numbers do not show

- **Why BORG failed.** The failures are product defects, not the router choosing badly: in job A the cheap model classified all 200 emails (about US$0.19 of work), then the run died at the final compose step (BRT-ORC-009); job C crashed on a type error. Root causes are being diagnosed (planner ruling 16); nothing here is corrected for them.
- **The token overhead.** BORG's job A cloud run sent 755,735 input tokens against 16,693 for the whole job in one direct call: each item call re-sends the operator instruction, governance and the answerer prompt, and the plan made two passes over the emails. This is a structural cost, measured but not fixed (ruling 17).
- **The receipt's baseline estimate.** The `baseline_dollars` field prices BORG's own tokens at the flagship's prices, so it overstates what one direct call costs (job A cloud: US$3.52 against a real direct cost of US$0.21). It is not quoted anywhere (ruling 18).
- **The on-premise lane on a live run.** The raw_local arm of job D sent no canary to any cloud model, against 120 in the by_reference arm. That is the lane's promise holding on a real run, even though the run failed at compose. With no completed local run, every other claim about the lane rests on the privacy tests.
- **One run per cell.** No medians; a different run could land differently.

## Spend

Ledger (`bench/results/spend.json`): **US$2.5595** of the US$20 cap: US$0.07 entered as prior spend (pool and local-model scoring) and US$2.4896 for the nine runs, priced from logged tokens; the job C judge made no call (US$0). Cross-check against the OpenRouter key's own usage counter: US$0.5761 before stage 1, US$3.0107 after, so the runs actually cost US$2.4346, slightly below the ledger; the ledger errs high.
