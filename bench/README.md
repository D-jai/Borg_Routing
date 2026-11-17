# BORG_ROUTE benchmark

This folder holds the benchmark that decides whether BORG_ROUTE earns its claim: the same job, done by BORG_ROUTE and by one call to a top model, compared on cost and quality. Everything below — the win rule, how failures count, how repeats are combined, which models play which part — is written here **before any run**, and it is not changed after a number has been seen.

## The win rule (fixed before any run)

BORG_ROUTE wins a job when its **median cost is at most 70% of the direct arm's median cost** and **quality is not worse**:

| Job | "Quality is not worse" means |
|---|---|
| A (200 support emails) and D (60 client notes) | accuracy within 2 percentage points |
| B (build a small service) | at least as many machine checks pass |
| C (questions over about 150,000 tokens) | the judge does not prefer the direct answer in a majority of comparisons |

Medians are taken over the repeats of each arm. Cost is the dollars on the logged rows of the run tree (every model call, including the planner and the compose step), not an estimate. No number is rounded in BORG_ROUTE's favour. If BORG_ROUTE wins no job, nothing is tuned: the results are reported as they stand.

Job D has no direct arm (sending client notes to a top cloud model in one call is what the local lane exists to avoid). It compares `borg_local_workers` (`raw_local`) with `borg_cloud`: the local arm wins if its median cost is at most 70% of `borg_cloud`'s and its accuracy is within 2 points. A direct arm for D can be run on request with `--arms direct`; it is not part of the default matrix.

## How results are read (fixed before any run)

`bench/aggregate.py` applies these rules to the result files; the summary quotes its output.

