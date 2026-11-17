# BORG_ROUTE benchmark

This folder holds the benchmark that decides whether BORG_ROUTE earns its claim: the same job, done by BORG_ROUTE and by one call to a top model, compared on cost and quality. Everything below — the win rule, how failures count, how repeats are combined, which models play which part — is written here **before any run**, and it is not changed after a number has been seen.

## The win rule (fixed before any run)

BORG_ROUTE wins a job when its **median cost is at most 70% of the direct arm's median cost** and **quality is not worse**:

| Job | "Quality is not worse" means |
|---|---|
| A (200 support emails) and D (60 client notes) | accuracy within 2 percentage points |
| B (build a small service) | at least as many machine checks pass |
| C (questions over about 150,000 tokens) | the judge does not prefer the direct answer in a majority of comparisons |

Medians are taken over the repeats of each arm; a job with a single repeat reports that run's numbers instead (see "The staged plan"). Cost is the dollars on the logged rows of the run tree (every model call, including the planner and the compose step), not an estimate. No number is rounded in BORG_ROUTE's favour. If BORG_ROUTE wins no job, nothing is tuned: the results are reported as they stand.

Job D has no direct arm (sending client notes to a top cloud model in one call is what the local lane exists to avoid). It compares `borg_local_workers` (`raw_local`) with `borg_cloud`: the local arm wins if its median cost is at most 70% of `borg_cloud`'s and its accuracy is within 2 points. A direct arm for D can be run on request with `--arms direct`; it is not part of the default matrix.

## How results are read (fixed before any run)

`bench/aggregate.py` applies these rules to the result files; the summary quotes its output.

