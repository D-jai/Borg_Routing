# User Guide — BORG_ROUTE
> Audience: Non-technical end users
> Updated with every feature — see Parallel Commit Law in CODING_STANDARD_CORE.md

---

<!-- Added: 2026-04-17 — Phase 0: Project Setup -->
## Getting Started

BORG_ROUTE is a cost-aware AI orchestration system that routes tasks to the
cheapest capable model. Point any OpenAI-compatible tool at it and it handles
the rest.

---

<!-- Added: 2026-04-18 — S1-00: Project scaffold + health endpoint -->
## Installation

### How to use it
1. Install Python 3.11 or newer from python.org.
2. Clone or unzip the project, then open a terminal in the project folder.
3. Run `python -m pip install -e ".[dev]"` to install all dependencies.
4. Copy `.env.example` to `.env` — fill in any API keys you have (you can add them later via the Keys page in the UI).
5. Download the spaCy language model: `python -m spacy download en_core_web_sm`
6. Start the server: `uvicorn main:app --reload`
7. Open `http://localhost:8000/health` in your browser — you should see `{"status":"ok"}`. The system is running.

### If something goes wrong
- **"Module not found" on startup** — Make sure you ran `pip install -e ".[dev]"` from the project root folder.
- **Port 8000 already in use** — Stop the other process or start BORG_ROUTE on a different port: `uvicorn main:app --reload --port 8001`

---

<!-- Added: 2026-04-18 — S1-09: Models UI + Keys UI -->
## Adding API Keys

To use external AI models (like those on OpenRouter), you need to add an API key.

### How to use it
1. Open `http://localhost:8000/ui/keys.html` in your browser.
2. For OpenRouter: enter your key in the OpenRouter row (get one free at openrouter.ai).
3. Click **Save**. The key is stored in your local `.env` file — it never leaves your machine.
4. Go to the **Models** page and click **Discover Models** to load the new provider's models.

### If something goes wrong
- **"All fields are required"** — fill in the Provider Name, Base URL, and API Key before saving.
- **Key not taking effect** — restart the server after saving a new key.

---

## Discovering Models

BORG_ROUTE can automatically find models running in LM Studio, Ollama, and on OpenRouter.

### How to use it
1. Start your local model server (LM Studio or Ollama) if you want local models.
2. Open `http://localhost:8000/ui/models.html`.
3. Click **Discover Models** at the top.
4. The table will refresh with all discovered models, including their context length, tier, estimated cost, and throughput.
5. Click **Benchmark** on any model to measure its actual quality and speed.

### If something goes wrong
- **No models appear after discovery** — Check that LM Studio is running on ports 1234–1237, or that Ollama is running at `http://localhost:11434`.
- **OpenRouter models missing** — Make sure you have added an OpenRouter API key on the Keys page.

---

<!-- Added: 2026-04-18 — S2-01: Envelope schema -->
## How Tasks Are Packaged (Envelopes)

Every piece of work BORG_ROUTE sends to a model is wrapped in a **WrappedTask** envelope. When the model finishes, it returns an **InboundEnvelope**. You never interact with envelopes directly — they are internal to the system — but understanding them helps when reading run traces.

### What a WrappedTask contains
- **instruction** — the exact task the model must perform
- **output_schema** — the exact shape the model must return (this is the compression contract)
- **type** — one of: `orchestrate`, `answerer`, `apply_to_each`, `state_op`
- **level_path** — where this task sits in the run tree (e.g. `"1a"`, `"2b"`)
- **trigger_reason** — why this envelope was created (e.g. `initial_plan`, `escalation_from:env-id`)

### What an InboundEnvelope contains
- **status** — always `resolved` or `failed`. A model must always commit to an outcome.
- **output** — the model's structured answer, matching the output_schema
- **failure_reason** — if status is `failed`, a short explanation

### If something goes wrong
- **"BRT-ENV-001" in logs** — A model returned `status="unresolved"`. This should never happen; the schema rejects it. Check the model's raw output in the run trace.
- **"BRT-ENV-003" in logs** — A plan exceeded the maximum recursion depth (default: 5). The task may be too complex to decompose at this depth; check the run trace and consider restructuring the request.
