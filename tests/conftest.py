"""Session-wide test fixtures for BORG_ROUTE.

``_isolated_data`` gives every test a temp database (schema created) and a temp
copy of the capability matrix, so no test writes ``data/borg_route.db`` or
``data/capability_matrix.yaml``. ``_restore_shared_matrix`` keeps rows ticked in
``main.matrix`` from leaking between tests. ``_admin_lock_test_defaults`` keeps
the admin lock out of unrelated tests.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

_MATRIX_PATH = Path("data/capability_matrix.yaml")
_REPO_DB = "data/borg_route.db"


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path_factory):
    """Give every test its own database and its own copy of the matrix.

    No test may write ``data/borg_route.db`` or ``data/capability_matrix.yaml``.
    The temp database has the schema already (``create_all``). The repo database
    path is redirected three ways, because modules bind it at import and as
    default arguments: every module-level ``DB_PATH`` equal to the repo path is
    repointed, and ``aiosqlite.connect`` / ``sqlite3.connect`` map the repo path
    (relative or absolute) to the temp file. The matrix default path of
    ``CapabilityMatrix`` and ``main.matrix`` point at a byte copy of the YAML.
    Tests that set their own paths still work: their changes sit on top of this
    one and are undone first. Everything is restored after the test.
    """
    import aiosqlite
    from src.db import schema
    from src.registry.capability_matrix import CapabilityMatrix

    iso = tmp_path_factory.mktemp("iso")
    tmp_db = str(iso / "borg_route.db")
    tmp_matrix = str(iso / "capability_matrix.yaml")
    if _MATRIX_PATH.exists():
        shutil.copyfile(_MATRIX_PATH, tmp_matrix)

    real_sqlite_connect = sqlite3.connect
    conn = real_sqlite_connect(tmp_db)
    try:
        conn.executescript(schema._SCHEMA_SQL)
        for migration in schema._MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.Error:
                pass  # already applied
        conn.commit()
    finally:
        conn.close()

    repo_db = os.path.normcase(os.path.abspath(_REPO_DB))

    def _redirect(database):
        try:
            if os.path.normcase(os.path.abspath(os.fspath(database))) == repo_db:
                return tmp_db
        except TypeError:
            pass
        return database

    real_aio_connect = aiosqlite.connect

    mp = pytest.MonkeyPatch()
    mp.setattr(aiosqlite, "connect", lambda database, *a, **k: real_aio_connect(_redirect(database), *a, **k))
    mp.setattr(sqlite3, "connect", lambda database, *a, **k: real_sqlite_connect(_redirect(database), *a, **k))
    for name, mod in list(sys.modules.items()):
        if mod is not None and (name == "main" or name.startswith("src.")) and getattr(mod, "DB_PATH", None) == "data/borg_route.db":
            mp.setattr(mod, "DB_PATH", tmp_db)
    mp.setattr(CapabilityMatrix.__init__, "__defaults__", (tmp_matrix,))
    shared = getattr(sys.modules.get("main"), "matrix", None)
    if shared is not None:
        mp.setattr(shared, "_path", Path(tmp_matrix))
    try:
        yield
    finally:
        mp.undo()


@pytest.fixture(autouse=True)
def _restore_shared_matrix():
    """The engine routes on ``main.matrix`` (R2), so a test that ticks rows or
    inserts a test row there must not leak into later tests: snapshot the
    rows before each test and put them back afterwards."""
    import sys
    import copy

    main_mod = sys.modules.get("main")
    shared = getattr(main_mod, "matrix", None)
    snapshot = copy.deepcopy(shared._models) if shared is not None else None
    try:
        yield
    finally:
        # ``main`` may have been imported during the test itself.
        shared = getattr(sys.modules.get("main"), "matrix", None)
        if snapshot is not None and shared is not None:
            shared._models = snapshot


@pytest.fixture(autouse=True)
def _admin_lock_test_defaults():
    """Keep the admin lock out of unrelated tests (work order 4.4).

    Starlette's TestClient reports its client as the host name ``testclient``,
    which is not a loopback address. Treat it as loopback for the whole suite,
    and make sure no BORG_ADMIN_TOKEN from a developer's ``.env`` leaks in.
    The auth tests use other client addresses (e.g. 10.0.0.5), which stay
    refused. This lives in the test suite only, never in production code.
    It uses its own MonkeyPatch (not the ``monkeypatch`` fixture) so it never
    shares a test's undo stack or its chdir.
    """
    from src.api import auth

    real = auth._is_loopback
    mp = pytest.MonkeyPatch()
    mp.setattr(auth, "_is_loopback", lambda host: host == "testclient" or real(host))
    mp.delenv("BORG_ADMIN_TOKEN", raising=False)
    try:
        yield
    finally:
        mp.undo()
