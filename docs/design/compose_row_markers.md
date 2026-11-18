# Rows placed by code: compose row markers (planner ruling 21, design note)

Status: design for review, 18-Nov-2026. No code yet. Touches `src/orchestrator/engine.py`, one new module `src/orchestrator/row_markers.py`, one function in `src/lane/context.py`, the error registry and the user guide. Does not touch `src/prompts/*.py`, `data/skills.yaml` or `data/capability_matrix.yaml`.

## 1. Problem

When a job's answer is a long list, compose retypes every row that apply_to_each already stored. For a 200-row list that is about 7,085 output tokens (bench round 1, job A direct arm), above the 6,000-token compose cap, so the reply is cut, the BRT-ORC-018 retry doubles the cap, and the cut call is paid for. Ruling 20 keeps the cap where it is. Code should write the rows, and compose should write only the text around them.

## 2. Marker syntax

`@@ROWS:<step id>@@` places the rows as a JSON array. `@@ROWS_TABLE:<step id>@@` places them as a Markdown table.

- Strict form, the only one that is substituted: `@@ROWS(_TABLE)?:([^@\n]{1,200})@@`. The captured id is compared as text with `str(step_id)`, the same comparison R1 uses.
- Loose form, used to catch near-misses: `@@\s*rows(_table)?\s*:` with case ignored. Any loose hit that is not a valid strict marker counts as an invalid marker (section 6).
- Why `@@`: skill placeholders are `{name}` (`library.py` `_PLACEHOLDER`), the orchestrator template uses `{key}` and `{{ }}` (`_safe_sub`, `_undouble_braces`), and a skill's wording goes through `str.format`. A marker without braces cannot be read as any of these. `@` needs no escape inside a JSON string, so the model writes the same characters in its JSON reply. `@@` has no meaning in CommonMark or GFM. In prose, `@` appears singly (emails, handles, decorators). A diff hunk header (`@@ -1,3 +1,4 @@`) does not match `@@ROWS`. Nothing in `src/`, `data/`, `ui/` or `docs/` contains `@@` today.
- Table form: header = the field names. One line per row. Scalar cells are written as text; a list or object is written as compact JSON. `|` becomes `\|`, and a line break becomes a space. It costs about 15 lines, so it is included.
- Limit: a step whose id contains `@` or a line break cannot be marked. Such a step is left out of the marker block.

## 3. List-holding steps and the row shape

A step holds a list when it is in this run's `results`, its status is `resolved`, and one of these holds:

- **apply_to_each:** `env.output` is the aggregate `{"results": [...], "failures": [...], "total", ...}` (`apply_to_each.expand_and_run`). Rows are `results` and `failures` merged on `index` and sorted by `index`, which is input order.
- **state_op returning a list:** `env.output == {"result": <list>}` (`state_ops.dispatch_state_op`). This covers filter and sample; count and group_by return objects and are not lists. Rows keep the list's stored order. The node type is taken from the task (`env._node_type = done_task.type`, set in the dispatch loop next to `_data_scope`), not guessed from the output's shape.

Row shape:

- A resolved item is `{"item": <id>, **output fields}`. The id is the item itself when it is a string or number; `item["id"]` when the item is an object with a scalar `id`; otherwise the index. The output fields are the keys of `output` when it is an object; any other output becomes `{"output": value}`. The envelope's `confidence` is not added. An output field named `item` or `error` is renamed `output_item` or `output_error`.
- A failed item is `{"item": <id>, "error": <code>}`. The code is `reason["code"]` when the reason is an object; when it is a string, it is `raw_failure_code(reason, "BRT-ORC-004")`. No message is included.
- An element of a state_op list is placed the same way when it is an aggregate entry (it has `index` and `output`), and as stored otherwise.
- Field names: `item`, then the output keys in the order first seen, then `error` if any row failed.
- JSON form: `[`, one compact row per line (`ensure_ascii=False`), `]`. A step with 0 rows places `[]`.

## 4. The line added to compose's user message

When at least one list-holding step exists, every compose call of the run (the first call, the BRT-ORC-029 re-ask, the invalid-marker re-ask and the cut-off retry) adds this block to its user message after the existing line:

```
Mode: compose. Reply with the final answer.
List steps the code can write out for you: put @@ROWS:<step id>@@ in your answer where a step's rows belong (a JSON array, one row per line, in input order), or @@ROWS_TABLE:<step id>@@ for a Markdown table. Do not type those rows yourself.
- classify_emails: 200 rows; fields: item, bucket, confidence, defer_reason, error
```

