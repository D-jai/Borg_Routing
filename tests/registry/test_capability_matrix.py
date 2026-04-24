"""Tests for src/registry/capability_matrix.py.

TC-REG-010 — load seed file; all 19 models present (7 local + 12 openrouter)
TC-REG-011 — query_by_task('coding') returns highest-score model first (checked-only)
TC-REG-012 — BRT-REG-010 raised on missing file
TC-REG-013 — BRT-REG-011 raised on malformed YAML
TC-REG-014 — BRT-REG-012 raised on update_score for unknown model_id
TC-REG-015 — upsert inserts and replaces correctly
TC-REG-016 — update_throughput persists value on existing model
TC-REG-017 — query_by_task excludes unchecked rows by default
TC-REG-018 — set_checked flips the flag on a known model; BRT-REG-012 on unknown
TC-REG-019 — update_pricing writes both input/output rates
"""
import tempfile
from pathlib import Path

import pytest
import yaml

from src.registry.capability_matrix import CapabilityMatrix
from src.registry.models import ModelInfo


SEED_PATH = "data/capability_matrix.yaml"


def _loaded_all_checked() -> CapabilityMatrix:
    cm = CapabilityMatrix(SEED_PATH)
    cm.load()
    for m in cm.all():
        cm.set_checked(m.model_id, True)
    return cm


def test_load_seed_file():
    """TC-REG-010 — load the real seed file; expect the curated OpenRouter rows.

    Post-BRT-DS-01 the YAML no longer hand-seeds LM Studio rows (those come in
    live via discovery), so the seed file is the 12 curated OpenRouter rows.
    """
    cm = CapabilityMatrix(SEED_PATH)
    cm.load()
    models = cm.all()
    assert len(models) == 12
    model_ids = {m.model_id for m in models}
    assert all("openrouter" in mid for mid in model_ids)


def test_query_by_task_coding_order():
    """TC-REG-011 — coding task returns model with highest coding score first."""
    cm = _loaded_all_checked()
    results = cm.query_by_task("coding", min_score=0)
    assert len(results) > 0
    scores = [m.task_scores["coding"] for m in results]
    assert scores == sorted(scores, reverse=True)
    # Top coding model should be a frontier OpenRouter model
    top_id = results[0].model_id.lower()
    assert "openrouter/" in top_id


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


def test_query_by_task_excludes_unchecked():
    """TC-REG-017 — with no rows checked, query_by_task returns [] by default."""
    cm = CapabilityMatrix(SEED_PATH)
    cm.load()
    # Every row defaults to checked=False
    results = cm.query_by_task("coding", min_score=0)
    assert results == []
    # Opting out of the filter returns the full population
    all_results = cm.query_by_task("coding", min_score=0, checked_only=False)
    assert len(all_results) > 0


def test_set_checked_flips_flag_and_raises_on_unknown():
    """TC-REG-018 — set_checked flips the flag; BRT-REG-012 raised for unknown model."""
    cm = CapabilityMatrix(SEED_PATH)
    cm.load()
    first_id = cm.all()[0].model_id
    assert cm.get(first_id).checked is False
    cm.set_checked(first_id, True)
    assert cm.get(first_id).checked is True
    cm.set_checked(first_id, False)
    assert cm.get(first_id).checked is False

    with pytest.raises(KeyError) as exc_info:
        cm.set_checked("fake/nonexistent", True)
    assert "BRT-REG-012" in str(exc_info.value)


def test_update_pricing_writes_both_rates():
    """TC-REG-019 — update_pricing replaces pricing on an existing model."""
    cm = CapabilityMatrix(SEED_PATH)
    cm.load()
    # Find any openrouter row (has pricing) or add one
    target = next((m for m in cm.all() if m.source == "openrouter"), None)
    assert target is not None
    cm.update_pricing(target.model_id, 1.23e-6, 4.56e-6)
    updated = cm.get(target.model_id)
    assert updated.pricing is not None
    assert updated.pricing.input_per_token == pytest.approx(1.23e-6)
    assert updated.pricing.output_per_token == pytest.approx(4.56e-6)

    with pytest.raises(KeyError) as exc_info:
        cm.update_pricing("fake/nonexistent", 1e-6, 2e-6)
    assert "BRT-REG-012" in str(exc_info.value)
