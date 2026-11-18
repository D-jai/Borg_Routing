# Packing small items into one call (planner ruling 23, design note)

Status: draft, 18-Nov-2026, for a fresh reviewer's attack. No code yet. Spec: `BUILD.md` section 8, row "Planner rulings 21-24", part (23); its defaults are used here unchanged. Files to change: a new `src/orchestrator/packing.py`, `src/orchestrator/apply_to_each.py`, `src/agents/executor.py` (two small helpers moved out, no behaviour change), `src/lane/context.py` (`guard_envelope` gets an optional document list), `src/lane/outbound.py` and `src/db/schema.py` (a `documents` column on the ledger), `src/api/tasks.py` (redaction keeps pack counts), `ui/assets/workspace.js`, `ui/assets/pages/run.js`, `tests/e2e/stub_model_server.py`, the error registry, `docs/user-guide.md` (line 706 says "one call per item"), `.env.example`. No change to `src/prompts/*.py`, `data/skills.yaml` or `data/capability_matrix.yaml`.

## 1. Problem

An apply_to_each step makes one model call per item, and each call repeats the same fixed text: the governance block, the operator's instruction, the answerer system prompt, the task ids, the skill text and the output schema. The round-1 measurement (`bench_round1_measurements.md`) found an item call of about 1,419 prompt tokens, of which the item itself was 54; about 96% is the same in every call. Packing sends several small items of one step in one call, with the fixed text once, and still stores one result per item.

## 2. Where packing sits

Inside `expand_and_run`, after the step's one route decision, which is unchanged. Packing changes only how the child tasks reach the model.

- Every child `WrappedTask` is still built by `_make_child_task`, with its own `envelope_id`, `subtask_id`, `level_path`, `trigger_reason`, `data_scope` and `documents`.
- Every item still gets its own `InboundEnvelope`, built from its slice of the reply. It then goes through `mark_model_reason`, `validate_inbound` and, for a raw item, `guard_envelope`, and is filed by the same code that files a single call's envelope: `results` (`index`, `item`, `output`, `confidence`) or `failures` (`index`, `item`, `reason` via `_failure_reason`, `model_id`), plus `_model_reason_indexes`, `lane_guarded` and the token and dollar totals.
- The aggregate output has no new key. state_ops, re_plan, compose, row markers (ruling 21), the receipt and replay therefore read exactly what they read today.
- Each item's `model_call` log event keeps the item's envelope id. A packed call's share fields are described in section 6.

## 3. How packs are formed

**Settings.** `BORG_PACK_MAX_ITEMS`, default 20, is read when the step starts. A value of 0 or 1 turns packing off. A value that is not an integer also turns it off and logs a warning, BRT-APE-007. With packing off, `_run_one` runs for every item exactly as today.

**Estimates.** The estimator is the router's: characters // 4 (`node_router._estimate_input_tokens`), made public as `estimate_tokens(text)`.

- An item's tokens: the estimate of the context text the single path would give that child. For a raw item, this is `Document id: <id>` plus `lane.text_block([id])`.
- An item's expected output: the estimate of `{"item_<i>": {"status": "resolved", "confidence": 0.00, "output": <output_schema>, "failure_reason": ""}}` serialised. This is no tuned figure. A JSON-Schema-style schema overestimates, which only makes packs smaller. A free-text field can underestimate; the reply is then cut, and section 5 halves the pack.
- A pack's prompt: the estimate of the full system prompt built by section 4, plus 1 for the user message.

**Forming the packs.** Items are walked once, in index order:

1. An item over 400 estimated tokens runs alone.
2. Every other item joins the open pack if, with it included, all of these hold:
   - the pack has at most `BORG_PACK_MAX_ITEMS` items;
   - the items' estimates total at most 4,000;
   - the prompt estimate plus the expected output fits the model's `context_length`;
   - the expected output is at most the answerer cap, `_answerer_caps(model, task.max_output_tokens)[0]`.
3. If an item does not fit, the open pack closes and a new one starts with that item.
4. An item that fails these checks on its own runs alone.
5. A pack left with one item runs alone on the single path.