Each step line holds only the id, the row count and the field names. With no list-holding step, the user message is byte-identical to today's. This includes the Rule Zero shortcut and the empty-plan compose, because both pass `stage_results={}`. The block is built in engine code, the same way as `_MODE_USER_MESSAGES` and `_COMPOSE_REASK`, and is passed through a new optional `extra_user_lines` argument of `_invoke_orchestrator_model`. The system prompt, `build_system_prompt()`, the template and `_safe_sub` are not touched. The block has no braces, so `_safe_sub` could not alter it even if it were applied.

## 5. Where substitution happens

In `_handle_compose_result`, on whatever reply is being read (the first reply or a re-ask reply):

1. `_compose_text` picks the answer field, as today. If the reply is `action: fail`, the rationale is the candidate answer.
2. **Validate:** every loose marker in the candidate must be a strict marker that names a list-holding step. If not, go to the invalid-marker path (section 6).
3. **Substitute:** `row_markers.place(value, steps)` runs on the chosen field's value before it is rendered to text. In a string, every marker is replaced by its rows text. When the answer is a list or object, a string leaf equal to a marker becomes the rows list itself; a marker inside a longer string leaf is replaced by the text. This runs once: inserted rows are never scanned again, so a worker's output cannot trigger a placement or pull in another list.
4. **Guard** (lane, section 7) on the assembled text.
5. Log BRT-ORC-028 if the shape is wrong (unchanged). Log an `orchestrator_decision` with `rows_placed: {step: count}`. Then call `_mark_answer_kind("composed")`, or `"declined"` for `action: fail`.

The recovery order of R2 is unchanged: if there is no final text, the reply goes to 031, then 029 (one re-ask), then 030 (salvage). Salvage is built by code from stored outputs, so it contains no marker. The returned string is the assembled answer. `run_pipeline` stores it, and `chat_completions` streams it only after `orchestrate_run` returns (`_stream_response`), so neither the store nor the stream ever sees a marker. Child runs do the same inside their own compose, so a parent sees the child's assembled text. A parent cannot mark a child's inner steps.

## 6. Failure cases

- **Invalid marker** (unknown id, a step that holds no list, or a malformed or wrong-case marker): BRT-ORC-032 is logged (`"reask": true`), and compose is asked once with `Mode: compose. Your last reply used a list marker that names no list step. Use only the step ids listed below. Reply with {"response": "<the final answer>"} only.` followed by the block from section 4. The model's bad id is not echoed back. There is one such re-ask per run. It is separate from the 029 re-ask, so a run makes at most three compose calls. A second invalid reply logs BRT-ORC-032 as a warning and the run takes the salvage path, `answer_kind = "salvaged"`; see open question 1.
- **Marker left in the answer:** this cannot happen. Every loose hit in compose-written text is either substituted or sent to the invalid path. A test asserts that no answer contains `@@ROWS` written by compose.
- **Reply that retypes the rows:** it has no marker, so it is accepted as today (`composed`). Nothing is placed.
- **Reply cut at the cap while a list-holding step exists:** in `_invoke_orchestrator_model` (compose mode with a block present), the BRT-ORC-018 retry still doubles the cap up to 16,000. It also always runs once, even when the cap is already at 16,000 (it then keeps the same cap), and its user message is replaced with:
  `Mode: compose. Your last reply was cut off at the length limit. Do not type out a step's rows: put @@ROWS:<step id>@@ where they belong and the code writes them in. Reply with {"response": "<the final answer>"} only.` followed by the section 4 block (without its first line). With no list-holding step, the retry behaves exactly as today.

## 7. Lane

