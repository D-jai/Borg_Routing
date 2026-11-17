A correct minimal answer to bench/jobs/job_b_spec.md, written by hand for the tests of bench/checks.py.

=== FILE: schema.sql ===
```sql
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS note_tags (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (note_id, tag_id)
);
```

=== FILE: app.py ===
```python
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

TAG_RE = re.compile(r"^[a-z0-9]{1,30}$")
SCHEMA = Path(__file__).with_name("schema.sql")

app = FastAPI()


class NoteIn(BaseModel):
    title: str
    body: str = ""
    tags: list[str] = []


def db():
    conn = sqlite3.connect(os.environ.get("NOTES_DB", "notes.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@app.on_event("startup")
def startup():
    with db() as conn:
        conn.executescript(SCHEMA.read_text())


def note_out(conn, row):
    tags = [r["name"] for r in conn.execute(
        "SELECT t.name FROM tags t JOIN note_tags nt ON nt.tag_id = t.id WHERE nt.note_id = ? ORDER BY t.name",
        (row["id"],))]
    return {"id": row["id"], "title": row["title"], "body": row["body"], "created_at": row["created_at"], "tags": tags}


@app.post("/notes", status_code=201)
def create_note(note: NoteIn):
    if not 1 <= len(note.title) <= 120:
        raise HTTPException(422, "title must be 1 to 120 characters")
    for t in note.tags:
        if not TAG_RE.match(t):
            raise HTTPException(422, f"bad tag {t!r}")
    with db() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute("INSERT INTO notes (title, body, created_at) VALUES (?, ?, ?)", (note.title, note.body, now))
        nid = cur.lastrowid
        for t in set(note.tags):
            conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))
            tid = conn.execute("SELECT id FROM tags WHERE name = ?", (t,)).fetchone()["id"]
            conn.execute("INSERT INTO note_tags (note_id, tag_id) VALUES (?, ?)", (nid, tid))
        return note_out(conn, conn.execute("SELECT * FROM notes WHERE id = ?", (nid,)).fetchone())


@app.get("/notes/{note_id}")
def get_note(note_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "not found")
        return note_out(conn, row)


@app.get("/notes")
def list_notes(tag: str | None = None):
    with db() as conn:
        if tag is None:
            rows = conn.execute("SELECT * FROM notes ORDER BY created_at DESC, id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT n.* FROM notes n JOIN note_tags nt ON nt.note_id = n.id JOIN tags t ON t.id = nt.tag_id "
                "WHERE t.name = ? ORDER BY n.created_at DESC, n.id DESC", (tag,)).fetchall()
        return [note_out(conn, r) for r in rows]


@app.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: int):
    with db() as conn:
        if conn.execute("SELECT 1 FROM notes WHERE id = ?", (note_id,)).fetchone() is None:
            raise HTTPException(404, "not found")
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    return Response(status_code=204)
```

=== FILE: openapi.yaml ===
```yaml
openapi: 3.0.3
info:
  title: Notes service
  version: "1.0"
paths:
  /notes:
    post:
      summary: Create a note
      responses:
        "201": {description: Created}
        "422": {description: Invalid title or tag}
    get:
      summary: List notes, optionally by tag
      parameters:
        - {name: tag, in: query, required: false, schema: {type: string}}
      responses:
        "200": {description: Notes, newest first}
  /notes/{id}:
    parameters:
      - {name: id, in: path, required: true, schema: {type: integer}}
    get:
      summary: Get a note
      responses:
        "200": {description: The note}
        "404": {description: Not found}
    delete:
      summary: Delete a note
      responses:
        "204": {description: Deleted}
        "404": {description: Not found}
```

=== FILE: test_app.py ===
```python
from fastapi.testclient import TestClient


def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_DB", str(tmp_path / "t.db"))
    import importlib, sys
    sys.modules.pop("app", None)
    return TestClient(importlib.import_module("app").app)


def test_create_get_delete(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as c:
        n = c.post("/notes", json={"title": "a", "body": "", "tags": ["x"]}).json()
        assert c.get(f"/notes/{n['id']}").status_code == 200
        assert c.delete(f"/notes/{n['id']}").status_code == 204
        assert c.get(f"/notes/{n['id']}").status_code == 404
        assert c.post("/notes", json={"title": "", "body": "", "tags": []}).status_code == 422
```
