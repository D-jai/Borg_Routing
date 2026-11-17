"""--matrix (bench/pool_matrix.yaml by default), --check-model in memory, --baseline, .env read before the
baseline is chosen, and the two price-only rows of rulings 11 and 12. No file under data/ is written."""
from __future__ import annotations

import hashlib
import os

import pytest
import yaml

from bench import run_bench

REPO = run_bench.REPO_ROOT
POOL = REPO / "bench" / "pool_matrix.yaml"
SHIPPED = REPO / "data" / "capability_matrix.yaml"


@pytest.fixture
def clean_env(tmp_path):
    """An empty .env for main(), and the process environment restored afterwards."""
    saved = dict(os.environ)
    os.environ.pop("BASELINE_MODEL_ID", None)
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    yield empty
    os.environ.clear()
    os.environ.update(saved)


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_the_bench_matrix_is_the_pool_file_checked_in_memory_only():
    before = digest(POOL)
    m = run_bench.load_matrix(["openrouter/anthropic/claude-opus-4.7"])
    assert run_bench.DEFAULT_MATRIX == POOL
    assert m.get("openrouter/anthropic/claude-opus-4.7").checked is True
    with pytest.raises(RuntimeError, match="never saved"):
        m.save()
    assert digest(POOL) == before
    rows = {r["model_id"]: r for r in yaml.safe_load(POOL.read_text(encoding="utf-8"))}
    assert rows["openrouter/anthropic/claude-opus-4.7"]["checked"] is False
    with pytest.raises(SystemExit):
        run_bench.load_matrix(["openrouter/nobody/nothing"])


def test_the_baseline_and_judge_rows_carry_the_live_prices_and_are_not_in_the_pool():
    rows = {r["model_id"]: r for r in yaml.safe_load(POOL.read_text(encoding="utf-8"))}
    opus = rows["openrouter/anthropic/claude-opus-5.5"]
    qwen = rows["openrouter/qwen/qwen4-max"]
    assert opus["pricing"] == {"input_per_token": 4.0e-06, "output_per_token": 2.0e-05}
    assert qwen["pricing"] == {"input_per_token": 1.425e-06, "output_per_token": 4.275e-06}
    assert opus["context_length"] == qwen["context_length"] == 1_000_000
    assert opus["checked"] is False and qwen["checked"] is False


def test_baseline_flag_pins_the_direct_model(clean_env, capsys):
    rc = run_bench.main(["--dry-run", "--env-file", str(clean_env), "--date", "2026_01_01",
                         "--baseline", "openrouter/anthropic/claude-opus-5.5", "--judge-model", "openrouter/qwen/qwen4-max"])
    out = capsys.readouterr().out
    assert rc in (0, 1)
    assert "baseline, direct arm : openrouter/anthropic/claude-opus-5.5" in out
    assert "judge, job C         : openrouter/qwen/qwen4-max" in out


def test_an_unknown_baseline_stops_before_anything(clean_env, capsys):
    rc = run_bench.main(["--dry-run", "--env-file", str(clean_env), "--baseline", "openrouter/nobody/nothing"])
    assert rc == 2 and "--baseline" in capsys.readouterr().out


def test_default_baseline_is_the_highest_reasoning_checked_model_of_the_matrix_file(clean_env, capsys):
    run_bench.main(["--dry-run", "--env-file", str(clean_env), "--date", "2026_01_01",
                    "--check-model", "openrouter/anthropic/claude-opus-4.7"])
    assert "baseline, direct arm : openrouter/anthropic/claude-opus-4.7" in capsys.readouterr().out


def test_env_file_is_read_before_the_baseline_is_chosen(clean_env, tmp_path, capsys):
    env = tmp_path / "with_baseline.env"
    env.write_text("BASELINE_MODEL_ID=openrouter/moonshotai/kimi-k2-thinking\n", encoding="utf-8")
    run_bench.main(["--dry-run", "--env-file", str(env), "--date", "2026_01_01"])
    assert "baseline, direct arm : openrouter/moonshotai/kimi-k2-thinking" in capsys.readouterr().out


def test_a_dry_run_writes_no_matrix_file(clean_env, capsys):
    before = (digest(POOL), digest(SHIPPED))
    run_bench.main(["--dry-run", "--env-file", str(clean_env), "--date", "2026_01_01",
                    "--matrix", str(POOL), "--check-model", "openrouter/x-ai/grok-4.20"])
    capsys.readouterr()
    assert (digest(POOL), digest(SHIPPED)) == before


LOCAL = "ollama/llama3.2:3b"


def _row(out, job, arm):
    return next(ln for ln in out.splitlines() if ln.startswith(f"{job:<4}{arm:<20}"))


def test_local_arms_run_by_default_when_a_local_model_is_checked(clean_env, capsys):
    assert run_bench.Config().skip_local is False
    run_bench.main(["--dry-run", "--env-file", str(clean_env), "--date", "2026_01_01", "--check-model", LOCAL])
    out = capsys.readouterr().out
    for job in ("A", "D"):
        row = _row(out, job, "borg_local_workers")
        assert "not run" not in row
        assert float(row.split()[4]) > 0, "a local arm still pays cloud planning: priced like a BORG arm"
    assert "27 cells = 27 to run + 0" in out


def test_without_a_checked_local_model_the_dry_run_says_not_run(clean_env, capsys):
    run_bench.main(["--dry-run", "--env-file", str(clean_env), "--date", "2026_01_01"])
    out = capsys.readouterr().out
    assert "not run" in _row(out, "A", "borg_local_workers") and "27 cells = 21 to run + 6" in out


def test_skip_local_is_still_available(clean_env, capsys):
    run_bench.main(["--dry-run", "--env-file", str(clean_env), "--date", "2026_01_01", "--check-model", LOCAL,
                    "--skip-local"])
    assert "not run" in _row(capsys.readouterr().out, "D", "borg_local_workers")
