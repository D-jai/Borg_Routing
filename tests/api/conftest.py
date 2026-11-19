"""Keep API tests off the operator's database.

A direct-to-model request now logs its call; point that at a temp file so no
test writes to ``data/borg_route.db``.
"""
import pytest


@pytest.fixture(autouse=True)
def _bypass_log_in_temp_db(tmp_path, monkeypatch):
    from src.api import chat_completions

    monkeypatch.setattr(chat_completions, "DB_PATH", str(tmp_path / "bypass_log.db"))
