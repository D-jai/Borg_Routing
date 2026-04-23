# Presentation Deck Source (generated)

> Generated from `.dev/ledger/` at 2026-04-23 04:33 UTC.
> One slide per demo-worthy feature. Copy into your slide tool of choice.

## Slide 1: Model discovery across LM Studio, Ollama, OpenRouter, and custom endpoints

**Hero bullet:** Point BORG_ROUTE at your machine — it finds every model by itself.

*Angle:* Zero config. One button. The lineup is discovered, not declared.

**Live demo script:**
- Open /ui/models.html on an empty install.
- Start LM Studio with one model loaded.
- Click "Discover Models".
- Show the table populate with real pricing, context length, and throughput columns.

## Slide 2: Live SSE run stream — watch a run unfold in real time

**Hero bullet:** Watch a run unfold live — no polling, no refresh, no page reload.

*Angle:* Observability that feels alive — not a log file, a dashboard.

**Live demo script:**
- Kick off a batch run that uses apply_to_each over ~20 items.
- Open the run trace page before the run finishes.
- Point at the green pulsing "Live" dot.
- Point at the apply_to_each counter climbing in real time.
- Wait for completion; dot flips to green "Completed".

## Slide 3: Replace benchmark subsystem with LLM-research scorer + checkbox curation

**Hero bullet:** One click, one LLM — every model in the pool gets a relative capability rating grounded in published benchmarks.

*Angle:* Stop guessing at model quality — let a search-aware LLM do the research, then you pick the pool.

**Live demo script:**
- {'Open the Models page. Point at the 7 LM Studio rows + 12 OpenRouter rows. Explain': 'curated pool, not discovery flood.'}
- Point at the "not researched" badges on a fresh row. Click "Get Model Info".
- Wait ~60s; scores fill in across all rows simultaneously with relative rankings.
- Tick 4-5 checkboxes to pick a pool for the day's work.
- Kick a run from the Chat page. The orchestrator now picks from just those 4-5 models; cost is predictable.
