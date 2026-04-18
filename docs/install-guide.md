# Install Guide — BORG_ROUTE
> Audience: Operators / IT staff
> Covers: installation, configuration, deployment, maintenance

---

## Prerequisites
- Python 3.11 or newer
- pip (bundled with Python)
- Optional: LM Studio (ports 1234–1237) or Ollama for local models
- Optional: OpenRouter API key for external models

## Quick Start (Local Development)
```bash
# 1. Navigate to project folder
cd C:\stage\borg_route   # or wherever you extracted it

# 2. Copy and configure env
copy .env.example .env
# Edit .env — add API keys as you obtain them

# 3. Install dependencies
python -m pip install -e ".[dev]"

# 4. Download spaCy language model (required for NER in Slice 5)
python -m spacy download en_core_web_sm

# 5. Run tests
python -m pytest   # expect 2 passing

# 6. Start dev server
uvicorn main:app --reload
# Server is up at http://localhost:8000
# Verify: GET http://localhost:8000/health → {"status":"ok"}
```

## Production Deployment
BORG_ROUTE is local-first. For a persistent deployment, run
`uvicorn main:app --host 0.0.0.0 --port 8000` behind a reverse proxy (nginx, caddy).

## Environment Variables
See `.env.example` — every variable is documented inline.

## Backup & Restore
Use the zip backup procedure in `HANDOVER_TO_BUILDER.md` Step 4 until
a formal backup strategy is defined in a later slice.

## Troubleshooting
See `docs/errors/error-registry.md` for error code reference.
