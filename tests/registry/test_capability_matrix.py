"""Tests for src/registry/capability_matrix.py.

TC-REG-010 — load seed file; all 13 models present
TC-REG-011 — query_by_task('coding') returns qwen3-coder first (highest score)
TC-REG-012 — BRT-REG-010 raised on missing file
TC-REG-013 — BRT-REG-011 raised on malformed YAML
TC-REG-014 — BRT-REG-012 raised on update_score for unknown model_id
TC-REG-015 — upsert inserts and replaces correctly
TC-REG-016 — update_throughput persists value on existing model
"""
import tempfile
from pathlib import Path

import pytest
import yaml

from src.registry.capability_matrix import CapabilityMatrix
from src.registry.models import ModelInfo


SEED_PATH = "data/capability_matrix.yaml"


def test_load_seed_file():
    """TC-REG-010 — load the real seed file; expect 13 models."""
    cm = CapabilityMatrix(SEED_PATH)
    cm.load()
    models = cm.all()
    assert len(models) == 13
    model_ids = {m.model_id for m in models}
    # at least one local and one openrouter
    assert any("lmstudio" in mid for mid in model_ids)
    assert any("openrouter" in mid for mid in model_ids)


def test_query_by_task_coding_order():
    """TC-REG-011 — coding task returns model with highest coding score first."""
    cm = CapabilityMatrix(SEED_PATH)
    cm.load()
    results = cm.query_by_task("coding", min_score=0)
    assert len(results) > 0
    scores = [m.task_scores["coding"] for m in results]
    assert scores == sorted(scores, reverse=True)
    # The top coding model should be qwen3-coder or deepseek-r1
    top_id = results[0].model_id.lower()
    assert "qwen" in top_id or "deepseek" in top_id or "coder" in top_id


def test_missing_file_raises_brt_reg_010():
    """TC-REG-012 — BRT-REG-010 raised when file does not exist."""
    cm = CapabilityMatrix("data/does_not_exist_xyz.yaml")
    with pytest.raises(FileNotFoundError) as exc_info:
        cm.load()
    assert "BRT-REG-010" in str(exc_info.value)


def test_malformed_yaml_raises_brt_reg_011():
    """TC-REG-013 — BRT-REG-011 raised on YAML that is not a list."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as f:
        f.write("key: value\n")
        tmp_path = f.name
    cm = CapabilityMatrix(tmp_path)
    with pytest.raises(ValueError) as exc_info:
        cm.load()
    assert "BRT-REG-011" in str(exc_info.value)


def test_update_score_unknown_model_raises_brt_reg_012():
    """TC-REG-014 — BRT-REG-012 raised on update_score for unknown model."""
    cm = CapabilityMatrix(SEED_PATH)
    cm.load()
    with pytest.raises(KeyError) as exc_info:
        cm.update_score("fake/nonexistent-model", "coding", 8.0)
    assert "BRT-REG-012" in str(exc_info.value)


def test_upsert_insert_and_replace():
    """TC-REG-015 — upsert inserts new model and replaces existing one."""
    cm = CapabilityMatrix(SEED_PATH)
    cm.load()
    initial_count = len(cm.all())

    new_model = ModelInfo(
        model_id="lmstudio/test-upsert-model",
        source="lmstudio",
        context_length=4096,
        task_scores={"coding": 7.0},
    )
    cm.upsert(new_model)
    assert len(cm.all()) == initial_count + 1
    assert cm.get("lmstudio/test-upsert-model") == new_model

    # Replace with different context_length
    updated = ModelInfo(
        model_id="lmstudio/test-upsert-model",
        source="lmstudio",
        context_length=8192,
        task_scores={"coding": 7.5},
    )
    cm.upsert(updated)
    assert len(cm.all()) == initial_count + 1
    assert cm.get("lmstudio/test-upsert-model").context_length == 8192


def test_update_throughput_persists():
    """TC-REG-016 — update_throughput changes tps on existing model."""
    cm = CapabilityMatrix(SEED_PATH)
    cm.load()
    first_id = cm.all()[0].model_id
    cm.update_throughput(first_id, 42.5)
    assert cm.get(first_id).throughput_tps == pytest.approx(42.5)
