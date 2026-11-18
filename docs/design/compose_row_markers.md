# Rows placed by code: compose row markers (planner ruling 21, design note)

Status: revised 18-Nov-2026 after review and planner rulings 21a and 21b. No code yet. Files to change: `src/orchestrator/engine.py`, a new `src/orchestrator/row_markers.py`, `src/orchestrator/apply_to_each.py` (failure origin, section 3), `guard_rows_block` in `src/lane/context.py`, `ui/assets/workspace.js` (one label, added after ruling 22's uncommitted edits to that file land), `tests/e2e/stub_model_server.py`, the error registry and the user guide. No change to `src/prompts/*.py`, `data/skills.yaml` or `data/capability_matrix.yaml`.

## 1. Problem

When the answer is a long list, compose retypes every row that apply_to_each already stored. For 200 rows that is about 7,085 output tokens (bench round 1, job A direct arm), more than the 6,000-token compose cap. The reply is cut, the BRT-ORC-018 retry doubles the cap, and the cut call is paid for. Ruling 20 keeps the cap as it is. Code should write the rows; compose should write only the text around them.

## 2. Marker syntax

`@@ROWS:<step id>@@` is replaced by a JSON array, `@@ROWS_TABLE:<step id>@@` by a Markdown table.

- **Strict form** (the only one substituted): `@@\s*(ROWS|ROWS_TABLE)\s*:\s*([^@\n]{1,200}?)\s*@@`, with the keyword matched in any case. The captured id has whitespace stripped and is compared as text with `str(step_id)`, as R1 does.
- **Loose detector:** `@@\s*row`, case ignored. Every loose hit that is not a strict marker for a listed step is invalid (section 6), with one exception: a hit whose exact text appears in the operator instruction or in the stage results is left as plain text, because it is quoted data, not a marker compose made.
- **Why `@@`:** skill placeholders are `{name}` (`library.py` `_PLACEHOLDER`). The orchestrator template uses `{key}` and `{{ }}` (`_safe_sub`, `_undouble_braces`), and skill wording goes through `str.format`. A marker without braces cannot be read as any of these. `@` needs no escape in a JSON string, so the model writes the same characters inside its JSON reply. `@@` means nothing in CommonMark or GFM. Prose uses `@` singly (emails, handles, decorators). A diff hunk header (`@@ -1,3 +1,4 @@`) does not match `@@\s*row`. Before this note, nothing in `src/`, `data/`, `ui/` or `docs/` contained `@@`; this note now does.
- **Not markable:** a step whose id has surrounding whitespace, is over 200 characters, or contains `@` or a newline is left out of the block. Its rows can still be retyped.

## 3. List-holding steps and the row shape

A step holds a list when it is in this run's `results` and `resolved`, and either:

- **apply_to_each:** `env.output` is the aggregate (`results`, `failures`, `total`, and so on; see `expand_and_run`). Its rows are `results` and `failures` merged and sorted on `index`, which gives input order.
- **state_op returning a list:** `env.output == {"result": <list>}` (filter or sample; count and group_by return objects). The node type is recorded from the task (`env._node_type = done_task.type`, set beside `_data_scope`) and not guessed from the shape. A list of aggregate entries (each has `index` and `output`) is sorted by `index`, so it is in input order. Any other list keeps its stored order, and the block says so.

**Rows:**

- A resolved item is `{"item": <id>, **output fields}`.
  - The id is the item itself when it is a string or number, or `item["id"]` when the item is an object with a scalar `id`. Otherwise it is the index.
  - An object output contributes its keys. Any other output becomes `{"output": value}`.
  - Output fields named `item` or `error` are renamed `output_item` and `output_error`.
- A failed item is `{"item": <id>, "error": <code>}`, with no message.
  - The code is `reason["code"]` when the reason is an object.
  - When the reason is a model-written string, the code is BRT-ANS-010.
  - Otherwise it is `raw_failure_code(reason, "BRT-ORC-004")`.
  - Model origin is recorded in `apply_to_each._run_one`. It becomes `"_from_model": bool(env._reason_from_model)` on the failure entry. `_stage_output_for_prompt` and salvage strip that key, so every prompt stays byte-identical.
- An element of a non-entry state_op list is placed as stored.
- Field names are `item`, then the output keys in the order first seen, then `error` if any row failed.

## 4. The block added to compose's user message

The block is sent only when at least one list-holding step exists. It is added after the existing first line of every compose call in the run: the first call, the BRT-ORC-029 re-ask, the BRT-ORC-032 re-ask and the cut-off retry.

```
Mode: compose. Reply with the final answer.
List steps the code can write out for you: put @@ROWS:<step id>@@ in your answer where a step's rows belong (a JSON array, one row per line), or @@ROWS_TABLE:<step id>@@ for a Markdown table. Do not type those rows yourself.
- classify_emails: 200 rows, input order; fields: item, bucket, confidence, defer_reason, error
```

Each step line carries the id, the row count, the order ("input order" or "stored order") and the field names, and nothing else.

With no list-holding step, the user message is byte-identical to today's. That covers the Rule Zero shortcut and the empty-plan compose, which both pass `stage_results={}`. The block is built in engine code, like `_MODE_USER_MESSAGES` and `_COMPOSE_REASK`, and passed in through a new optional `extra_user_lines` argument of `_invoke_orchestrator_model`. `build_system_prompt()`, the template and `_safe_sub` are not touched.

## 5. Where substitution happens

**Gate:** validation and substitution run only when the block was sent. Otherwise the reply is read exactly as today, untouched.

In `_handle_compose_result`, on each reply read (the first reply or a re-ask reply):

1. `_compose_text` picks the answer field, as today. For `action: fail`, the rationale is the candidate answer.
2. **Validate** every loose hit (section 2). If one is invalid, take the BRT-ORC-032 path (section 6).
3. **Render and substitute** with `row_markers.place(value, steps)` on the chosen field's value, before it is turned into text:
   - JSON form: wrapped in a ```` ```json ```` fence, unless the marker already stands alone on a line inside a fence. Backticks directly around a marker are dropped.
   - Table form: placed on its own lines with a blank line before and after, never inside a fence. If the marker is inside a fence, the fence is closed before the table and reopened after it with the same info string. Scalar cells are written as text; a list or object cell is written as compact JSON. `|` becomes `\|` and a line break becomes a space.
   - Object or list answers: a string leaf equal to a marker becomes the rows list itself. A marker inside a longer leaf is placed in compact form, on one line with no fence.
   - Substitution is a single pass. Placed rows are never scanned again, so a worker's output cannot trigger a placement or pull in another list.
4. **Guard each rendered block** (section 7) before anything is returned.
5. Log BRT-ORC-028 if the shape is wrong (unchanged). Log an `orchestrator_decision` event with `rows_placed: {step: count}`. Then call `_mark_answer_kind("composed")`, or `"declined"` for `action: fail`.

R2's order is unchanged: if the reply has no final text, BRT-ORC-031 applies, then 029 (one re-ask), then 030 (salvage). Salvage is built in code from stored outputs and holds no compose-made marker. `run_pipeline` stores the returned string. `chat_completions` streams it only after `orchestrate_run` returns, so neither the store nor the stream ever sees a marker.

**Stated limit, child runs:** a child run places rows inside its own compose. The parent receives the child's answer as text and cannot mark it, so a list inside a child run is retyped by the parent's compose. This is flagged to the planner.

## 6. Failure cases

- **Invalid marker (ruling 21a).** BRT-ORC-032 is logged with `"reask": true`. Compose is asked once with:
  `Mode: compose. Your last reply used a list marker that names no list step. Use only the step ids listed below. Reply with {"response": "<the final answer>"} only.` followed by the section 4 block lines. The bad id is not echoed. If the second reply is also invalid, BRT-ORC-032 is recorded on the run as a warning and the run is salvaged (`answer_kind = "salvaged"`, never `composed`). The run view labels 032 at the same level as 029, 030 and 031, as "A list marker named no list step". Bench scoring of a salvaged run is unchanged.
- **Marker left in an answer.** No compose-made marker or near-miss can be left: every loose hit is either substituted or sent to the invalid path. The only exception is quoted text from section 2. The no-`@@ROWS` test covers the composed, salvaged (032 and 030) and declined paths.
- **Reply that retypes the rows.** It is accepted as today and nothing is placed.
- **Reply cut at the cap while the block was sent.** In `_invoke_orchestrator_model` the BRT-ORC-018 retry always runs once:
  - The cap is `max(current, min(2 * current, 16000))`.
  - The user message is replaced by:
    `Mode: compose. Your last reply was cut off at the length limit. Do not type out a step's rows: put @@ROWS:<step id>@@ where they belong and the code writes them in. Reply with {"response": "<the final answer>"} only.` followed by the block's step lines.
  - The event logs `"retry_message": "rows_pointer"`.
  - If the pointer retry is cut again, the JSON-only parse retry is skipped. A reply that parses goes to the R2 path; one that does not raises BRT-ORC-002, and the caller salvages (as for any compose call that raises).
  - With no block, the retry is exactly as today.
- **Worst case: 9 posts.** Each compose invocation makes at most 3 posts: the first post, the pointer retry, and the JSON-only retry, which runs only when the pointer retry was not cut. A run makes at most 3 compose invocations: the first, the 029 re-ask and the 032 re-ask. Today's worst case is 6. A test drives the 9-post path and asserts the count.

## 7. Lane

**The claim:** placement adds only text compose already received in this run. Nothing new leaves the machine in this run.

- Plan and re_plan calls get no block.
- Compose's `stage_results` are neither trimmed nor changed. The block repeats only ids, counts, order words and output key names, and compose already receives all of them.
- For a raw step in `raw_local` or `all_local`, `item` is the document part id when `lane.is_part_id(item)` is true, and otherwise the index (the same rule as salvage). Each resolved item's output passed `guard_envelope` on its own, and a failed item carries a code only. A raw state_op list is offered only when every element is an aggregate entry.

**Guard on placed blocks (ruling 21b).** `guard_rows_block(rows, block_text, ctx)` runs `copied_rule` against every run document (`ctx.indexes(None)`). It checks:

- the decoded row values, `normalise(serialise(rows))`, the same decoding `guard_envelope` uses, never escaped JSON text;
- for the table form, also `normalise(block_text)`.

The guard runs on each rendered rows block, in `raw_local` and `all_local` when the run has documents. A hit raises `LaneError("BRT-LANE-009")` naming document ids only. The run fails closed: nothing is stored or streamed. The user guide calls BRT-LANE-009 a defect, as it does BRT-LANE-003. The guard never runs on compose's own text, with or without a marker; that text behaves exactly as today. There is no per-item size cap, which would block every list.

**Stated limits** (in this note and in the user guide's "On-premise lane" section):

- The coverage rule (b), the share of a document's 8-grams, is not checked on joined rows. Each row was guarded alone, and a document split across many rows below both copy limits is not caught.
- Salvage also joins rows in code and has no such check. That is an open question for the lane's next review (ruling 21b); salvage is not extended here.

`by_reference` makes no promise and is unchanged.

## 8. Invariants (DESIGN_PRINCIPLES.md)

- No new node type: placement is post-processing inside compose.
- Law 1: governance reaches compose through `build_system_prompt()` verbatim and unchanged. The block is content in the user message and replaces nothing.
- Orchestrator = Composer: same prompt and mode flag, and compose still receives every stage result.
- Law 2: no scratch is read or written.
- No protected file is edited and no new framework is added (stdlib `re` and `json` only).
- Off switch: a plain revert. With no list-holding step, behaviour is byte-identical.

## 9. Tests to write first (red before code)

**Harness:** `tests/e2e/stub_model_server.py` gains a per-call `compose_for(req) -> (content, finish_reason)` hook, so each compose call (first call, re-ask, retry) can be scripted.

**`tests/e2e/test_compose_row_markers.py`:**

1. **200 items.** 200 stand-in items, one of which fails. Compose replies with a short text plus `@@ROWS:rate@@`. Checks:
   - The compose reply content is under 500 characters, so under 500 tokens with any tokenizer. This proves the engine side only; it says nothing about how often a real model chooses the marker.
   - The answer holds 200 rows in input order inside a json fence. The failed row is in its place with its code: BRT-ANS-010 when the model wrote the reason.
   - The run is `composed`, no `@@ROWS` remains, and the user message holds the exact block.
2. **Gate.** With no list step, the compose user message equals `Mode: compose. Reply with the final answer.`, and a reply containing `@@ROWS:x@@` is returned untouched.
3. **Rendering.** The table form: header row, 200 lines, `|` escaped, blank lines around it, and a fence closed and reopened around it. The JSON form: a fence is added, no double fence when the marker is alone in a fence, surrounding backticks are dropped. A dict-leaf marker becomes a compact list.
4. **Invalid and near-miss markers.** An unknown id, a non-list step, `@@rows : x @@`, `@@Row:x@@` and a marker with a space-padded id. Each gives one re-ask with the exact text. A valid second reply gives `composed`. An invalid second reply gives BRT-ORC-032 and `salvaged`. A loose hit quoted from the operator instruction stays as text.
5. **Retyping.** A retyped reply is accepted and nothing is placed.
6. **Cut-off.** The first compose call returns `finish_reason: "length"` while the block was sent. The retry carries the exact pointer text, the cap from the formula and `"retry_message": "rows_pointer"`. At 16,000 the retry still runs at the same cap. When the pointer retry is cut again, there is no JSON-only retry, and the run is salvaged or takes the R2 path. The 9-post worst case is driven and its count asserted. With no block, the retry is unchanged.
7. **Lane (ruling 21b).**
   - In `raw_local`, a forced row holding a 120-character document passage gives BRT-LANE-009, and nothing is stored.
   - The same passage in compose's own text next to a marker behaves as today.
   - An `all_local` case with no markers gives the same composed answer as today.
   - Rows carry part ids only.
   - Plan and re_plan bodies are identical with and without the feature.
8. **Replay of stage-1 job A.**
   - Source: the recorded replies exist only in the git-ignored `bench/results/2026_11_17/dbs/A_borg_cloud_1.db`. Orchestrator replies are in `logs.decision_json["parsed"]`; item outputs are in `envelopes.inbound_payload` (`classify_emails`: 189 resolved, 11 failed with BRT-ANS-005; `extract_urgency`: 200). They are extracted to `tests/fixtures/stage1_job_a_replay.json` as a test input only: document ids and model-written fields, no email text. The documents come from the committed `bench/data/job_a`.
   - Strictness: the test asserts that its step ids match the recording, and the stand-in fails loudly on any request it has no recording for.
   - Script: the recorded compose reply is `{"action":"compose"}`, so the run takes the 029 re-ask, which carries the block. The stand-in answers that re-ask with a scripted `{"response": "@@ROWS:extract_urgency@@"}`.
   - Expected: the run completes as `composed`, with 200 rows in document order.

**Unit tests, `tests/orchestrator/test_row_markers.py`:** the index merge and sort, the id rules, the key renames, failure codes and `_from_model` (including that it is stripped from prompts), the strict and loose parsers, the block's id exclusions, rendering, and the no-rescan rule.

## 10. Open questions

1. Child runs (section 5): a list inside a child run is retyped by the parent's compose. Should it be planned later?
2. For the lane's next review (ruling 21b): salvage joins rows in code with no rows-block check.
