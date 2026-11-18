# Bench round 2: root causes and measurements (stage 1, 18-Nov-2026)

Covers: why BORG lost round 2 on jobs A, B, C and D (cloud arm), answered from the run databases and the code. Runs on 8a304f5, freeze f655890, seed 4132800769. Read-only: nothing in src/, data/, ui/, bench/ or tests/ was changed, no paid calls, no tests run (planner ruling 31f). Run databases are git-ignored (`bench/results/2026_11_18/dbs/`). Token counts are logged counts unless marked. `log_payloads` stores request text cut at 2,000 characters; the full request is in `outbound_requests.body` (only when a lane is on: A, C and D have it, B does not).

Dug by a Sonnet helper; the lead re-checked against the code and the databases: the packing condition (packing.py:262-263), the skill's `side_effects: [deferrals]` (data/skills.yaml:59-60) and its presence in A's stored apply_to_each payload, A's answer (10 typed rows, 200 placed doc rows, no urgency in any placed row), C's unit-less "412" and "2,317", and that `src`, `data` and `ui` are unchanged between 8a304f5 and the checkout. No fixes are proposed here; the planner writes the fix rulings.

## 1. Job A: composition (the opening list, the placed rows, the urgencies)

**What compose was shown.** Compose (grok-4.20, max_tokens 6,000, json_object) got the governance block with the operator's instruction verbatim (category + urgency, `{"file","category","urgency"}`), the whole orchestrator template, and the stage results: `classify_emails` as 196 results `{"item": "doc_001", "output": {"bucket": "billing", "confidence": 1.0, "defer_reason": ""}}` plus 4 failures (BRT-ANS-005, missing `defer_reason`), and `fix_and_complete_classifications` with 4 rows `{"file": "doc_004", "category": "shipping", "urgency": 5}` etc. The user message was:

> Mode: compose. Reply with the final answer.
> List steps the code can write out for you: put @@ROWS:<step id>@@ in your answer where a step's rows belong (a JSON array, one row per line)... Do not type those rows yourself.
> - classify_emails: 200 rows, input order; fields: item, bucket, confidence, defer_reason, error

**Why it wrote its own list and the markers were also placed.** Compose did both. It typed 10 rows (`email_001.txt` ... `email_010.txt`, the file names it took from the document list) and then wrote `@@ROWS:classify_emails@@` between the last typed row and a closing `]`. `row_markers.place` (src/orchestrator/row_markers.py:472) replaced the marker with the step's stored rows as its own JSON array. Log 207: `rows placed by code (row markers) ... "rows_placed": {"classify_emails": 200}`. The result is the typed list, then `[` + 200 skill-shaped rows + `]`, then compose's own `]`: the answer ends `]\n]`. Nothing in code checks that compose also typed rows.

**Where the urgencies came from.** No step produced an urgency per email.
- The plan (log 1) had `skill_id: classify_into_buckets` and `output_schema: {"file","category","urgency"}` but no `extra_fields`. The skill's rendered prompt asks only for bucket, confidence, defer_reason. The node's `output_schema` is not used for a skill step: src/skills/library.py:216-218 ("the step's own extra_fields only, never the skill's template, never the node's output_schema"), and src/orchestrator/engine.py:2120 onward renders the skill and never reads `node["output_schema"]` (lines 2331 and 2402 read it only for non-skill nodes). The item request holds the operator's urgency instruction only inside the governance block, while the stage "Instruction:" asks only for a category. Item replies: 0 of 200 contain "urgency".
- re_plan (log 203) saw the 4 failures, wrote a fix step that asked for category and urgency "for EVERY one of the 200 emails" but gave it `"documents": [doc_004, doc_042, doc_138, doc_195]`. It returned 4 rows. Those 4 are the only urgencies in the run (key: 004 right, 042 right, 138 right, 195 gave 5, key 4).
- Compose's own rationale (log 208): "minor urgency values inferred from typical patterns where not explicitly scored." Its 10 typed urgencies match the key on 5 of 10 (003, 004, 005, 006, 009); its category for email_010 (billing) is wrong, key feedback (the worker also said billing).

**Why the placed rows have the skill's fields.** `rows_for` / `_resolved_row` (row_markers.py:93-143) file each stored item output as `{"item": <doc id>, ...output keys}`; failures become `{"item", "error": code}`. `_fields` (156-169) lists those keys to compose. The rows are the worker's stored output, which has the skill's schema (bucket, confidence, defer_reason), not the operator's. Result file: accuracy 0.0, items_answered 0 (no row has `file`, `category`, `urgency`).

