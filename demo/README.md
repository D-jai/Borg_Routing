# Demo data and demo runs

`demo/data/` holds fictional data written for the recorded demo runs (planner ruling 24). Each file opens with a header line saying so. No real person or company appears in it, and no claim cites it.

`scripts/record_demo_runs.py` records three demo runs (`--job 1|2|3|all`, `--dry-run` to check first and spend nothing) through the same pipeline function the Ask page uses, into `demo/runs/db/demo.db` (gitignored, never the app's main database). Spend is capped at US$1.00 in `bench/results/spend.json`, every run is labelled "demo run" there and in `demo/runs/manifest.json`. Job 3 holds a canary string on purpose: the replay export (work order 4.5) must refuse a file that contains it.

Demo runs show what the product does. They back no claim and no number in the README or the deck.

Matrix: demo runs use the bench's proof pool, checked in memory (`bench/pool_matrix.yaml` through `bench/run_bench.py`'s `load_matrix`, the rows the round-1 BORG arms checked; no matrix file is written).

Replay files (work order 4.5): `demo/runs/job<N>_<run8>.json`, written by `scripts/export_run.py` from the demo database and committed (they hold no key, no absolute path, no canary and no raw document text; `tests/scripts/test_demo_exports.py` checks this). `BORG_DEMO_MODE=replay` plays them (`docs/user-guide.md`, "Replay demo"). The recorder keeps each run's final answer in `demo/runs/db/answers/<run_id>.md` (git-ignored), because the run database does not store it; the export reads it from there. The three files today: job 1 run d04e0d8a, job 2 run e535b654, job 3 run e31a5330 (the lane run; exported as a non-admin sees it with the lane on). Runs 19f56430, ece3d973 and 230b0186 are not exported.