- **A run either completes or fails.** A run is "failed" when BORG_ROUTE's pipeline ends in failure, or, for the direct arm, when the answer is empty or still cut off (`finish_reason` "length") after its one retry, or when any call errors.
- **Failed runs count, on both sides.** A failed BORG run is a quality loss for its repeat, and its dollars stay in its arm's median cost. A failed direct run counts against direct in the same way. A run that dies early and cheaply cannot buy a cost win: it still loses its quality comparison.
- **Per-run pairing.** Repeat *n* of an arm is compared with repeat *n* of the arm it is measured against (direct for A, B and C; `borg_cloud` for D's local arm). Every repeat pair is one comparison, failed runs included:
  - a failed run against a completed one: the completed one is preferred; two failed runs: a tie;
  - A and D, both completed: one side is preferred only when its accuracy is more than 2 points higher; otherwise a tie;
  - B, both completed: the side with more machine checks passed is preferred; equal is a tie;
  - C, both completed: the judge's verdict for that pair (below).
- **Quality holds** unless the direct arm (D: `borg_cloud`) is preferred in a majority of all comparisons. With three repeats, direct preferred twice means quality does not hold.
- **Cost holds** when the BORG arm's median cost is at most 70% of the other arm's. **A win** needs both.
- **No verdict** is given for a job with an unfinished repeat (missing or refused by the budget guard): it is reported "incomplete". A local arm written as "not run" is reported "not run". Job B is "void" if any of its runs lacked Docker (below).

Job C's win rule is unchanged in wording: "the judge does not prefer the direct answer in a majority of comparisons". Its comparisons are the repeat pairs above, and a failed run is handled exactly as listed (no judge call is made for a pair with a failed run).

## The models (fixed before any run)

All rows come from the bench's own matrix, `bench/pool_matrix.yaml` (`--matrix`, the default). Rows are checked **in memory** with `--check-model`; neither this file nor `data/capability_matrix.yaml` is ever written by the runner.

- **Baseline (the direct arm): `openrouter/anthropic/claude-opus-5.5`**, US$4.00 per million input tokens and US$20.00 per million output tokens, context 1,000,000 tokens, from the live OpenRouter models list, checked 17-Nov-2026. Reason: it is the newest Anthropic flagship listed at or below claude-opus-4.7's price (US$5 / US$25). It is pinned with `--baseline`. It is **not** in BORG_ROUTE's pool (its row is unchecked).
- **BORG_ROUTE's own models**: the checked pool. Its planner and every step's model are chosen by the engine from the checked pool, as in normal use; **the planner is not the baseline model.** The 3.3 run checks claude-opus-4.7, gemini-3.1-pro-preview, grok-4.20, gpt-5.3-codex, deepseek-v3.2, gpt-5.4-nano, gemini-3.1-flash-lite-preview and ollama/llama3.2:3b, in addition to claude-sonnet-4.6 and kimi-k2-thinking, which the pool file already checks.
- **Judge (job C): `openrouter/qwen/qwen4-max`**, US$1.425 per million input tokens and US$4.275 per million output tokens, live OpenRouter list, checked 17-Nov-2026. **Vendor rule:** the judge's vendor has no model in the checked pool and is not the baseline's vendor; the runner refuses any other judge. If no such vendor exists, two judges are used, one from each side's vendor (`--judge-model` twice); each must pass the sanity case, each judges every pair twice with the order swapped, and BORG_ROUTE takes a comparison only when both judges prefer it (any judge preferring direct gives it to direct; otherwise a tie).

## Fairness rules for the two arms

- **Same output caps.** The direct arm uses BORG_ROUTE's own answerer cap policy (the engine's `_answerer_caps`): `ANSWERER_MAX_TOKENS` (default 2000), raised to at least 8000 for reasoning-family models, with the job's expected output (`est_output_tokens`) as a floor, the same way a step's `max_output_tokens` is a floor inside BORG_ROUTE. On `finish_reason` "length" it retries once at double the cap (at most 16,000), as BORG answerers do. Each result records every call's `finish_reason` and both attempts' caps, tokens and dollars.
- **Same text.** The direct arm gets the instruction and the documents in one call, or the fewest calls that fit its context, then a merge call. BORG_ROUTE gets the same instruction with the documents handed over by reference.
- **The judge** sees answers only as "1" and "2", judges each pair twice with the order swapped (a split is a tie), and has up to 4000 output tokens per call (room for a reasoning model to think). A reply that cannot be read is asked again once; if it is still unreadable, that round counts as **"direct preferred"** (the conservative side). Before judging, it must pass a sanity case: a deliberately truncated wrong answer has to lose both times (an unreadable sanity round counts as a failure), or it refuses to judge.

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
- **spec checks**, read from `jobs/job_b_spec.md`: the SQL creates every table the spec names (`notes`, `tags`, `note_tags`); the OpenAPI YAML has every endpoint the spec lists (path and method; the path parameter's name may differ); `app.py` assigns `app = FastAPI(...)` and has a route handler for every endpoint (read with `ast`, never imported);
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

## Estimate, budget and the spend ledger

- **Estimate.** The dry run prices the direct arm on the baseline. BORG arms (local arms too, since their planner and compose steps run in the cloud) are priced as an upper bound: every token at the **highest checked pool input price and the highest checked pool output price**, plus the answerer system prompt (governance header, answerer prompt and the instruction, measured from `src/prompts` at run time) **once per item** (email, note or document; one for job B). The judge is priced with every call at its full 4000-token cap and one re-ask in four. The estimate must come to **US$14 or less** for the full matrix; if not, the runner lists what to shrink, in the order set in `shrink_rules.yaml` (job C first, then repeats from three to two); apply with `--shrink-steps N`.
- **Budget guard.** Before each run (and before judging), if the ledger's sum plus that run's estimate — the same conservative estimate — would pass **US$20** (the phase ceiling), the run is refused and a result file with status `refused` is written.
- **Ledger** (`results/spend.json`), append-only. Each run (and the judge) adds an entry **when it starts**, with status `started` and the path of its database or calls log, and completes it with its logged dollars when it ends; a rerun adds a new entry, never replaces one. If the runner crashes, the next start recovers every `started` entry by summing the dollars logged in that run's database and calls log. A run that raises records the dollars it logged, never 0.
- **3.1's scoring spend counts.** The lead passes it once with `--prior-spend X` (the dollars the 3.1 rescoring logged) before the first run, so the US$20 guard covers the whole phase.
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
| `run_bench.py` | The runner: job x arm x repeat, the judge step, the dry run |
| `aggregate.py` | The win rule over repeats, as written above |
| `checks.py` | Job B machine checks |
| `judge.py` | Job C pairwise judge with a sanity case |
| `facts.py` | Job C's informational fact score |
| `llm.py` | One priced chat call, and the capped call with its retry |
| `budget.py`, `shrink_rules.yaml` | Spend ledger, budget guard, and what to shrink if the estimate is over |
| `results/<YYYY_MM_DD>/` | One JSON per run (`<job>_<arm>_<n>.json`), `judge_C.json`, and `dbs/` (git-ignored); `results/spend.json` is the ledger |

## Running it

```
python bench/run_bench.py --dry-run --baseline openrouter/anthropic/claude-opus-5.5 \
    --judge-model openrouter/qwen/qwen4-max --check-model <each pool model> ...   # spends nothing
python bench/run_bench.py --prior-spend <3.1 scoring dollars> --date <YYYY_MM_DD> <same flags>   # spends money
python bench/make_data.py --check                                                 # the committed data matches the seed
```

The runner reads `.env` (`--env-file`) before it chooses the baseline. Without `--baseline`, the baseline is `BASELINE_MODEL_ID` if set, else the checked model with the highest reasoning score in the matrix file.

Tests: `python -m pytest tests/bench -q` (against the stand-in model server and mocked Docker; no network, no spend).
