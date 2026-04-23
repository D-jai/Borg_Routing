# User Guide (generated)

> Generated from `.dev/ledger/` at 2026-04-23 04:33 UTC.
> Do not hand-edit — re-run `python .dev/render_user_guide.py`.
> Only ledger entries with a `user_guide:` block appear here.

## Discovering Models

BORG_ROUTE can now auto-discover every model running on the user's machine
and on OpenRouter without any manual config. Hit one button and the
capability matrix is populated with model IDs, context lengths, and
pricing. This is the "point it at your lineup" moment — after this, the
router has something to route to.

### How to use it
1. Start your local model server (LM Studio on ports 1234-1237, or Ollama at http://localhost:11434) if you want local models in the matrix.
2. Open http://localhost:8000/ui/models.html in your browser.
3. Click "Discover Models" at the top of the page.
4. The table refreshes with every model found, including context length, tier, estimated cost, and throughput.
5. Click "Benchmark" on any row to measure that model's actual quality and speed.

### If something goes wrong
- **No models appear after clicking Discover.** — LM Studio or Ollama is not running, or is on a non-default port. Check the ports above.
- **OpenRouter models missing.** — Add an OpenRouter API key on the Keys page and re-run discovery.

### Error codes related to this feature
- `BRT-REG-001` — Discovery: provider unreachable
- `BRT-REG-003` — Discovery: OpenRouter API error
- `BRT-REG-004` — Discovery: custom endpoint misconfigured

## Live Run Trace

The Run Trace page now shows envelopes appearing and updating live as
the orchestrator dispatches them. A green pulsing dot indicates the
stream is active; an apply_to_each counter ticks upward as parallel
subtasks launch. No polling, no refresh. Closes with a "Completed" or
"Failed" badge when the run finishes.

### How to use it
1. Open http://localhost:8000/ui/run.html?id=<run_id> while a run is still in progress.
2. A green pulsing dot labelled "Live" appears in the top bar while the stream is active.
3. Envelope rows appear and update in real time as the orchestrator dispatches them.
4. The apply_to_each counter ticks upward as parallel subtasks launch.
5. When the run finishes the dot changes to "Completed" or "Failed" and the stream closes.
6. No refresh is needed — everything updates automatically.

### If something goes wrong
- **Dot stays grey / nothing updates.** — The run is already finished — live stream only fires on in-progress runs.
- **Page shows "Stream abandoned".** — The browser tab was backgrounded too long. Reload the page.

### Error codes related to this feature
- `BRT-API-028` — Stream: run_id not found
- `BRT-API-029` — Stream: connection abandoned mid-run

## Curating Your Model Pool (checkboxes) and Researching Capabilities (Get Info)

The old benchmark subsystem — which sent 5 prompts per task_type to every
model and scored outputs via a hardcoded local judge LLM — is gone.
In its place: a "Get Model Info" button that asks a web-search LLM to
research each model's capabilities using published benchmarks, then writes
relative 0-10 task scores to the matrix.

Model intake is now operator-curated. The OpenRouter discovery flood
(300+ models) is replaced by a 12-model curated list in
data/curated_models.yaml. Every row gets a checkbox in the Models UI.
Only checked rows are considered by the router — so the operator controls
the "available pool" directly.

Unresearched rows default to a flat 5.0 on every task_type. The
orchestrator softens its 8.0 quality bar to 5.1 when no model qualifies,
logging BRT-ORC-007-DEGRADED, so the system still routes during a cold
start or after a matrix wipe.

### How to use it
1. Open http://localhost:8000/ui/models.html. You see two tables — LM Studio (local, live-discovered) on top and OpenRouter (12 curated) below.
2. Every row has a checkbox in the first column. Unchecked = invisible to the router. Checked = in the pool.
3. {'Tick the models you want the orchestrator to be able to use today. Typical': '3-6 total.'}
4. Click the "Get Model Info" button. A web-search LLM (default perplexity/sonar-pro-search) researches each model's published benchmarks and writes relative 0-10 scores into the matrix. Takes 30-90 seconds.
5. The score cells fill in with per-task numbers (reasoning / coding / summarize / writing / roleplay / agentic / extraction).
6. Pricing for OpenRouter rows is pulled straight from OpenRouter's /models endpoint during Discover.
7. Changes persist to data/capability_matrix.yaml immediately; survive a restart.

### If something goes wrong
- **"Get Model Info" fails with BRT-REG-030.** — The scoring LLM endpoint is unreachable or the API key is wrong. Check SCORING_MODEL_ENDPOINT and OPENROUTER_API_KEY in .env.
- **"Get Model Info" fails with BRT-REG-031.** — The scoring LLM returned unparseable JSON. Usually means you pointed SCORING_MODEL at a model that does not actually support web search. Switch to perplexity/sonar-pro-search or gpt-4o-search-preview.
- **The router fails with BRT-REG-020 even though rows exist.** — No row is checked, or none meets the quality bar. Tick at least one row on the Models page, or run Get Model Info so rows have real scores (unchecked rows sit at a flat 5.0).
- **OpenRouter section shows 0 rows after Discover.** — data/curated_models.yaml is missing or empty (BRT-REG-032). Restore from the sample file that ships with the project.

### Error codes related to this feature
- `BRT-REG-030` — Scorer LLM call failed (HTTP / timeout)
- `BRT-REG-031` — Scorer response could not be parsed as JSON
- `BRT-REG-032` — data/curated_models.yaml missing or malformed
- `BRT-ORC-007-DEGRADED` — Quality bar relaxed from 8.0 to 5.1 because no model qualified
