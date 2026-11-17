# BORG_ROUTE benchmark

This folder holds the benchmark that decides whether BORG_ROUTE earns its claim: the same job, done by BORG_ROUTE and by one call to a top model, compared on cost and quality. The rule that decides the result is written here **before any run**, and it is not changed after a number has been seen.

## The win rule (fixed before any run)

BORG_ROUTE wins a job when its **median cost is at most 70% of the direct arm's median cost** and **quality is not worse**:

| Job | "Quality is not worse" means |
|---|---|
| A (200 support emails) and D (60 client notes) | accuracy within 2 percentage points |
| B (build a small service) | at least as many machine checks pass |
| C (questions over about 150,000 tokens) | the judge does not prefer the direct answer in a majority of comparisons |

Medians are taken over the repeats of each arm. Cost is the dollars on the logged rows of the run tree (every model call, including the planner and the compose step), not an estimate. No number is rounded in BORG_ROUTE's favour. If BORG_ROUTE wins no job, nothing is tuned: the results are reported as they stand.

Job D has no direct arm (sending client notes to a top cloud model in one call is what the local lane exists to avoid). It compares `borg_local_workers` (`raw_local`) with `borg_cloud`: the local arm wins if its median cost is at most 70% of `borg_cloud`'s and its accuracy is within 2 points. A direct arm for D can be run on request with `--arms direct`; it is not part of the default matrix.

## Arms per job

All data is handed over **as documents by reference** (planner ruling 2, 16-Nov-2026): the operator's message holds only the short instruction, and the files sit under `BORG_DOCS_ROOT`, which the runner points at `bench/data` for each run.

| Job | `direct` | `borg_cloud` | `borg_local_workers` |
|---|---|---|---|
| A: triage 200 emails | same text, one call to the baseline model (or the fewest calls that fit its context) | `lane_mode: by_reference` | `lane_mode: raw_local` (local steps only) |
| B: build the service | the spec in one call | no lane (the spec is short and goes in the message) | none |
| C: questions over 15 documents | same text, one call (or the fewest that fit, then a merge call) | `lane_mode: by_reference` | none |
| D: label 60 client notes | none by default | `lane_mode: by_reference` | `lane_mode: raw_local` |

`direct` is the baseline model the app itself would use (`BASELINE_MODEL_ID`, else the checked model with the highest `reasoning` score). `borg_cloud` may route to any checked model. `by_reference` makes no privacy promise; it only keeps bulk data out of the prompt. The runs use the checked models of the capability matrix as a temporary in-memory copy; `data/capability_matrix.yaml` is never written.

## Local arms are "not run" in this build

`borg_local_workers` (jobs A and D) is fully supported by the runner, but **no local arm has been run and none will be in this build**. Work order 3.1 could not get a local model to serve: Ollama in Docker pulled `qwen3:4b` and timed out loading it twice (17-Nov-2026), so by the standing ruling the local arms are skipped. The runner's `--skip-local` flag is on by default: each local run writes a result file with `"status": "not run"` and the reason, never a number. Job D's verdict therefore stays open, and nothing in the results may be read as a local-model figure. To run them later: start a local model, check it in the matrix, and pass `--no-skip-local`.

## What is here

| Path | What |
|---|---|
| `make_data.py` | Seeded generator for everything in `data/` (`--check` verifies the committed files) |
| `data/job_a/`, `job_c/`, `job_d/` | The documents handed to the runs. Answer keys are in `data/answers/`, outside these folders |
| `jobs/*.yaml`, `jobs/job_b_spec.md` | One file per job: instruction, data folder, arms, quality measure |
| `run_bench.py` | The runner: job x arm x repeat |
| `checks.py` | Job B machine checks (SQL, Python parse, OpenAPI YAML, tests in a no-network container) |
| `judge.py` | Job C pairwise judge with a sanity case |
| `budget.py`, `shrink_rules.yaml` | Spend ledger, budget guard, and what to shrink if the estimate is over |
| `results/<YYYY_MM_DD>/` | One JSON per run (`<job>_<arm>_<n>.json`), plus `judge_C.json`; `results/spend.json` is the ledger |

## Data

- **A:** 200 short emails (`email_001.txt` ...), each with one of five categories (billing, technical, account_access, shipping, feedback) and an urgency from 1 to 5. Quality is the share of emails with both fields right.
- **B:** `jobs/job_b_spec.md`, a notes service in SQL, FastAPI, OpenAPI YAML and pytest. All four outputs are machine-checkable.
- **C:** 15 documents of about 600,000 characters (about 150,000 tokens) with eight planted facts and two planted contradictions. The answer key lists them. The machine also records how many facts and contradictions an answer contains; the judge decides the win rule.
- **D:** 60 client notes, each with a canary account number (`ACCT-CANARY-nnnn-nnnn`) and a risk label (low, medium, high). The result records whether any canary reached a request that left the machine.

## Running it

```
python bench/run_bench.py --dry-run          # matrix and pre-flight estimate; spends nothing
python bench/run_bench.py                    # the real run: this spends money
python bench/make_data.py --check            # the committed data matches the seed
```

- **Budget guard.** `results/spend.json` sums the logged dollars of every run. Before each run, if that sum plus the run's estimate would pass **US$20** (the phase ceiling), the run is refused and a result file with status `refused` is written. `--prior-spend X` records dollars spent earlier in the phase (for example 3.1's scoring).
- **Estimate limit.** `--dry-run` must come to **US$14 or less** for the full matrix. If it does not, the runner lists what to shrink, in the order set in `shrink_rules.yaml` (job C first, then repeats from three to two); apply with `--shrink-steps N`.
- **Job B tests run only in Docker.** Generated code is never run outside a container. Build the image once, with network: `docker build -t borg_bench_py bench/docker`. The tests then run under `docker run --network none` with a memory cap, a process cap and a timeout. If Docker is down or the image is missing the step is reported "skipped" for both arms alike.
- **The judge** is a model in neither arm (not checked, not the baseline). It sees answers as "1" and "2", judges each pair twice with the order swapped (a split is a tie), and must first pass a sanity case: a deliberately truncated wrong answer has to lose both times, or it refuses to judge.
- Tests: `python -m pytest tests/bench -q` (against the stand-in model server; no network, no spend).