## 2. Job A: why 200 single calls (packing was off)

`apply_to_each.py:430` calls `packing.packing_context(task, 200, 20, db)`. It returns None at src/orchestrator/packing.py:262-263:

```
if task.side_effects or task.tool_requests:
    return None
```

The skill declares `side_effects: [deferrals]` (data/skills.yaml:59-60). The A apply_to_each envelope's stored `outbound_payload` has `'side_effects': ['deferrals'], 'tool_requests': []`, `data_scope: 'raw'`, 200 document items. So the parent task has side effects and packing is refused. The children do not inherit side_effects (`_make_child_task`, apply_to_each.py:172-190; the item prompt shows "Allowed scratch tables: (none)"), so the check blocks a step whose children use no scratch. No event is logged when `packing_context` returns None, so the database does not say this; the proof is the code plus the stored payload.

Other conditions excluded: lane mode (`by_reference`: `keeps_raw_local` is False, and 23e says document items pack under by_reference); first-pack rule (never reached); the 400-token item limit (a ~54-token email is far below it).

**What differs from the ruling-23 live check** (`scripts/live_check_packing.py`, 40 items, 2 calls): it builds a bare `WrappedTask` with `dependency_input={"items": [...40 plain strings]}`, no `skill_id`, no `side_effects` (default empty), no lane, no documents. A_borg_cloud is a skill step with `side_effects ['deferrals']`, document-id items and a by_reference lane. The same pattern: D's two classify_into_buckets steps also carry `['deferrals']` and ran 60 + 29 single calls. C's extract_fields has `side_effects []` and did pack, but only two items per step (pack_outcome logs 165 and 167: items 2, accepted 2). GUESS, not measured: only two parts are under the 400-token alone limit and the rest ran alone (164 calls = 83 x 2 - 2).

## 3. Job A: size of one item call

Calls: 200 item calls average 1,565 prompt tokens (1,545 to 1,586) and 65 output; the 201st call is the fix step (1,990 / 180). The 1,978 figure (395,578 / 200) includes 80,508 orchestrator tokens. First item request (doc_020), logged 1,567 tokens, chars/4 estimate 1,418:

| Part | Chars | Est. tokens (chars/4) | Share |
|---|---|---|---|
| Governance header (no global or admin rules) | 646 | 162 | 11.4% |
| Operator instruction, verbatim | 439 | 110 | 7.7% |
| Answerer system prompt (static) | 3,484 | 871 | 61.4% |
| Task ids header | 209 | 52 | 3.7% |
| Skill instruction text | 276 | 69 | 4.9% |
| "Context: Document id: doc_020" | 31 | 8 | 0.5% |
| The email (label + 3 lines) | 211 | 53 | 3.7% |
| "State query result (none)" | 37 | 9 | 0.7% |
| Output schema | 202 | 50 | 3.5% |
| Side effects, fence, trailer | 131 | 33 | 2.3% |
| User message "begin" | 5 | 1 | 0.1% |
| Total | 5,671 | 1,418 | 100% |

Same as round 1 (about 96% fixed overhead). The operator's instruction (including the urgency request) is in there at 110 tokens, but the "Instruction:" the worker acts on never mentions urgency.

## 4. Job B

