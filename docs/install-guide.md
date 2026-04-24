# Install Guide — BORG_ROUTE
> Audience: Operators / IT staff / LLM installer agents
> Covers: installation, configuration, deployment, maintenance
> Cross-platform: Windows, Linux, macOS

---

## Note to an LLM Installer Agent — READ FIRST

If you are an LLM agent tasked with installing BORG_ROUTE, read this
section before executing anything. Following it will save you 15-30
minutes of debugging what would otherwise look like mysterious failures.

### Platform detection
Detect the platform before running any shell command:
- **Windows (PowerShell / cmd):** use `copy`, `;` to chain, backslash paths optional but relative paths preferred.
- **Linux / macOS / Git Bash on Windows:** use `cp`, `&&` to chain, forward slashes.
- A safe cross-platform alternative to `copy` / `cp` is `python -c "import shutil; shutil.copy('.env.example', '.env')"`.

### Known pitfalls (in order of frequency)
1. **Running tests before starting LM Studio/Ollama.** Tests pass, but then `POST /models/discover` returns 0 local models. This is expected — tests do not probe live providers. Start your local model server *before* calling discover.
2. **Calling `POST /models/research` before `POST /models/discover`.** Scorer runs against the matrix, which is empty on a fresh install. Order is: (1) start local model servers, (2) discover, (3) research.
3. **Forgetting `OPENROUTER_API_KEY`.** Without it, OpenRouter discovery AND the scorer (default `perplexity/sonar-pro-search`, routed via OpenRouter) both fail. Set the key in `.env` before discover/research.
4. **Using `copy` on Linux or `cp` in raw PowerShell.** See "Platform detection" above; prefer the Python one-liner.
5. **Assuming every row in the matrix has real scores.** After discover, all 19 rows have `task_scores` at a flat 5.0 until `POST /models/research` is run. The router will either use that flat score or fall back with `BRT-ORC-007-DEGRADED`. This is normal; running research is what fills it in.
6. **Skipping the checkbox step.** Every row defaults to `checked: false`. Until the operator (or you, via `PATCH /models/{id}/checked`) ticks at least one model, the router returns `BRT-REG-020 no model found`.
7. **Trying to benchmark.** There is no `POST /models/benchmark` any more. It was replaced by `POST /models/research`. If you see stale docs mentioning benchmark, they are wrong.

### Skip spaCy for now
Slice 5 (NER worker) is parked by default. The previous install step
`python -m spacy download en_core_web_sm` is **not required** for a
functioning system today. Skip it. Only run it if the operator
specifically asks for NER. (The `spacy` library itself is still a
dependency because tests import it, but the language model download
is optional.)

**Test-count math (BRT-QA-01):** on a fresh install without
`en_core_web_sm`, the three NER worker tests skip automatically
(TC-S5-010 / 011 / 012). You should see `253 passed, 3 skipped,
0 failed` (i.e. 256 tests collected, 3 skipped). Installing the
language model flips those three from skip to pass, giving the
full `256 passed` result.

### Success criteria
The install is done when all of these pass:
- `python -m pytest` → exits 0 with 256 passing
- `GET http://localhost:8000/health` → `{"status":"ok"}`
- `GET http://localhost:8000/models/matrix` → returns 19 rows (7 LM Studio + 12 OpenRouter) with `checked: false` defaults
- At least one row has been ticked with `PATCH /models/<id>/checked` (use `d-jai/test-model` from LM Studio, or any one you plan to use)

---

## Prerequisites
- Python **3.11 or newer** (3.11 / 3.12 / 3.13 all tested).
- `pip` (bundled with Python).
- Optional: **LM Studio** (Windows / macOS / Linux builds — all use ports 1234-1237).
- Optional: **Ollama** (Linux-native, macOS + Windows builds exist — port 11434).
- Optional: **OpenRouter API key** for external models AND for the capability scorer.

## Quick Start (Local Development)

### Windows (PowerShell)
```powershell
cd C:\Stage\BORG_ROUTE                      # or wherever you extracted it

copy .env.example .env
# Edit .env — add OPENROUTER_API_KEY (and any LM Studio paths)

python -m pip install -e ".[dev]"
python -m pytest                            # expect 256 passing

uvicorn main:app --reload
# Server at http://localhost:8000
```

### Linux / macOS / Git Bash
```bash
cd /path/to/BORG_ROUTE                      # wherever you cloned it

cp .env.example .env
# Edit .env — add OPENROUTER_API_KEY (and any LM Studio paths)

python3 -m pip install -e ".[dev]"
python3 -m pytest                           # expect 256 passing

uvicorn main:app --reload
# Server at http://localhost:8000
```

### Cross-platform (works everywhere including raw PowerShell)
```bash
python -c "import shutil; shutil.copy('.env.example', '.env')"
python -m pip install -e ".[dev]"
python -m pytest
python -m uvicorn main:app --reload
```

### Verify
`GET http://localhost:8000/health` → `{"status":"ok"}`. The system is up.

---

## First-Run Checklist (after `uvicorn` is running)

1. **Start your local model server** (LM Studio or Ollama) if you want local models.
2. **Open the UI:** `http://localhost:8000/ui/models.html`
3. **Click Discover Models.** Expect 7 LM Studio rows (if LM Studio is running) + 12 curated OpenRouter rows.
4. **Click Get Model Info.** Scorer runs for 30-90 seconds; fills relative 0-10 task scores per row. Requires `OPENROUTER_API_KEY`.
5. **Tick the checkbox** on any row you want in today's routing pool (typical: 3-6 total).
6. **Kick a test run** from `http://localhost:8000/ui/index.html` — the orchestrator will route to one of the checked models.

## Production Deployment

### Linux (systemd + reverse proxy — recommended)
```bash
# Run as non-root user
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```
Put it behind nginx or caddy for TLS. The `aiosqlite` DB at
`data/borg_route.db` is a single file — back it up with any file
backup tool.

### Windows
Run under NSSM or a Windows Service wrapper of your choice. Same
`uvicorn` command, same single-DB backup story.

### Notes on workers
`--workers > 1` is **not supported** today because the orchestrator keeps
an in-process run registry and SQLite DB. For multi-worker scale, wait
for the Postgres migration slice. Use vertical scale (faster CPU) until
then.

## Environment Variables
See `.env.example` — every variable is documented inline. Key ones:
- `OPENROUTER_API_KEY` — required for OpenRouter discovery + scorer.
- `SCORING_MODEL` (default `perplexity/sonar-pro-search`) — must support real web search.
- `SCORING_MODEL_ENDPOINT` (default OpenRouter) — override if you prefer a direct provider.

## Backup & Restore
Back up `data/borg_route.db` (the SQLite DB) and `data/capability_matrix.yaml`
(scores + checkboxes). That is the full state. No other files carry
user-specific configuration.

## Troubleshooting
See `docs/errors/error-registry.md` for the full error-code reference.
Most common on a fresh install:
- `BRT-REG-020` — no row checked. Tick at least one on the Models UI.
- `BRT-REG-030` — scorer endpoint unreachable. Check `OPENROUTER_API_KEY` and network.
- `BRT-REG-032` — `data/curated_models.yaml` missing. Restore from source tree.
- `BRT-ORC-007-DEGRADED` — warning only; quality bar fell back to 5.1. Run Get Model Info to refresh scores.
