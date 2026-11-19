# BORG_ROUTE

Breaks a job into steps and sends each step to the cheapest model that can do it, local or cloud. <!-- claim C01 --> With the private lane on (`raw_local` or `all_local`), the steps that read your documents run only on your own machines. <!-- claim C36 -->

<!-- TODO (planner): add the Run-view screenshot here. docs/design/screens/ does not exist yet; the CI interface check (work order 4.6) produces the images. Do not invent an image path before then. -->

## Who it is for

Companies that want AI on their own machines. You run the server, you hold the keys, and you can keep documents on your own hardware (see "The on-premise lane").

## See the demo

You need Docker with the Compose plugin. No key, no `.env` file and no model are needed.

```
docker compose up -d --build
```

Open `http://localhost:8000`. The default compose service sets replay mode and needs no env file. <!-- claim C27 --> In replay mode the app plays back runs recorded earlier, and no model, key or network call is made. <!-- claim C28 --> Choose a recorded job on the Ask page, watch the steps appear, and read the receipt at the end. Every page says the recorded jobs are made-up demo data and not benchmark results. <!-- claim C35 --> The install guide has the details: `docs/install-guide.md`.

## Results

The two published benchmark rounds are in `bench/results/` (`2026_11_17` and `2026_11_18`). Each folder has a `summary.md`.

<!-- TODO (planner): "where it pays" wording and any cost-saving sentence go here when the planner sends them. -->

## Where it does not pay

<!-- TODO (planner): the install guide ("When BORG_ROUTE Pays Off") says a short single-shot prompt costs more and takes longer than one direct call, but no test backs that sentence, so it is not repeated here with a claim id. Send the wording or name a backing test. -->

## How it works

1. You send a job through an OpenAI-style chat call, a task call or an MCP tool. <!-- claim C29 --> <!-- claim C33 --> <!-- claim C30 -->
2. A planning model breaks the job into steps. For each step the router picks the ticked model with the lowest effective cost whose score clears the step's floor and whose context fits. The exceptions: the planner names a ticked model, or the run uses `borg_route:local`, which prefers a local model that clears the floor. If no ticked model clears the floor, the step runs on the run's default model and the trace warns BRT-ORC-019. <!-- claim C01 --> <!-- claim C02 --> "Cheapest" means the lowest cost counting both dollars and waiting time, among the models that clear the step's quality floor. A slow local model can lose to a fast cheap cloud model unless the private lane or the local-first setting (`borg_route:local`) is on. The run records why each model was chosen, with up to eight models that cleared the floor. <!-- claim C03 -->
3. Every model call that reports its usage is priced and logged, planner calls included. <!-- claim C04 --> The run total is the sum of the logged calls. <!-- claim C06 --> The operator's own words reach every step, item and call unchanged. <!-- claim C08 -->
4. A short conversational message with no documents is usually answered in one call, without a plan (the Rule Zero shortcut). <!-- claim C34 -->
5. The planning model writes the final answer. A long list in the answer is written by code from the stored rows, in input order. <!-- claim C24 -->

```
job --> planner --> step 1 --> the ticked model with the lowest effective
                \-> step 2 --> cost that clears that step's floor; local and
                \-> step 3 --> cloud models are ranked together
                         \--> final answer + receipt
```

## Run it for real

1. Copy `.env.example` to `.env` and add your OpenRouter key. **Set `BORG_ADMIN_TOKEN` in `.env`: the live Docker setup needs it.** The browser reaches the container from Docker's gateway address, not from the container's own loopback, so without a token the admin pages and key routes refuse every request.
2. Start the live setup. <!-- claim C31 -->

```
docker compose -f compose.yaml -f compose.live.yaml up -d --build
```

Without Docker, the Python steps are in `docs/install-guide.md`.

3. Open `http://localhost:8000/ui/models.html`, press Discover, tick the models the router may use, press Get Model Info to score them, then ask your first question on the Ask page.

Full steps, settings and troubleshooting: `docs/install-guide.md`. How each page and setting behaves: `docs/user-guide.md`.

## The on-premise lane

The lane is off by default. Its promise depends on the mode.

- `raw_local` and `all_local` keep your documents on your own machines. The promise, in the user guide's words: raw document text is sent only to models on your own machines, and everything sent to a cloud model is listed and you can read it. <!-- claim C11 -->
- In `all_local`, nothing is sent off the machine. <!-- claim C12 -->
- A step that must stay local never falls back to a cloud model. <!-- claim C13 -->
- Every request is recorded before it is sent, and a failed record blocks the send. <!-- claim C14 -->
- In `raw_local` and `all_local`, what a local step returns is checked, and an output that copies document text is withheld. <!-- claim C17 -->
- `by_reference` makes no privacy promise. Cloud models can read the documents, and the output check and the check on outgoing requests (BRT-LANE-006) do not run. See "On-premise lane" in `docs/user-guide.md`.

Read "What is not promised" in the user guide before you rely on the lane.

## Limits

- A reply cut off by the token cap is retried once with a larger cap, then fails. <!-- claim C22 -->
- If every planned step fails and the planning model still writes an answer, the receipt says so in plain words. Treat that answer as unchecked. <!-- claim C26 -->
- Admin pages and keys are locked by `BORG_ADMIN_TOKEN`. Without a token, only the machine itself may use them, which in Docker is nobody, so the live Docker setup needs a token. Running `uvicorn` directly on the host is the one case where the loopback rule works without one. <!-- claim C18 -->
- The replay demo cannot change anything: every route that changes state is refused. <!-- claim C28 -->

## Licence

MIT. See `LICENSE`.

## Contact

Dhananjay Raol, Aliud Inquisito Inc.

<!-- TODO (planner): add a contact address or web link for the README contact line. -->