Pack membership depends only on the items and the settings, never on timing. Inside a pack, items are listed in index order.

**Lane key.** A pack never mixes lane keys (section 7).

**Concurrency.** The step's semaphore (`concurrency_limit`, capped at `MAX_CONCURRENCY`) is taken around each HTTP call, packed or single. It is never held while waiting on another call, so there is no deadlock. A pack counts as one call in flight, and so does each retry or half it spawns. Results are sorted by `index` at the end, as today.

## 4. The packed call

`packing.build_packed_prompt(task_children, governance)` returns `build_system_prompt(stage_prompt=FRAME_filled, global_rules, admin_rules_ledger, user_instruction=parent.governance_instruction or parent.instruction)`. This is the same function and the same arguments the single path uses (`executor._execute_answerer`).

- Governance and the operator's instruction appear verbatim, once, first.
- The user message is `begin`, as today.
- The payload has the same keys as the single path: `model`, `messages`, `temperature` 0.2, and `max_tokens` = the first cap.
- The packed call does not use the doubled-cap retry. A cut reply is handled by section 5.

**Is the single-path system prompt reused?** Only the governance part. `ANSWERER_SYSTEM_PROMPT` is not used. Its contract ("one task, one envelope") and its output format (one envelope) contradict a keyed reply, and leaving it out removes its 871 tokens. The frame restates, in short, the rules the reply needs. The live check (section 11) measures whether accuracy holds.

**Why Law 1's claim test still holds.** The governance header comes from the same `build_system_prompt()` call with the same `user_instruction`. The claim test's regex, which reads the "Original User Instruction" block up to `\n\n---\n`, finds the operator's exact words in every packed request (test 7).

**The frame.** This is a constant in `packing.py`. Slots are filled in one pass with the executor's `\{(\w+)\}` rule, so slot-like text inside an item is never filled.

```
# Packed Task

You are a leaf worker in BORG_ROUTE. This request holds {count} separate items. Do the task below for each item on its own. The items are independent: never let one item change the answer for another, and never merge, compare or skip items.

## Task (the same for every item)

{instruction}

## Output schema for each item's "output" (match exactly)

{output_schema}

## Rules for every item

1. Follow the Governance Layer above. If doing the task for an item would contradict it, mark that item failed with a short reason.
2. "output" matches the schema exactly: no extra fields, no missing fields.
3. "status" is "resolved" when you did the task for that item and "failed" when you genuinely cannot. If the item does not support an answer, use "failed" with the reason "insufficient context". Do not invent.
4. If the schema allows a "defer" value, use it when you are genuinely uncertain.
5. "confidence" (0.0 to 1.0) is your real certainty for that item.

## Items

{items_json}

## Reply

Reply with one JSON object and nothing else. It has exactly one key for each item id above ({id_list}), each used once. Each value is:
{"status": "resolved" | "failed", "confidence": 0.0-1.0, "output": { ... matching the schema ... }, "failure_reason": "only when status is failed"}
```

**The slots.**

- `{instruction}`: the step's instruction (for a skill step, the rendered skill text), once.
- `{output_schema}`: `json.dumps(schema, indent=2)`, as the single path does.
- `{items_json}`: `prompt_json({"item_<index>": <context text>, ...}, indent=2)`. JSON string escaping means no item can forge another item's boundary.
- `{id_list}`: the ids, comma-separated.
- Ids are `item_<index>`, using the item's position in the whole step, not in the pack. An answer carrying another pack's id is therefore an unknown id here.
- Children carry no `side_effects`, `tool_requests` or state query today, so the frame has no slot for them. If a child ever has any of these, that item runs alone.

## 5. Reading the reply

The reply is parsed with `json.loads(object_pairs_hook=...)`, which records duplicate keys (plain `json.loads` silently keeps the last one). The fence and brace fallbacks of `_extract_json` are kept.

