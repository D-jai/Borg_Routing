"""Job B reference tests: our own checks of the generated notes service, written from bench/jobs/job_b_spec.md.

They run ONLY inside ``docker run --network none`` (bench/checks.py), against the
generated ``app.py`` mounted read-only at /work (on PYTHONPATH, working folder).
Each test is one machine check. They test behaviour the spec states, nothing more:
response bodies may carry extra fields, and a list may come back bare or wrapped
in an object.
"""
from __future__ import annotations

import importlib
import sys
import time
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_DB", str(tmp_path / f"notes_{uuid.uuid4().hex[:8]}.db"))
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    with TestClient(module.app) as c:
        yield c
    sys.modules.pop("app", None)


def _note(c, title="First", body="text", tags=("alpha",)):
    r = c.post("/notes", json={"title": title, "body": body, "tags": list(tags)})
    assert r.status_code == 201, r.text
    return r.json()


def _items(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list):
                return v
    raise AssertionError(f"no list of notes in {payload!r}")


def test_create_returns_201_and_the_note(client):
    n = _note(client, "Shopping", "milk", ("home", "todo"))
    assert isinstance(n.get("id"), int)
    assert n["title"] == "Shopping" and n["body"] == "milk"
    assert sorted(n["tags"]) == ["home", "todo"]
    assert isinstance(n.get("created_at"), str) and n["created_at"][:2] in ("19", "20")


def test_get_returns_the_note_with_its_tags(client):
    n = _note(client, "Read", "a book", ("books",))
    r = client.get(f"/notes/{n['id']}")
    assert r.status_code == 200
    got = r.json()
    assert got["id"] == n["id"] and got["title"] == "Read" and got["tags"] == ["books"]


def test_get_missing_note_is_404(client):
    assert client.get("/notes/999999").status_code == 404


def test_list_by_tag_returns_only_tagged_notes(client):
    a = _note(client, "A", "", ("work",))
    b = _note(client, "B", "", ("home",))
    c = _note(client, "C", "", ("work", "home"))
    r = client.get("/notes", params={"tag": "work"})
    assert r.status_code == 200
    ids = {n["id"] for n in _items(r.json())}
    assert ids == {a["id"], c["id"]} and b["id"] not in ids


def test_list_without_tag_returns_all_newest_first(client):
    first = _note(client, "Older", "", ())
    time.sleep(1.1)  # distinct timestamps even at one-second resolution
    second = _note(client, "Newer", "", ())
    r = client.get("/notes")
    assert r.status_code == 200
    ids = [n["id"] for n in _items(r.json())]
    assert set(ids) == {first["id"], second["id"]}
    assert ids.index(second["id"]) < ids.index(first["id"])


def test_a_tag_shared_by_two_notes_is_stored_once_and_lists_both(client):
    a = _note(client, "One", "", ("shared",))
    b = _note(client, "Two", "", ("shared",))
    ids = {n["id"] for n in _items(client.get("/notes", params={"tag": "shared"}).json())}
    assert ids == {a["id"], b["id"]}


def test_delete_returns_204_then_the_note_is_gone(client):
    n = _note(client, "Temp", "", ("gone",))
    r = client.delete(f"/notes/{n['id']}")
    assert r.status_code == 204
    assert client.get(f"/notes/{n['id']}").status_code == 404
    assert _items(client.get("/notes", params={"tag": "gone"}).json()) == []


def test_delete_missing_note_is_404(client):
    assert client.delete("/notes/999999").status_code == 404


def test_empty_title_is_422(client):
    assert client.post("/notes", json={"title": "", "body": "", "tags": []}).status_code == 422


def test_title_over_120_characters_is_422(client):
    assert client.post("/notes", json={"title": "x" * 121, "body": "", "tags": []}).status_code == 422
    assert client.post("/notes", json={"title": "x" * 120, "body": "", "tags": []}).status_code == 201


def test_bad_tag_is_422(client):
    for bad in ("Has Space", "UPPER", "x" * 31, "bad!"):
        r = client.post("/notes", json={"title": "ok", "body": "", "tags": [bad]})
        assert r.status_code == 422, (bad, r.status_code)
