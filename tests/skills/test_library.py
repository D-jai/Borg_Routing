"""Tests for src/skills/library.py.

TC-SKL-001 — load() populates skills from data/skills.yaml
TC-SKL-002 — get() returns skill by id
TC-SKL-003 — get() returns highest version when version=None
TC-SKL-004 — BRT-SKL-003 raised for unknown skill_id
TC-SKL-005 — BRT-SKL-004 raised for wrong version
TC-SKL-006 — BRT-SKL-001 raised for missing file
TC-SKL-007 — catalogue_for_prompt() includes all skill ids
TC-SKL-008 — render() returns valid WrappedTask for classify_into_buckets
TC-SKL-009 — WrappedTask from render() passes validate_outbound
TC-SKL-010 — BRT-SKL-005 raised when required arg missing
TC-SKL-011 — log_usage() writes to skill_usage_log
"""
import uuid

import pytest

from src.db.schema import create_all
from src.skills.library import SkillLibrary, log_usage
from src.envelopes.validator import validate_outbound

SKILLS_PATH = "data/skills.yaml"


@pytest.fixture
def lib() -> SkillLibrary:
    sl = SkillLibrary()
    sl.load(SKILLS_PATH)
    return sl


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "skills_test.db")
    await create_all(path)
    return path


def test_load_populates_skills(lib):
    """TC-SKL-001 — load() populates at least 10 skills."""
    assert len(lib._skills) >= 10


def test_get_by_id(lib):
    """TC-SKL-002 — get() returns the requested skill."""
    skill = lib.get("classify_into_buckets")
    assert skill["skill_id"] == "classify_into_buckets"
    assert skill["version"] == 1


def test_get_latest_version(lib):
    """TC-SKL-003 — get() without version returns highest version."""
    # Add a fake v2 to test
    lib._skills.append({
        "skill_id": "classify_into_buckets",
        "version": 2,
        "description": "v2 test",
        "envelope_type": "apply_to_each",
        "required_args": {},
        "outbound_template": {"instruction": "test", "output_schema": {}},
    })
    skill = lib.get("classify_into_buckets")
    assert skill["version"] == 2
    # Cleanup
    lib._skills = [s for s in lib._skills if not (s["skill_id"] == "classify_into_buckets" and s["version"] == 2)]


def test_get_unknown_skill_raises(lib):
    """TC-SKL-004 — BRT-SKL-003 raised for unknown skill_id."""
    with pytest.raises(RuntimeError, match="BRT-SKL-003"):
        lib.get("nonexistent_skill")


def test_get_wrong_version_raises(lib):
    """TC-SKL-005 — BRT-SKL-004 raised for unavailable version."""
    with pytest.raises(RuntimeError, match="BRT-SKL-004"):
        lib.get("classify_into_buckets", version=999)


def test_missing_file_raises():
    """TC-SKL-006 — BRT-SKL-001 raised for missing file."""
    sl = SkillLibrary()
    with pytest.raises(RuntimeError, match="BRT-SKL-001"):
        sl.load("data/nonexistent_skills.yaml")


def test_catalogue_includes_all_skill_ids(lib):
    """TC-SKL-007 — catalogue_for_prompt() mentions every skill."""
    catalogue = lib.catalogue_for_prompt()
    for skill in lib._skills:
        assert skill["skill_id"] in catalogue


def test_render_classify_into_buckets(lib):
    """TC-SKL-008 — render() returns a WrappedTask for classify_into_buckets."""
    run_id = uuid.uuid4()
    task = lib.render(
        skill_id="classify_into_buckets",
        version=1,
        args={
            "buckets": ["bug", "feature", "question"],
            "items_ref": "run:issues",
            "concurrency_limit": 10,
        },
        run_id=run_id,
    )
    assert task.type == "apply_to_each"
    assert task.skill_id == "classify_into_buckets"
    assert task.skill_version == 1
    assert task.run_id == run_id
    assert "bug" in task.instruction or "enum" in str(task.output_schema)


def test_rendered_task_passes_validate_outbound(lib):
    """TC-SKL-009 — rendered WrappedTask passes validate_outbound."""
    task = lib.render(
        skill_id="classify_into_buckets",
        version=1,
        args={
            "buckets": ["a", "b", "c"],
            "items_ref": "run:items",
            "concurrency_limit": 5,
        },
    )
    validate_outbound(task)  # must not raise


def test_render_missing_required_arg_raises(lib):
    """TC-SKL-010 — BRT-SKL-005 raised when a required arg is missing."""
    with pytest.raises(RuntimeError, match="BRT-SKL-005"):
        lib.render(
            skill_id="classify_into_buckets",
            version=1,
            args={"buckets": ["a", "b"]},  # missing items_ref and concurrency_limit
        )


@pytest.mark.asyncio
async def test_log_usage_writes_to_db(db):
    """TC-SKL-011 — log_usage writes a row to skill_usage_log."""
    import aiosqlite
    run_id = uuid.uuid4()
    env_id = uuid.uuid4()
    await log_usage(
        skill_id="classify_into_buckets",
        skill_version=1,
        run_id=run_id,
        envelope_id=env_id,
        outcome="success",
        tokens_used=200,
        latency_ms=450,
        composer_accepted=True,
        child_residue_count=0,
        db_path=db,
    )
    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT skill_id, outcome, tokens_used FROM skill_usage_log"
        ) as cur:
            rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "classify_into_buckets"
    assert rows[0][1] == "success"
    assert rows[0][2] == 200
