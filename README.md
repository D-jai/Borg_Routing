# BORG_ROUTE

BORG_ROUTE: Brokered Orchestration, Retrieval and Generation. Breaks a job into steps and sends each step to the cheapest model that can do it, on your machines first.

<!-- TODO (planner): add the Run-view screenshot here. docs/design/screens/ does not exist yet; the CI interface check (work order 4.6) produces the images. Do not invent an image path before then. -->

## Who it is for

Companies that want AI on their own machines. You run the server, you hold the keys, and you can keep documents on your own hardware (see "The on-premise lane").

## See it in two minutes

You need Docker with the Compose plugin. No key, no `.env` file and no model are needed.

```
docker compose up -d --build
```

Open `http://localhost:8000`. <!-- claim C27 --> The default setup is a replay demo: it plays back runs recorded earlier, and nothing is sent to any model or website. <!-- claim C28 --> Choose a recorded job on the Ask page, watch the steps appear, and read the receipt at the end. The recorded jobs use made-up data and are not benchmark results. The install guide has the details: `docs/install-guide.md`.

## Results

The two published benchmark rounds are in `bench/results/` (`2026_11_17` and `2026_11_18`). Each folder has a `summary.md`.

<!-- TODO (planner): "where it pays" wording and any cost-saving sentence go here when the planner sends them. -->

## Where it does not pay

<!-- TODO (planner): the install guide ("When BORG_ROUTE Pays Off") says a short single-shot prompt costs more and takes longer than one direct call, but no test backs that sentence, so it is not repeated here with a claim id. Send the wording or name a backing test. -->

## How it works

1. You send a job, with or without documents, through the Ask page, an OpenAI-style chat call, a task call or an MCP tool. <!-- claim C29 --> <!-- claim C30 -->
2. A planning model breaks the job into steps and says what kind of work each one is.
3. For each step the router picks the ticked model with the lowest effective cost whose score clears the step's quality bar. <!-- claim C01 --> The run records why, with the other models that also cleared the bar. <!-- claim C03 -->
4. Every model call is priced and logged, and the run total is the sum of those calls. <!-- claim C06 --> The operator's own words reach every step, item and call unchanged. <!-- claim C08 -->
5. The planning model writes the final answer from the step results, and a receipt shows steps, models, time and cost.

```
job --> planner --> step 1 --> model A (cheapest that clears the bar)
                \-> step 2 --> model B (a local model, if one clears it)
                \-> step 3 --> model C
                         \--> final answer + receipt
```

## Run it for real

1. Copy `.env.example` to `.env` and add your OpenRouter key. Set `BORG_ADMIN_TOKEN` as well if the app can be reached from another machine.
2. Start the live setup. <!-- claim C31 -->

```
docker compose -f compose.yaml -f compose.live.yaml up -d --build
```

Without Docker, the Python steps are in `docs/install-guide.md`.

3. Open `http://localhost:8000/ui/models.html`, press Discover, tick the models the router may use, press Get Model Info to score them, then ask your first question on the Ask page.

Full steps, settings and troubleshooting: `docs/install-guide.md`. How each page and setting behaves: `docs/user-guide.md`.

## The on-premise lane

The lane keeps your documents on your machines. Its exact promise, in four parts:

- In `raw_local`, raw document text goes only to models on your own machines. <!-- claim C11 -->
- In `all_local`, nothing is sent off the machine. <!-- claim C12 -->
- A step that must stay local never falls back to a cloud model. <!-- claim C13 -->
- Every request is recorded before it is sent, and a failed record blocks the send. <!-- claim C14 -->

What a local step returns can be passed on, so it is checked and withheld if it copies document text. <!-- claim C17 --> The user guide lists what the lane does not promise ("On-premise lane", "What is not promised"). Read that list before you rely on the lane.

## Limits

- A reply cut off by the token cap is retried once at double the cap, then fails. <!-- claim C22 -->
- If every planned step fails and the planning model still writes an answer, the receipt says so in plain words. Treat that answer as unchecked. <!-- claim C26 -->
- Admin pages and keys are locked by `BORG_ADMIN_TOKEN`. Without a token, only the machine itself may use them. <!-- claim C18 -->
- The benchmark rounds are published with their failures; read them before you form a view (see "Results").
- The replay demo cannot change anything: every route that changes state is refused. <!-- claim C28 -->

## Licence

MIT. See `LICENSE`.

## Contact

Dhananjay Raol, Aliud Inquisito Inc.

<!-- TODO (planner): add a contact address or web link for the README contact line. -->