| Case | What happens |
|---|---|
| Value under a pack id, builds a valid `InboundEnvelope` and passes `validate_inbound` | Accepted for that item. A raw item then goes through `guard_envelope`. |
| Value with `status: failed` and a reason | A model decline, stored exactly as a single decline: `_reason_from_model`, so BRT-ANS-010 upward, or BRT-LANE-004 after the guard in `raw_local`/`all_local`. **Not retried**, as the single path does not retry a decline. The live check reports declines per arm. |
| Pack id missing, given twice, or its value fails to build or validate | That item is retried **once, alone**, on the unchanged single path (`_dispatch_child`). Its result is final. |
| Key that is not an id of this pack | Ignored and logged. Nothing is stored under it. The item it was meant for is then missing and retried alone. |
| Reply not JSON, or not an object | Every item is missing, so each is retried alone. |
| `finish_reason == "length"` | Nothing in the reply is read. The pack is halved, `[:ceil(n/2)]` and `[ceil(n/2):]`, and each half runs as a pack. A half of one item runs on the single path, which keeps its doubled-cap retry. |
| The packed post raises (BRT-ANS-002/006/008/011, or any `LaneError`, BRT-LANE-006 included) | Every item of the pack falls back to the single path. Each item then gets exactly today's outcome and failure entry, and the backstop judges each item alone, so one item's hit does not refuse 19 others. Failed posts are normally unbilled. A billed one is logged as a share (section 6). |

**Limit.** A model that swaps two answers between valid ids cannot be caught by ids alone (open question 4).

**Bounds.** Halving stops at one item, so a pack of n has at most ceil(log2 n) levels of packed calls: 5 for 20 items (20, 10, 5, 3, 2). In the worst case, every packed call is cut down to single items. That is a full binary tree: n - 1 packed posts plus n single calls of at most 2 posts each, so 59 posts for a pack of 20. Today the same 20 items take at most 40 posts. A reply read without a cut costs at most 1 + 2n = 41 posts. Every cut post is paid and logged.

## 6. Routing and cost

**Routing.** There is one route decision per step, as today, logged with the same `items` count. The route's per-item cost estimate assumes one call per item and is left unchanged, so it overstates a packed step. The ranking does not change, because every model gets the same estimate.

**The split.** Each packed post, whether read, cut or failed-but-billed, is split across its items. Each item gets one `model_call` event with its share. No call-level row is added, so `SUM(logs)` and the digest stay the true total.

- **Tokens** (integers) use largest-remainder rounding:
  - Input weights are `n * est_i + O`, where `O` is the prompt estimate minus the items' estimates. That is the item's own tokens plus an equal share of the fixed text, scaled by n to stay in integers.
  - Output weights are the length of each read item's serialised value. Items with no value get 0. If every weight is 0, the split is equal.
  - The method: `q_i = T*w_i // W`, then the `T - sum(q_i)` leftover units go to the largest remainders, ties to the lower index. The parts sum to T exactly.
- **Dollars** are integers in picodollars (1e-12 USD):
  - `c_i = t_in_i * round(p_in * 1e12) + t_out_i * round(p_out * 1e12)`.
  - The call total is the sum of `c_i`, an exact integer identity. Provider prices have at most 12 decimals, so `round` is exact.
  - The stored `dollars` is `c_i / 1e12`. `baseline_dollars` is split the same way at baseline prices.
  - Every item event carries `decision.pack = {call, step, size, position, cut, accepted, call_tokens_in, call_tokens_out, call_picodollars}`. Test 5 asserts the integer identity and that the digest equals `SUM(logs.dollars)`.
  - The first item event of each post also carries `payload_in` and `payload_out` (each `[:2000]`, as today).

**The run view.** The step panel's "Items" row in `pages/run.js` reads **"N items in M calls"**, where M is the number of distinct `pack.call` values for the step plus (N minus the items accepted from packs). With packing off this gives "N items in N calls".

- `workspace.js` counts one call per distinct `pack.call` in the "model calls" tile, in `callsByModel` and in `eventSentences`.
- `_redact_events` (lane on, non-admin) keeps `decision.pack` (ids and counts only) on `model_call` events.

**Ruling-22 floor.** `direct_floor_dollars` is computed from the operator message, the documents and the final answer's characters. It never reads calls or tokens, so it is unchanged. Only BORG's own cost beside it falls.

## 7. Lane