- **A run either completes or fails.** A run is "failed" when BORG_ROUTE's pipeline ends in failure, or, for the direct arm, when the answer is empty or still cut off (`finish_reason` "length") after its one retry, or when any call errors.
- **Failed runs count, on both sides.** A failed BORG run is a quality loss for its repeat, and its dollars stay in its arm's median cost. A failed direct run counts against direct in the same way. A run that dies early and cheaply cannot buy a cost win: it still loses its quality comparison.
- **Per-run pairing.** Repeat *n* of an arm is compared with repeat *n* of the arm it is measured against (direct for A, B and C; `borg_cloud` for D's local arm). Every repeat pair is one comparison, failed runs included:
  - a failed run against a completed one: the completed one is preferred; two failed runs: **no verdict** (the pair is left out of the comparisons; it is not a tie);
  - A and D, both completed: one side is preferred only when its accuracy is more than 2 points higher; otherwise a tie;
  - B, both completed: the side with more machine checks passed is preferred; equal is a tie;
  - C, both completed: the judge's verdict for that pair (below).
- **Quality holds** unless the direct arm (D: `borg_cloud`) is preferred in a majority of the comparisons (pairs with a verdict). With three comparisons, direct preferred twice means quality does not hold. If every pair failed on both sides there is nothing to compare and the job gets **no verdict**.
- **Cost holds** when the BORG arm's median cost is at most 70% of the other arm's, **and** at least one of the BORG arm's runs completed, **and** the other arm's cost is above zero. An arm whose every run failed cannot win on cost, however cheap its failures were. **A win** needs both cost and quality to hold.
- **Aborted attempts** (a run cut by a transport error and rerun, below) are listed beside the run that replaced them, with their real spend, for both arms alike; that spend counts in the ledger but not in the arm's median.
- **One repeat is one run.** `aggregate.py` marks a job with a single repeat "one run (thinner evidence)" and reports that run's cost and quality as they are, never as a median. With two or three repeats it reports the median of those repeats.
- **No verdict** is given for a job with an unfinished repeat (missing or refused by the budget guard): it is reported "incomplete". A local arm written as "not run" is reported "not run". Job B is "void" if any of its runs lacked Docker (below).

Job C's win rule is unchanged in wording: "the judge does not prefer the direct answer in a majority of comparisons". Its comparisons are the repeat pairs above, and a failed run is handled exactly as listed (no judge call is made for a pair with a failed run; a pair with both runs failed is no verdict).

## The models (fixed before any run)

All rows come from the bench's own matrix, `bench/pool_matrix.yaml` (`--matrix`, the default). Rows are checked **in memory** with `--check-model`; neither this file nor `data/capability_matrix.yaml` is ever written by the runner.

- **Baseline (the direct arm): `openrouter/anthropic/claude-opus-5.5`**, US$4.00 per million input tokens and US$20.00 per million output tokens, context 1,000,000 tokens, from the live OpenRouter models list, checked 17-Nov-2026. Reason: it is the newest Anthropic flagship listed at or below claude-opus-4.7's price (US$5 / US$25). It is pinned with `--baseline`. It is **not** in BORG_ROUTE's pool (its row is unchecked).
- **BORG_ROUTE's own models**: the checked pool. Its planner and every step's model are chosen by the engine from the checked pool, as in normal use; **the planner is not the baseline model.** The 3.3 run checks claude-opus-4.7, gemini-3.1-pro-preview, grok-4.20, gpt-5.3-codex, deepseek-v3.2, gpt-5.4-nano, gemini-3.1-flash-lite-preview and ollama/llama3.2:3b, in addition to claude-sonnet-4.6 and kimi-k2-thinking, which the pool file already checks.
- **Judge (job C): `openrouter/qwen/qwen4-max`**, US$1.425 per million input tokens and US$4.275 per million output tokens, live OpenRouter list, checked 17-Nov-2026. **Vendor rule:** the judge's vendor has no model in the checked pool and is not the baseline's vendor; the runner refuses any other judge. A local row names a model family, so it counts for its maker: `ollama/llama3.2:3b` is a **Meta** model, and a Meta judge (`meta-llama/...`) is refused while it is in the pool (likewise qwen -> Qwen, gemma -> Google, mistral -> Mistral, phi -> Microsoft). If no such vendor exists, two judges are used, one from each side's vendor (`--judge-model` twice); each must pass the sanity case, each judges every pair twice with the order swapped, and BORG_ROUTE takes a comparison only when both judges prefer it (any judge preferring direct gives it to direct; otherwise a tie).

## Fairness rules for the two arms

- **Same output caps.** The direct arm uses BORG_ROUTE's own answerer cap policy (the engine's `_answerer_caps`): `ANSWERER_MAX_TOKENS` (default 2000), raised to at least 8000 for reasoning-family models, with the job's expected output (`est_output_tokens`) as a floor, the same way a step's `max_output_tokens` is a floor inside BORG_ROUTE. On `finish_reason` "length" it retries once at double the cap (at most 16,000), as BORG answerers do. Each result records every call's `finish_reason` and both attempts' caps, tokens and dollars.
- **Same text.** The direct arm gets the instruction and the documents in one call, or the fewest calls that fit its context, then a merge call. BORG_ROUTE gets the same instruction with the documents handed over by reference.
- **The judge** sees answers only as "1" and "2", judges each pair twice with the order swapped (a split is a tie), and has up to 4000 output tokens per call (room for a reasoning model to think). A reply that cannot be read is asked again once; if it is still unreadable, that round counts as **"direct preferred"** (the conservative side). Before judging, it must pass a sanity case: a deliberately truncated wrong answer has to lose both times (an unreadable sanity round counts as a failure), or it refuses to judge. **A judge that fails or refuses makes the runner exit non-zero (exit code 6)**: there is no job C verdict.
- **Length and format are recorded.** `judge_C.json` records, beside each pair's verdict, each answer's length (characters, words) and format markers (heading count, list-item count, code blocks, document part-id references such as `doc_003#2`), so a reader can see whether the verdicts tracked length or format. The judge never sees these numbers.

## Arms per job

All data is handed over **as documents by reference** (planner ruling 2, 16-Nov-2026): the operator's message holds only the short instruction, and the files sit under `BORG_DOCS_ROOT`, which the runner points at `bench/data` for each run.

| Job | `direct` | `borg_cloud` | `borg_local_workers` |
|---|---|---|---|
| A: triage 200 emails | same text, one call to the baseline model (or the fewest calls that fit its context) | `lane_mode: by_reference` | `lane_mode: raw_local` (local steps only) |
| B: build the service | the spec in one call | no lane (the spec is short and goes in the message) | none |
| C: questions over 15 documents | same text, one call (or the fewest that fit, then a merge call) | `lane_mode: by_reference` | none |
| D: label 60 client notes | none by default | `lane_mode: by_reference` | `lane_mode: raw_local` |

