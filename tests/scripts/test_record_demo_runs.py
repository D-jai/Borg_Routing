"""Demo recorder (ruling 24): no network in --dry-run, and the demo data is shaped as promised."""
import importlib.util
import socket
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
CANARY = "CANARY-7f3a91-ZEBRA-LANTERN"


def _load():
    spec = importlib.util.spec_from_file_location("record_demo_runs", ROOT / "scripts" / "record_demo_runs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dry_run_makes_no_http_call_and_reports_three_jobs(monkeypatch, capsys):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise AssertionError("network call in --dry-run")

    for name in ("send",):
        monkeypatch.setattr(httpx.AsyncClient, name, boom)
        monkeypatch.setattr(httpx.Client, name, boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    mod = _load()
    assert mod.main(["--dry-run", "--no-probe"]) == 0
    out = capsys.readouterr().out
    assert calls == []
    assert out.count("demo run job ") == 3
    assert "no model call" in out and "sk-" not in out


def test_dry_run_survives_an_unreachable_ollama(monkeypatch, capsys):
    def refuse(*a, **k):
        raise ConnectionRefusedError

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", lambda *a, **k: pytest.fail("httpx used"))
    assert _load().main(["--dry-run", "--job", "3"]) == 0
    assert '"reachable": false' in capsys.readouterr().out


def test_demo_data_has_header_and_canary_only_in_job_3():
    files = [p for p in (ROOT / "demo" / "data").rglob("*") if p.is_file()]
    assert len(files) == 13
    for p in files:
        first = p.read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith("#") and "DEMO DATA" in first and "cited by no claim" in first, p.name
    with_canary = {p.parent.name + "/" + p.name for p in files if CANARY in p.read_text(encoding="utf-8")}
    assert len(with_canary) == 1 and all(n.startswith("job3_notes/") for n in with_canary)
    feedback = (ROOT / "demo" / "data" / "job1_feedback.txt").read_text(encoding="utf-8").splitlines()
    assert len(feedback) - 1 == 60


def test_the_answer_is_kept_beside_the_database_byte_for_byte(tmp_path):
    """Work order 4.5: the run database does not store the final answer; the export reads it here."""
    mod = _load()
    text = "Line one\r\nline two\n| a | b |\n"
    path = mod.save_answer("d04e0d8a-0000", text, folder=tmp_path / "answers")
    assert path.name == "d04e0d8a-0000.md" and path.read_bytes() == text.encode("utf-8")


def test_no_answer_file_without_a_run_or_a_text(tmp_path):
    mod = _load()
    assert mod.save_answer(None, "x", folder=tmp_path) is None
    assert mod.save_answer("r1", None, folder=tmp_path) is None
    assert list(tmp_path.iterdir()) == []
