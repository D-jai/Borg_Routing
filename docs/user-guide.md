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

---

<!-- Added: 2026-04-18 — Slice 3: Recursion + Admin + Resume + Threads -->
## Recursion and Depth Control

BORG_ROUTE can orchestrate sub-problems by spawning **child runs**. Each child run has its own run ID, linked to its parent via `parent_run_id`. The depth of nesting is controlled by **Max Recursion Depth** (default: 5).

### What happens at the depth cap
When an orchestrate node is dispatched at the maximum depth, BORG_ROUTE **demotes it to an answerer** — the model responds directly rather than trying to recurse further. This is logged as a `BRT-ORC-020` event in the run trace.

### How to adjust the depth limit
1. Open `http://localhost:8000/ui/admin.html`.
2. In the **Settings** section, adjust **Max Recursion Depth** (1–10).
3. Click **Save Settings**.

---

## Admin Panel

The Admin panel at `http://localhost:8000/ui/admin.html` lets you configure BORG_ROUTE's behaviour without restarting the server.

### Global Rules
Text entered here is injected verbatim into every model call's governance layer. Use it to add blanket constraints — for example, "Always respond in English" or "Never reveal pricing information."

### Admin Rules Ledger
Additive rules that accumulate over time. New rules are appended; old rules can be retired (they are excluded from future model calls but kept in the ledger for audit purposes).

### Settings
- **Max Recursion Depth** — Maximum levels of nested orchestration (1–10, default: 5).
- **Default Strategy** — `SYSTEM_CHOOSE` lets BORG_ROUTE pick the best available model; `LOCAL_FIRST` prefers local models and only falls back to external if no local model qualifies.
- **Context Window Size** — How many recent conversation turns to include in the orchestrator's context (default: 10).
- **Time Cost Per Second** — Dollar value assigned to one second of local-model compute time, used in cost estimation (default: $0.0083 ≈ $30/hr).

---

## Resuming a Failed Run

If a run fails partway through (network error, model timeout, etc.), you can resume it without re-running the work that already completed.

### How to resume
1. Open `http://localhost:8000/ui/dashboard.html`.
2. Find the failed run in the run history table.
3. Click **Resume** — only failed or in-progress runs show this button.
4. The system re-dispatches only the failed/in-flight envelopes. Completed work is not repeated.

### Via API
```
POST /runs/{run_id}/resume
```
Returns 202 immediately. Poll `GET /runs/{run_id}` for updated status.

### If something goes wrong
- **"BRT-DB-020"** — The run is already completed and cannot be resumed.
- **"no_pending_work"** — All envelopes already succeeded; nothing to retry.

---

## Multi-turn Conversations (Threads)

BORG_ROUTE tracks conversation history in **threads**. Each thread stores the sequence of user and assistant messages. When a new run is created within a thread, the last N messages are passed to the orchestrator as context, enabling coherent multi-turn conversations.

### How threads work
1. A thread is created automatically when you start a conversation via the Chat UI.
2. Each user message and assistant response is stored in the thread.
3. The orchestrator receives the last **Context Window Size** turns as `thread_window`.
4. Run IDs are linked to their thread via the `thread_id` field on the run record.

---

## Dashboard

The Dashboard at `http://localhost:8000/ui/dashboard.html` shows all runs with their status, parent link, and creation time. Click **Trace** to open the full run trace for any run.
