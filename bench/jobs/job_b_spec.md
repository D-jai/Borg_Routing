# Spec: Notes service (all Python)

Build a small notes service. Everything is Python, SQL or YAML so every file can be checked by a machine.

## Behaviour

- A note has an integer `id`, a `title` (1 to 120 characters, required), a `body` (text, may be empty), a `created_at` (ISO 8601 UTC string set by the service) and zero or more tags.
- A tag is a lowercase word of 1 to 30 letters or digits. Tags are stored once and linked to notes (many to many).
- Endpoints (JSON in and out):
  - `POST /notes` creates a note from `{"title": ..., "body": ..., "tags": [...]}`; returns 201 and the note.
  - `GET /notes/{id}` returns the note with its tags; 404 if it does not exist.
  - `GET /notes?tag=<tag>` lists the notes carrying that tag (all notes if `tag` is absent), newest first.
  - `DELETE /notes/{id}` removes the note and its tag links; returns 204; 404 if it does not exist.
- A title that is empty or longer than 120 characters returns 422. A tag that breaks the tag rule returns 422.

## Files to produce

1. `schema.sql`: SQLite schema with tables `notes`, `tags` and `note_tags`, with primary keys, foreign keys and a unique constraint on tag names.
2. `app.py`: a FastAPI application object named `app` that implements the endpoints with the standard library `sqlite3` module. The database path comes from the environment variable `NOTES_DB` (default `notes.db`); `app.py` creates the tables from the contents of `schema.sql` on start-up.
3. `openapi.yaml`: an OpenAPI 3 description (`openapi` and `paths` keys) of the four endpoints.
4. `test_app.py`: pytest tests using `fastapi.testclient.TestClient` and a temporary database, covering create, get, list by tag, delete, 404 and 422.

## Answer format

Give each file as a heading line `=== FILE: <name> ===` followed by a fenced code block with the full file, in this order: `schema.sql`, `app.py`, `openapi.yaml`, `test_app.py`. No other text is needed.