The worker (gpt-5.3-codex, one call, 3,606 in / 12,534 out, US$0.182) stored `output.files` of 10,435 characters with all four headings: `=== FILE: schema.sql ===`, `app.py`, `openapi.yaml`, `test_app.py`, each in a fenced block; the last file ends `assert bad_tag_query.status_code == 422`. So the worker did the job. (The envelope's `max_output_tokens` is 4000 while 12,534 output tokens were logged; GUESS: hidden reasoning tokens; not checked.)

Compose (log 5, 10,521 tokens in) received it: passing steps show `env.output` unchanged (engine.py `_stage_output_for_prompt`, about line 727-742), and compose's input is 2,312 tokens above the plan call's. The compose request text itself is not stored (lane off; payload cut at 2,000 characters), so full receipt is inferred. Compose returned:

> The notes service has been built according to the provided specification. All four required files have been generated ... **Files produced:** - `schema.sql` (...) - `app.py` (...) ... You can copy each file from the stage result (n1) under the corresponding `=== FILE: ... ===` heading.

No file text in the answer; the grader reports `missing: schema.sql, app.py, openapi.yaml, test_app.py`. re_plan said "Ready to output in the exact requested answer format" and compose then summarised instead. No row marker applies (the output is a string, not a list).

## 5. Job C

Matcher (bench/facts.py): a "count" fact (key "412 loading bays", "2,317 days") matches the same number only if the key's unit word (singular or plural) is within the next three words (`positions`, the `kind == "count"` branch; `quantities` collects the next three words). Money, dates and names have their own rules.

Unmatched: the loading bays question and the audit-log days question (direct matched all 8). BORG's answer lines were `2. 412` and `5. 2,317`. The values are right; the units are missing (the matcher saw `after: ['3','ingrid','vassallo']` and `['6','brannock','cryogenics']`). The workers returned `"kessler_falls_loading_bays": "412"` and `"audit_logs_retention_days": "2,317"` (units dropped by the extraction step), and the operator said "Be brief". Informational score only; the grader and the grade are unchanged. The judge (qwen4-max, 2 rounds) preferred direct.

Contradictions (0 of 2): no request saw both conflicting documents together. Each answerer request holds one 8,000-character part. Request bodies with both "2011" and "2014": 0; with both "seven members" and "nine members": 0. Each half went to a different call, and the workers on those parts reported unrelated filler contradictions (e.g. "steady" vs "slower than hoped"). Compose then wrote filler contradictions, including "14 units of budget vs 154 units of budget", which are not the planted ones. The re_plan rationale ("no cross-document conflicts") shows the planner believed per-part extraction could find cross-document conflicts. Also logged: BRT-ORC-029 (compose had no final answer once, re-asked).

## 6. Job D (cloud): 0.60 on 60

Pass 1: 29 results, 31 failures (BRT-ANS-005, missing required key `defer_reason`; same schema trap as A). re_plan asked `retry_failed_notes` with `skill_args: {"items_ref": "classify_notes", "failed_items_only": true}`. The step ran over `classify_notes`' success list: `_upstream_list` (engine.py:1340-1354) reads the step's "results" key, `failed_items_only` is not an argument anything reads, and the 29 retried items are exactly the 29 that had already succeeded (set equal; labels identical to pass 1). So the 31 failed notes never got a worker label.

Compose typed all 60 rows itself (1,198 output tokens). Comparison with the key:
- 29 notes with a worker label: final label equals the worker's in all 29 (nothing lost or altered in assembly); 25 right, 4 wrong (worker errors).
- 31 notes with no worker label: compose wrote "medium" for all 31; 11 right (the key has 11 medium among them), 20 wrong.
- Wrong total 24 = 4 + 20. Errors by type: high labelled medium 11, low labelled medium 9, medium labelled low 4.

Canaries: `canaries_sent_to_cloud` 60, `canaries_in_answer` 0 (by_reference lane sends the text to cloud by design).

## 7. What a live run on round-1 data would have shown

(Reasoned, not run.) Causes that depend on the code path and not on the data would all have shown up in one end-to-end run on round-1 data (seed 20261117):
1. the skill ignoring the node's `output_schema` and never asking for urgency (job A is the same task, 200 emails, same plan shape; round 1 even recorded that urgency was never requested);
2. packing refused by `side_effects` on any `classify_into_buckets` step (A and D have the same skill; the live check bypassed skills);
3. compose typing rows itself next to a marker;
4. the BRT-ANS-005 `defer_reason` failures on small flash-lite replies (round 1 saw 4 of 200 on A; the rate varies with data, but the trap exists on any classify run);
5. the retry step re-running the successes (any retry through `inputs_ref` of a step with failures);
6. per-part extraction splitting the planted contradiction pair (job C's parts are 8,000 characters whatever the seed; the pair sits in two documents);
7. compose answering B with a summary (B ran on the same spec);
8. units dropped by extraction on C (depends partly on the model's reply; GUESS it recurs).

Only the exact counts (which notes fail, which urgencies compose invents, which 31 notes were unlabelled) depend on the data. Round-1 job-A and D runs went through the lane and the same code paths, so the live check was the missing step, not a better data seed: it ran a hand-built task that skipped the skill, the lane and compose.

## Open

- Job B compose request: not stored; receipt of the full output inferred from `_stage_output_for_prompt` and token counts.
- Job C packing: why only two items per step packed (GUESS: the 400-token alone limit) not measured.
- Job B worker output-token count (12,534) vs `max_output_tokens` 4000: cause not checked (GUESS reasoning tokens).
- Whether packing works for job A once `side_effects` no longer blocks it: not tested (no paid calls).
