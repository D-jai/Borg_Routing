# Demo data and demo runs

`demo/data/` holds fictional data written for the recorded demo runs (planner ruling 24). Each file opens with a header line saying so. No real person or company appears in it, and no claim cites it.

`scripts/record_demo_runs.py` records three demo runs (`--job 1|2|3|all`, `--dry-run` to check first and spend nothing) through the same pipeline function the Ask page uses, into `demo/runs/db/demo.db` (gitignored, never the app's main database). Spend is capped at US$1.00 in `bench/results/spend.json`, every run is labelled "demo run" there and in `demo/runs/manifest.json`. Job 3 holds a canary string on purpose: the replay export (work order 4.5) must refuse a file that contains it.

Demo runs show what the product does. They back no claim and no number in the README or the deck.

Matrix: demo runs use the bench's proof pool, checked in memory (`bench/pool_matrix.yaml` through `bench/run_bench.py`'s `load_matrix`, the rows the round-1 BORG arms checked; no matrix file is written).