`by_reference` makes no privacy promise; it only keeps bulk data out of the prompt.

## Local arms (ruling 13)

The local arms run in this build: `borg_local_workers` for job A and the `raw_local` arm of job D, on **`ollama/llama3.2:3b` served by Ollama in Docker on the ThinkPad (CPU only, 16 GB machine)**, checked in memory with `--check-model ollama/llama3.2:3b`. They write to their own dated results folder (`--date`), and the cloud arms are not re-run for them. In `raw_local`, a raw step whose tier floor the local model does not clear fails with **BRT-LANE-002** (no data is sent to the cloud instead); such a run counts as a quality loss under the rules above, with its dollars in the median. `--skip-local` remains available (off by default): a skipped local arm writes a result file with status `"not run"` and the reason, never a number. With no checked local model, the local arms are also written as `"not run"`.

## Job B: requires Docker

Generated code is never run outside a container. The checks, all in `checks.py`:

- the four files are present; the SQL executes in SQLite (in memory); the Python files parse; the YAML loads with `openapi` and `paths`;
- **spec checks**, read from `jobs/job_b_spec.md`: the SQL creates every table the spec names (`notes`, `tags`, `note_tags`); the OpenAPI YAML has every endpoint the spec lists (path and method; the path parameter's name may differ). There is **no handler check** on `app.py`'s source (dropped by the round-2 review, 17-Nov-2026: it scored down a valid app that registers routes another way); whether the endpoints work is decided by the reference suite, which calls them;
- the answer's own tests, run in the container (an answer with no test file fails this check);
- **our reference suite**, `jobs/job_b_reference_tests/` (11 tests written from the spec, one check each), run against the answer's `app.py` in the container.

Both test runs use `docker run --network none` with a memory cap, a process cap, a read-only file system and a timeout. Build the image once, with network: `docker build -t borg_bench_py bench/docker`. **If Docker or the image is unavailable for any job B run (or `--no-docker` is given), every job B result is void**, for both arms: no job B verdict is given.

## Job C: the fact score is informational

Each job C result records `informational_fact_score` (`facts.py`): planted facts matched after normalising numbers and units (thousands commas, US$, "million"/"m", number words, dates in any common order; counts must carry the key's unit), and contradictions found only when both conflicting values and a topic word appear close together. **It is never quoted** and plays no part in the win rule: the judge decides job C.

## Data

- **A:** 200 short emails (`email_001.txt` ...), each with one of five categories (billing, technical, account_access, shipping, feedback) and an urgency from 1 to 5. Quality is the share of emails with both fields right.
- **B:** `jobs/job_b_spec.md`, a notes service in SQL, FastAPI, OpenAPI YAML and pytest. Quality is the number of machine checks passed.
- **C:** 15 documents of about 600,000 characters (about 150,000 tokens) with eight planted facts and two planted contradictions. The answer key lists them.
- **D:** 60 client notes, each with a canary account number (`ACCT-CANARY-nnnn-nnnn`) and a risk label (low, medium, high). The result records whether any canary reached a request that left the machine.

## The staged plan (planner ruling 14; fixed before any paid run)

Paid runs go in two stages, so that money is spent on measured cost, not on guesses.

- **Stage 1** (`--stage 1`): one repeat of every cell (job x arm), ordered by conservative estimate, cheapest first. Before each run the guard above applies with that run's estimate; a refusal stops the stage.
- **Tripwire.** After each run, if its real logged spend (the run plus any aborted transport attempts) is more than its own estimate, everything stops and every remaining run is re-estimated from measured costs: the same cell's dearest observed run, rounded up to the next cent (a run that logged US$0 counts as US$0); a cell not yet measured gets its conservative estimate **multiplied by the worst real-to-estimate ratio observed so far** (at least 1; a run that cost 4 times its estimate makes every unmeasured cell 4 times its estimate). The budget guard then uses these re-estimates for every remaining run. The re-estimate (with the ratio) is written to `results/<date>/reestimate_s<stage>_<k>.json` and printed. The stage continues only if the ledger's spend plus the re-estimated remainder is at most US$20; otherwise it stops (exit code 5).
- **Stage 2** (`--stage 2`, same `--date`): from stage 1's result files, extra repeats of every cell are estimated from measured cost (the dearest observed run of that cell, rounded up to the cent). **Only a job whose every repeat-1 cell finished** (completed or failed) gets extra repeats; a job with a repeat-1 cell missing, refused, still marked started, or "not run" (except a local arm written "not run" by design) is skipped, with the reason printed. Repeats are added **by whole job**, so every arm of a job has the same number of repeats, in the fixed **order A, D, B, C**: each job tries **both extra repeats (2 and 3), else one extra repeat (2)**, and is added at the first count where ledger spend plus the estimate for those extra repeats (the judge's calls for the new pairs included for job C) is at most US$20. A job that fits at neither count is skipped and the later jobs are still tried in order. `--dry-run --stage 2` prints the plan and spends nothing.
- **The decision to add or stop repeats looks at cost only, never at which arm is ahead.** The code that decides reads each result file's status and `cost_dollars` and nothing else; a test swaps the quality results and asserts the plan does not change.
- **One-repeat jobs.** A job that ends with one repeat is reported as "one run (thinner evidence)" with that run's numbers, not a median. The win rule's thresholds are unchanged.
- **Judge.** Job C is judged after each stage that ran its runs, and its estimate is part of the stage's remaining spend. **Verdicts already stored for repeat 1 are kept and reused, never re-judged** in stage 2 (no paying twice, no replacing a verdict): stage 2 judges only the new pairs, after a fresh sanity case, and if that judging fails the stored verdicts stay in `judge_C.json` beside the failure.

### The run time limit (fixed before stage 1)

Every arm runs under **`BORG_MAX_RUN_SECONDS=7200`** (two hours), set by the runner for each BORG run, so no run is cut by the engine's default 300 s. Reason: the local arms run `ollama/llama3.2:3b` on a CPU-only 16 GB machine, and they must be able to finish and be timed as measured; the same limit applies to the cloud arms so that no arm is helped or hurt by a clock. The direct arm's HTTP timeout is also **7200 s** per call, so a long answer from a reasoning model is not cut by the client. Each result records its wall time (`wall_seconds`); local wall time is reported as measured, never adjusted.

### The routing configuration (fixed for every arm, written into every result file as `routing_config`)

- **Leaf floors 6.0 / 7.5 / 9.0** (light / mid / heavy): the engine defaults, unchanged. The runner removes any `LEAF_QUALITY_*` override from the environment for a BORG run.
- **Time cost per second:** the engine default (0.0001); any `TIME_COST_PER_SEC` override is removed for a BORG run.
- **The pool:** the checked rows passed to the run (`bench/pool_matrix.yaml` with the `--check-model` rows). Nothing outside it can be chosen.
- **Lane mode per arm:** `borg_cloud` is `by_reference` (jobs A, C, D) or no lane (job B); `borg_local_workers` is `raw_local` (jobs A and D); the direct arm has no lane.
- **Receipts baseline:** `BASELINE_MODEL_ID` is set to the bench baseline (`openrouter/anthropic/claude-opus-5.5`) for each BORG run. The engine looks the id up in the matrix it is given, which holds every row of `bench/pool_matrix.yaml` whether checked or not, so it can resolve a baseline that is not in the pool. If it could not, the run would use the engine's own default (the highest-reasoning checked model) and `routing_config.baseline_note` would say so.
- **A raw step that fails BRT-LANE-002** (a local model below the tier floor, so the data is not sent to the cloud instead) is a **failed BORG run**, counted as a quality loss under the rules above. It is never a reason to move a floor.

## Estimate, budget and the spend ledger

- **Estimate.** The dry run prices the direct arm on the baseline. BORG arms (local arms too, since their planner and compose steps run in the cloud) are priced as an upper bound: every token at the **highest checked pool input price and the highest checked pool output price**, plus the answerer system prompt (governance header, answerer prompt and the instruction, measured from `src/prompts` at run time) **once per item** (email, note or document; one for job B). The judge is priced with every call at its full 4000-token cap and one re-ask in four. This is the *conservative estimate*. It is a planning number, not a limit: there is no pre-flight ceiling on the whole matrix (planner ruling 14 replaced the earlier whole-matrix pre-flight rule).
- **Budget guard.** Before each run (and before judging), if the ledger's sum plus that run's estimate would pass **US$20** (Deej's cap on real logged spend, unchanged), the run is refused, a result file with status `refused` is written, **and the whole stage stops** (exit code 3). A guard failure is a stop, not a warning.
- **Ledger** (`results/spend.json`), append-only. Each run (and the judge) adds an entry **when it starts**, with status `started` and the path of its database or calls log, and completes it with its logged dollars when it ends; a rerun adds a new entry, never replaces one. If the runner crashes, the next start recovers every `started` entry by summing the dollars logged in that run's database and calls log. A run that raises records the dollars it logged, never 0.
- **Earlier scoring spend counts.** The lead passes it once with `--prior-spend X` before the first run, so the US$20 guard covers the whole phase. X is the sum of **both** earlier spends: the 3.1 cloud rescoring of the proof pool, **and** the ruling-13 scoring of the local model (`ollama/llama3.2:3b`, scored through the app's research route, which calls a cloud scoring model).
- **Run databases.** Each BORG run's database is kept at `results/<date>/dbs/<job>_<arm>_<n>.db` (a rerun gets `__2`, `__3`...), and its path is in the result JSON (`db_path`). The direct arm and the judge write a calls log there (`*.calls.jsonl`). The folder is git-ignored.
- **Dates.** `--date YYYY_MM_DD` pins the results folder. It defaults to today, so **a resume on another day must pass the original `--date`**, or it starts a new folder.

## What is here

| Path | What |
|---|---|
| `make_data.py` | Seeded generator for everything in `data/` (`--check` verifies the committed files) |
| `data/job_a/`, `job_c/`, `job_d/` | The documents handed to the runs. Answer keys are in `data/answers/`, outside these folders |
| `jobs/*.yaml`, `jobs/job_b_spec.md` | One file per job: instruction, data folder, arms, quality measure |
| `jobs/job_b_reference_tests/` | Our reference tests for job B (run only in the container) |
| `pool_matrix.yaml` | The bench's matrix: the proof pool, the baseline and judge price rows, the local model |
| `run_bench.py` | The runner: stage 1, stage 2, the tripwire, the judge step, the dry runs |
| `aggregate.py` | The win rule over repeats, as written above |
| `checks.py` | Job B machine checks |
| `judge.py` | Job C pairwise judge with a sanity case |
| `facts.py` | Job C's informational fact score |
| `llm.py` | One priced chat call, and the capped call with its retry |
| `budget.py` | Spend ledger and budget guard |
| `shrink_rules.yaml` | **Superseded** by planner ruling 14 (the staged plan); no longer read by any code, kept for the record |
| `results/<YYYY_MM_DD>/` | One JSON per run (`<job>_<arm>_<n>.json`), `judge_C.json`, and `dbs/` (git-ignored); `results/spend.json` is the ledger |

## Running it

```
python bench/run_bench.py --dry-run --stage 1 --matrix bench/pool_matrix.yaml --baseline openrouter/anthropic/claude-opus-5.5 \
    --judge-model openrouter/qwen/qwen4-max --check-model <each pool model> ...     # spends nothing
python bench/run_bench.py --stage 1 --prior-spend <3.1 scoring dollars> --date <YYYY_MM_DD> <same flags>   # spends money
python bench/run_bench.py --dry-run --stage 2 --date <same date> <same flags>      # the stage 2 plan, spends nothing
python bench/run_bench.py --stage 2 --date <same date> <same flags>                # spends money
python bench/make_data.py --check                                                  # the committed data matches the seed
```

A paid run without `--stage` is refused. The runner reads `.env` (`--env-file`) before it chooses the baseline. Without `--baseline`, the baseline is `BASELINE_MODEL_ID` if set, else the checked model with the highest reasoning score in the matrix file.

Tests: `python -m pytest tests/bench -q` (against the stand-in model server and mocked Docker; no network, no spend).