- **The planner's view is unchanged.** Plan and re_plan calls get no block. Compose's `stage_results` are not trimmed and not changed. The block repeats only ids, counts and output key names, and compose already receives all of these in `stage_results` (`_stage_output_for_prompt` returns a resolved step's output whole). The block travels in the same `send_outbound` body, so the backstop checks it.
- **Raw rows in `raw_local` and `all_local`:** for a raw step, `item` is the document part id only when `lane.is_part_id(item)`, and otherwise it is the index (the same rule as salvage). Each resolved item's output passed `guard_envelope` on its own, and a failed item carries only a code. A raw state_op list is offered only when every element is an aggregate entry. Can substitution put raw-derived text in the answer that the guard would have caught? No. Every row placed is text that compose received in this same run. In `raw_local` that request left the machine only after the backstop passed it, and the backstop is stricter than the guard because it adds the coverage rule. The one new thing code does is join texts: row next to row, and compose's text next to a row. Each row starts with `{"item": ...`, and that key text sits between any two pieces, so a joined verbatim run cannot form across a boundary. That claim is checked, not assumed:
- **Guard on the assembled answer:** a new `guard_answer(text, ctx)` in `src/lane/context.py` applies `copied_rule` for every run document (`ctx.indexes(None)`) to the normalised assembled text. It applies the copy rules only, not the per-item size cap, which would block every list. It runs on every composed answer while `ctx.keeps_raw_local` and the run has documents. A hit raises `LaneError("BRT-LANE-009")` naming document ids only, the run fails, and nothing is stored or streamed. `by_reference` makes no promise and is unchanged.

## 8. Invariants (DESIGN_PRINCIPLES.md)

- **Four node types:** none added. Placement is a step of compose's post-processing, not a node.
- **Law 1:** governance still reaches compose through `build_system_prompt()`, verbatim and unchanged. The block is content in the user message and replaces nothing.
- **Orchestrator = Composer:** same prompt, same mode flag. Compose still receives every stage result, and the ruling's "input not trimmed" holds.
- **Law 2:** no scratch is read or written.
- **Protected files:** no edit. The tech stack is unchanged (stdlib `re` and `json`).
- **Off switch:** a plain revert. With no list-holding step, behaviour is byte-identical.

## 9. Tests to write first (red before code)

`tests/e2e/test_compose_row_markers.py`, using the stand-in server (`stub_model_server.StubModels`):

1. 200 inline stand-in items, one of which fails. Compose replies `{"response": "Classified below.\n@@ROWS:rate@@"}`. Check: the compose reply content is under 500 characters, so under 500 tokens with any tokenizer. The answer holds 200 rows in input order, and the failed row is in its place with its code. `answer_kind` is composed. No `@@ROWS` remains. The compose user message has the exact block.
2. With no list step, the compose user message equals `Mode: compose. Reply with the final answer.`
3. Table form: header, 200 lines, `|` escaped.
4. Invalid marker: an unknown id, then a non-list (answerer) step, then a lowercase marker. Each produces one re-ask with the exact text. A valid second reply gives composed. An invalid second reply gives BRT-ORC-032 and salvaged.
5. A retyped reply is accepted and nothing is placed.
6. Cut-off: the first compose returns `finish_reason: "length"` with a list step. The retry's user message is the exact cut-off text, and the cap is doubled. At 16,000 the retry still happens with the same cap. With no list step, the retry is unchanged.
7. Lane, `raw_local`: rows carry part ids and no document text. The plan and re_plan bodies are identical with and without the feature. A compose text holding a 120-character document passage next to a marker gives BRT-LANE-009, and the answer is not stored.
8. Replay of stage-1 job A. **Where the fixtures are:** nowhere committed. The recorded replies exist only in git-ignored `bench/results/2026_11_17/dbs/A_borg_cloud_1.db`: plan, re_plan and compose replies in `logs.decision_json["parsed"]`, and the per-item outputs in `envelopes.inbound_payload` (`classify_emails` 189 + 11 failed with BRT-ANS-005; `extract_urgency` 200). The lead's earlier replay was not committed. Plan: extract them into `tests/fixtures/stage1_job_a_replay.json` (document ids and model-written fields only, no email text), and use the committed `bench/data/job_a` documents. The recorded compose reply is `{"action":"compose"}`, so the test gets the 029 re-ask, which carries the block. The stand-in answers that re-ask with a scripted `{"response": "@@ROWS:extract_urgency@@"}`. Check: completed, composed, 200 rows in document order.

Unit tests in `tests/orchestrator/test_row_markers.py`: the index merge, the id rules, the key renames, failure-code extraction, state_op entries, the raw item rule, the strict and loose parsers, and the no-rescan rule.

## 10. Open questions for the planner

1. "Then fail with a new code": should a second invalid marker **salvage** (as proposed: BRT-ORC-032 as a warning, `answer_kind` salvaged, which keeps ruling 20's "never thrown away" and the lead's overrule) or fail the run outright?
2. The guard on the assembled answer (BRT-LANE-009) runs on **every** composed answer in `raw_local`/`all_local` with documents, not only answers with markers. This newly guards answers compose wrote itself. Should it be narrowed to answers with markers?
3. The replay test needs a committed extract of the round-1 job A recordings (about 50 KB, no email text). Is committing it acceptable? It is used only as a test input, never to choose wording or defaults.
4. New codes: BRT-ORC-032 (invalid marker) and BRT-LANE-009 (assembled answer withheld). Please confirm the numbers.