**Pack key.** `(data_scope, must_stay_local, has_documents)`. `must_stay_local` uses the executor's formula: `lane.all_local or (raw and lane.keeps_raw_local)`. Inside one step the first two are already shared. The key keeps a raw step's document items apart from its non-document items.

- **Raw packs.** Document text is loaded with `lane.text_block` inside the packing code, immediately before the post. That is the new "executing task" of rule 5.1. Only ids travel before that point.
- **Raw-local pack.** It is posted with `must_stay_local=True` through `send_outbound`, so the call guard sends it only to a local host, or raises BRT-LANE-003 and nothing is sent.
- **Outbound record.** `call_scope` gains `documents`. `record_start` writes them to a new nullable `outbound_requests.documents` column (a JSON list of every document id in the pack, in pack order; NULL for unpacked calls). The row's `envelope_id` is the step's aggregate envelope.
- **Copy check.** The backstop runs in `send_outbound` on the whole body. It covers every packed payload by construction, and a hit falls back to single calls (section 5).
- **Output guard.** `guard_envelope` still runs per item, as today. One gap opens: today item B's model never saw item A's document, so checking B's output against B's documents was enough. In a pack it is not. `guard_envelope` therefore gains `check_docs`: for a packed item it is the union of the pack's document ids. The failure still names only the item's own ids. Test 6d covers B copying A's passage.

**`by_reference`.** Packing does put one document's text beside another's in a single cloud request, where today each went alone. Under `by_reference` (no privacy promise, no guard, no backstop):

- The same model at the same provider receives the same text either way. Nothing reaches a new party, and the ledger records it all.
- Two things change:
  - the provider's request logs now link the documents together;
  - a document carrying injected instructions can now affect the other items' answers in its pack.
- Both go into the user guide's stated limits (open question 5). The second also applies to local packs, where the output guard bounds copying but not correctness.

## 8. Off switch

With `BORG_PACK_MAX_ITEMS` set to 0 or 1, `expand_and_run` runs today's `_run_one` path, and no packing code builds or sends anything.

**Proof, as in the ruling-21 review.** `scripts/compare_request_bodies.py` runs one fixed stand-in job on two trees:

- the job: a plan with an apply_to_each of 45 items, one of them oversized, plus a `raw_local` documents run;
- the trees: a `git worktree` at the commit before the build, and the build tree with the value 0 and then 1;
- controls: `uuid.uuid4` is patched to a seeded sequence and the clock is fixed;
- output: every request body is written as `json.dumps(body)`, which is httpx's own serialisation;
- the check: `diff` must be empty.

The base tree's bodies are also kept as a fixture, and an in-suite test pins them.

## 9. Invariants (DESIGN_PRINCIPLES.md)

- **No new node type.** Packing is transport inside apply_to_each.
- **Law 1.** Governance goes verbatim through `build_system_prompt()` on every packed call, and nothing replaces it.
- **N envelopes.** Each item still has its own envelope and result.
- **Law 2.** Not touched.
- **Files and stack.** No protected file is edited, and nothing outside the stack is added (stdlib only).
- **Revert.** The change reverts cleanly.

## 10. Tests (red first; stand-in server)

The stand-in gains `kind == "packed"` (detected by the `# Packed Task` line) and a `pack_for(req) -> reply | (reply, finish) | Exception` hook. By default it answers every id. The tests:

1. **200 small items make 10 calls.** 10 packed requests and 0 single ones; 200 results in index order with today's fields; the run view data reads "200 items in 10 calls".
2. **A reply missing two items** retries exactly those two, alone, with bodies equal to their packing-off bodies.
3. **Wrong ids.**
   - A value under `item_99` (unknown here) is stored nowhere, and its item is retried alone.
   - A duplicated id is retried alone.
   - A value under another pack's id is unknown.
4. **An oversized item** (1,604 characters, so 401 estimated tokens) runs alone with its packing-off body. The limit case (1,603 characters, so 400 tokens) is packed.
5. **The split costs sum to the call.**
   - Integer token and picodollar parts sum exactly.
   - The digest equals `SUM(logs.dollars)`.
   - A cut call's shares are counted.
   - Halving: the chain of cuts down to single items makes exactly 59 posts.
