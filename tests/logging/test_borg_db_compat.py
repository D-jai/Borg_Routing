"""
BRT-LOG-DB-COMPAT tests — verify BORG_ROUTE's log schema is conducive to
The_Borg_DB ingestion.

TC-DBC-001: provenance singletons resolve to stable values
TC-DBC-002: write_event stamps instance_id / source_version / domain_profile
TC-DBC-003: create_run stamps instance_id on the runs row
TC-DBC-004: borg_db_export_cursor table + view exist after schema init
TC-DBC-005: borg_db_interactions view returns the expected join shape
TC-DBC-006: env-var overrides on instance_id and source_version are honoured
"""
from __future__ import annotations

import asyncio
import os

import aiosqlite
import pytest

from src.db.schema import create_all
from src.db.state import create_run
from src.logging import provenance
from src.logging.writer import write_event


# ---------- TC-DBC-001 — provenance singletons ----------

def test_resolve_instance_id_is_stable_across_calls(monkeypatch):
    monkeypatch.delenv("BORG_INSTANCE_ID", raising=False)
    provenance._reset_for_tests()
    a = provenance.resolve_instance_id()
    b = provenance.resolve_instance_id()
    assert a == b
    assert a.startswith("borg-route:")
    assert len(a) > len("borg-route:")


def test_resolve_source_version_returns_string(monkeypatch):
    monkeypatch.delenv("BORG_SOURCE_VERSION", raising=False)
    provenance._reset_for_tests()
    sv = provenance.resolve_source_version()
    # Either git:abc1234 or 'unknown' — we don't care which, just that it's truthy.
    assert isinstance(sv, str)
    assert sv  # not empty


def test_resolve_domain_profile_defaults_to_general(monkeypatch):
    monkeypatch.delenv("BORG_DOMAIN_PROFILE", raising=False)
    provenance._reset_for_tests()
    assert provenance.resolve_domain_profile() == "general"


# ---------- TC-DBC-006 — env var overrides ----------

def test_env_var_overrides_for_provenance(monkeypatch):
    monkeypatch.setenv("BORG_INSTANCE_ID", "test-instance-42")
    monkeypatch.setenv("BORG_SOURCE_VERSION", "v9.9.9")
    monkeypatch.setenv("BORG_DOMAIN_PROFILE", "healthcare")
    provenance._reset_for_tests()
    assert provenance.resolve_instance_id() == "test-instance-42"
    assert provenance.resolve_source_version() == "v9.9.9"
    assert provenance.resolve_domain_profile() == "healthcare"


# ---------- TC-DBC-002 — write_event stamps provenance fields ----------

@pytest.mark.asyncio
async def test_write_event_stamps_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("BORG_INSTANCE_ID", "compat-test-instance")
    monkeypatch.setenv("BORG_SOURCE_VERSION", "compat-test-version")
    monkeypatch.setenv("BORG_DOMAIN_PROFILE", "engineering")
    provenance._reset_for_tests()

    db = str(tmp_path / "compat.db")
    await create_all(db)

    run_id = await create_run("test instruction", db_path=db)

    await write_event(
        "model_call",
        run_id=run_id,
        model_id="test/model",
        decision={"code": "TEST"},
        db_path=db,
    )
    # write_event is fire-and-forget; let the task run.
    await asyncio.sleep(0.1)

    async with aiosqlite.connect(db) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT instance_id, source_version, domain_profile FROM logs WHERE run_id=?",
            (str(run_id),),
        ) as cur:
            row = await cur.fetchone()

    assert row is not None
    assert row["instance_id"] == "compat-test-instance"
    assert row["source_version"] == "compat-test-version"
    assert row["domain_profile"] == "engineering"


# ---------- TC-DBC-003 — create_run stamps instance_id ----------

@pytest.mark.asyncio
async def test_create_run_stamps_instance_id(tmp_path, monkeypatch):
    monkeypatch.setenv("BORG_INSTANCE_ID", "create-run-instance")
    provenance._reset_for_tests()

    db = str(tmp_path / "compat.db")
    await create_all(db)

    run_id = await create_run("hello", db_path=db)

    async with aiosqlite.connect(db) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT instance_id FROM runs WHERE run_id=?", (str(run_id),)
        ) as cur:
            row = await cur.fetchone()

    assert row["instance_id"] == "create-run-instance"


# ---------- TC-DBC-004 — cursor table + view exist after schema init ----------

@pytest.mark.asyncio
async def test_export_cursor_table_and_view_exist(tmp_path):
    db = str(tmp_path / "compat.db")
    await create_all(db)

    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE name IN ('borg_db_export_cursor','borg_db_interactions') "
            "ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()

    found = {(name, kind) for name, kind in rows}
    assert ("borg_db_export_cursor", "table") in found
    assert ("borg_db_interactions", "view") in found


# ---------- TC-DBC-005 — view returns expected shape ----------

@pytest.mark.asyncio
async def test_borg_db_interactions_view_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("BORG_INSTANCE_ID", "view-test-instance")
    monkeypatch.setenv("BORG_DOMAIN_PROFILE", "legal")
    provenance._reset_for_tests()

    db = str(tmp_path / "compat.db")
    await create_all(db)

    run_id = await create_run("draft a brief", db_path=db)
    await write_event(
        "orchestrator_decision",
        run_id=run_id,
        model_id="orch/test",
        decision={"code": "TEST", "msg": "hello"},
        payload_in="prompt-in",
        payload_out="prompt-out",
        db_path=db,
    )
    await asyncio.sleep(0.1)

    async with aiosqlite.connect(db) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM borg_db_interactions WHERE run_id=?",
            (str(run_id),),
        ) as cur:
            row = await cur.fetchone()

    assert row is not None
    keys = set(row.keys())
    # Spot-check the columns DB will need
    for required in (
        "log_id", "run_id", "event_type", "model_id", "decision_json",
        "instance_id", "source_version", "domain_profile",
        "user_instruction", "run_status", "payload_in", "payload_out",
    ):
        assert required in keys, f"view missing column {required}"

    assert row["instance_id"] == "view-test-instance"
    assert row["domain_profile"] == "legal"
    assert row["user_instruction"] == "draft a brief"
    assert row["payload_in"] == "prompt-in"
    assert row["payload_out"] == "prompt-out"


# ---------- TC-DBC-007 — log_entities + log_relations carry provenance fields ----------

@pytest.mark.asyncio
async def test_log_entities_relations_have_provenance_columns(tmp_path):
    """Schema must surface confidence + provenance + instance_id columns
    on log_entities and log_relations even before the NER worker fills
    them — so The_Borg_DB queries don't break on missing columns."""
    db = str(tmp_path / "compat.db")
    await create_all(db)

    async with aiosqlite.connect(db) as conn:
        async with conn.execute("PRAGMA table_info(log_entities)") as cur:
            ent_cols = {r[1] for r in await cur.fetchall()}
        async with conn.execute("PRAGMA table_info(log_relations)") as cur:
            rel_cols = {r[1] for r in await cur.fetchall()}

    for col in ("instance_id", "confidence", "provenance"):
        assert col in ent_cols, f"log_entities missing {col}"
        assert col in rel_cols, f"log_relations missing {col}"


# Cleanup: ensure the tests don't pollute the singleton cache for
# subsequent test files.
@pytest.fixture(autouse=True)
def _reset_provenance():
    yield
    provenance._reset_for_tests()
    for var in ("BORG_INSTANCE_ID", "BORG_SOURCE_VERSION", "BORG_DOMAIN_PROFILE"):
        os.environ.pop(var, None)