6. **Lane.**
   - (a) A `raw_local` pack reaches only the local stand-in.
   - (b) Document items and non-document items of one raw step are in different packs.
   - (c) The ledger `documents` column lists every id.
   - (d) Item B copying item A's 120-character passage gets BRT-LANE-004.
   - (e) A derived cloud pack with a backstop hit falls back, only the offending item is refused (BRT-LANE-006), and the others resolve.
   - (f) `by_reference` packs two documents into one cloud call, and the guard does not run.
7. **Law 1.** A claim-test variant with a packed apply_to_each: every packed request's governance instruction equals the operator's, and appears once.
8. **Off switch.** The pinned bodies, and the two-tree script.
9. **Also:**
   - a decline is stored as BRT-ANS-010 and not retried;
   - a post that raises falls back to singles;
   - with `concurrency_limit` 2, at most 2 posts are in flight;
   - an invalid `BORG_PACK_MAX_ITEMS` turns packing off and logs BRT-APE-007.

## 11. Live check (after the build, cents)

**The script.** `scripts/live_check_packing.py`, not under `bench/`. It builds one apply_to_each task directly (no planner, no compose, so only worker calls are paid) and runs it twice on a temporary database: `BORG_PACK_MAX_ITEMS=0`, then 20.

- One cheap worker is pinned by model id: `openrouter/google/gemini-3.1-flash-lite-preview` (US$0.25/US$1.50 per million tokens). The id is printed in the report.
- The governance instruction is a fixed operator sentence.

**The items.** 40 short lines, written fresh for this check and never taken from `bench/`.

- Each line names a quantity, and the label is its kind: `length | mass | time | temperature | volume | none`.
- About 7 per label. A few are deliberately tricky ("a stone's throw", "two degrees of separation", "a pint-sized dog") so the check can tell the two arms apart.
- The output schema is `{"kind": "length|mass|time|temperature|volume|none"}`.
- The items and labels go in `tests/fixtures/packing_live_items.json`, committed in their own commit before the run. That commit is the proof that they were labelled first.

**The report.**

- Correct count per arm. A failed or declined item counts as wrong.
- Declines, retries alone, calls, tokens and dollars per arm.
- The spend goes in `bench/results/spend.json` through `bench/budget.py` (`Budget.check` before, `record` after, key `ruling23_live_check`).

**The pass rule.** If packed is more than 2 items worse than unpacked (`packed_correct < unpacked_correct - 2`), report to the planner and change nothing.

**The estimate.**

- Unpacked: 40 calls of about 1,450 prompt tokens and about 60 output tokens, about US$0.018.
- Packed: 2 calls of about 1,200 prompt tokens and about 1,200 output tokens, about US$0.004.
- The total is about US$0.02, with `Budget.check` capped at US$0.10.

## 12. Open questions for the planner

1. **Reply value shape.** The ruling says each value matches the step's output schema. This note makes each value `{status, confidence, output, failure_reason}`, with `output` matching the schema. Without that, packed items would lose their stored `confidence` and per-item declines (BRT-ANS-010), and "same fields" would not hold in value. Confirm.
2. **Ollama's served context.** Ollama serves with its own context window (often 4,096 tokens), whatever the matrix's `context_length` says, and silently cuts the *front* of an over-long prompt, which is where governance sits. A 4,000-token local pack could lose Law 1 text without any error. Proposal: for an `ollama`-source row, the fit check uses `min(context_length, 4096)` until a served value is recorded. This is a new default, so it is the planner's call.
3. **Timeout on slow local hosts.** A packed reply is about n times one item's output. On a CPU host that can pass `BORG_ANSWERER_CALL_TIMEOUT`, and the timeout then falls back to singles after a wasted wait. Keep one timeout, or scale it by pack size?
4. **Swapped answers.** Two answers swapped between valid ids cannot be detected. Accept this as a stated limit, measured by the live check, or add an echo field?
5. **Cross-item influence.** One document can steer the other items' answers in its pack, in `by_reference` and in local packs alike. Pack raw items as designed, with the limit stated, or keep raw items unpacked?
