# Compose places, it does not retype (planner ruling 32e, design note)

Status: revised 18-Nov-2026 after a fresh adversarial review in two rounds and the lead's rulings on it, then accepted by the planner (BUILD.md section 8, "Planner readings under ruling 32 (4)") with one cut: check C2 (BRT-ORC-039) is dropped. 039 stays unused and the other codes keep their numbers. This revision follows that reading; C2's analysis stays in 2.4.2 as history. It extends ruling 21 (docs/design/compose_row_markers.md). Everything in that note stands unless a section below says otherwise.

**Wording.** The BUILD.md 32e row reads: "seven requirements: chosen fields and names with file names for documents; a whole-output marker; the code-side compose block wording; four post-compose checks with one re-ask then salvage; what invented values a check can catch; compose token change on A, B, D; replay tests on the four stored compose inputs". Every threshold, every definition of "usable" and every change to salvage below is my own choice and is marked as such. None of them is the ruling's.

**Ships with 32c.** 32c's BRT-ORC-035 message told the planner "to rename fields, do it when composing". Planner ruling 35a removed that message: 035 no longer refuses, the step runs as written and the names go on the asked-for list, which compose is shown beside the step's own fields (ruling 35d, below). Renaming still relies on section 2.1.

**Files to change:**
- `src/orchestrator/row_markers.py`
- `src/orchestrator/compose_checks.py` (new)
- `src/orchestrator/engine.py`: `_RowContext`, `_handle_compose_result`, `_salvage_finished_results`, new `_salvage_after_check`
- `src/lane/context.py`: `guard_rows_block` gains a text form, `guard_text_block`
- `ui/assets/workspace.js`: labels for the new codes
- `docs/errors/error-registry.md` and `docs/user-guide.md`
- the tests and fixtures in section 2.7

No change to `src/prompts/*.py`, `data/skills.yaml` or `data/capability_matrix.yaml`. No new node type and no framework: stdlib `re` and `json`, plus the backstop's `normalise`.

**Assumptions:**
- 32a to 32d are built before this.
  - On the stored data, no job A step produced an urgency, and job D's retry step re-ran the 29 successes.
  - 32c is built (`dc97aa8`, `85c6ea5`). `engine.asked_but_not_produced(run_id)` returns `[{"step", "skill", "field"}]`; since ruling 35a it is filled on the first hit (the step runs as written, no re_plan). This note reads that function as it is.
- Nobody knows whether grok-4.20 writes the new markers. The 32g trial measures it.
- Token-per-row figures are estimates from logged totals (method in 2.6).
- The compose-side checks are not plan validation: they read step results and compose's text, so the new class rule (plan-validation messages reach re_plan in every lane) does not apply to them. They follow the lane rules of 2.2.

## 1. Problem (round-2 evidence)

From docs/design/bench_round2_measurements.md and the stored run DBs (bench/results/2026_11_18/dbs/*_borg_cloud_1.db).

- **Job A** (causes note section 1).
  - Compose was told `classify_emails: 200 rows ... fields: item, bucket, confidence, defer_reason, error`.
  - It typed 10 rows itself (`email_001.txt` ... `email_010.txt`), then put `@@ROWS:classify_emails@@` inside its own open `[`. Code placed the 200 rows as a separate array, so the answer ends `]\n]`.
  - The placed rows carry `item: doc_001` and `bucket`. No step produced an urgency.
  - Compose's rationale: "minor urgency values inferred from typical patterns". Grader: items_answered 0.
- **Job B** (section 4). Step `n1` stored `{"files": <10,435 characters>}` holding all four files. Compose answered with a summary pointing to "the stage result (n1)". The grader reports all four files missing.
- **Job C** (section 5).
  - The first compose reply had no final answer (BRT-ORC-029). That call's `orchestrator_decision` event is missing from `logs`, though outbound rows 167 and 168 both exist (not investigated).
  - The second reply gave bare numbers and filler contradictions. No placement problem.
- **Job D** (section 6). 29 results, 31 failures. Compose ignored the block, typed all 60 rows (1,198 output tokens) and wrote "medium" for the 31 notes with no result (11 right by chance). Grader: 0.60.

**Measured with the grader on the stored compose inputs:**
- Today's salvage scores items_answered 0 on A and on D (rows `{"index","item": doc id,"output":{...}}`). On B it finds no files: the output is JSON-dumped with escaped newlines.
- Placing the stored rows with chosen fields (`file=item, category=bucket` on A, `file=item, risk=bucket` on D):
  - A: items_answered 200, category 0.925 (185 of 200), urgency 0.0.
  - D: items_answered 60, risk 0.4167 (25 of 60).
- B's stored output placed verbatim passes the six checks that run without Docker.
- **D falls from 0.60 to 0.42 once nothing is guessed.** All 11 extra hits in round 2 were guesses. The planner should know this before 32g. How it is reported (planner): no grader change; the win rule reads the grader's figure over all items. The trial report gives, for any list job, items with a result / all items, accuracy over all items, and accuracy over items with a result. Round 2's D is stated as 0.60 as scored, of which 11 correct labels were guesses for notes with no result; 25 of 60 (0.42) without them; 25 of the 29 with a result.

## 2. Design per requirement

### 2.1 Requirement 1: rows with chosen fields and names, and joins

**Marker syntax** (compatible with ruling 21):

```
@@ROWS:<step id>@@                                    ruling 21, unchanged
@@ROWS_TABLE:<step id>@@                              ruling 21, unchanged
@@ROWS:<target> | <entry>, <entry>, ...@@              chosen fields, JSON array
@@ROWS_TABLE:<target> | <entry>, <entry>, ...@@        chosen fields, Markdown table
target := <step id> | <step id> + <step id> [+ ...]    one step, or a join on the item
entry  := <field> | <name>=<field>
```

Examples: `@@ROWS:classify_emails | file=item, category=bucket@@` and `@@ROWS:classify_emails + extract_urgency | file=item, category, urgency@@`.

**Parsing** (`row_markers.parse_target`, new):
- STRICT becomes `@@\s*(ROWS_TABLE|ROWS|OUTPUT)\s*:\s*([^@\n]{1,600}?)\s*@@`, case ignored. The capture limit goes from 200 to 600 characters; step ids keep the 200 limit through `markable()`.
- LOOSE becomes `@@\s*(row|output)`, case ignored.
- Diff hunk headers are masked before LOOSE runs: `@@ -\d+(,\d+)? \+\d+(,\d+)? @@`, both `@@` pairs. Otherwise a context line such as `@@ -1,2 +1,2 @@ output = x` would match `@@ output`.
- The quoted-hit exemption is unchanged.
- **Target resolution**, in order:
  1. The whole trimmed capture equals a listed step id: the ruling-21 form. Ids holding `|` or `+` still work.
  2. Otherwise, the first `|` whose left part, trimmed, resolves as a target: a listed id, or ids joined by `+`, each trimmed and listed. The right part is the spec.
  3. Otherwise, with no spec, the whole capture split on `+`, every part a listed id.
  4. Otherwise the marker is invalid (2.4.6).
- **Spec:** entries split on `,`; each is `field` or `name=field`, trimmed.
  - Names and fields are 1 to 64 characters, with none of `,` `=` `|` `@` `[` `]` `{` `}` or a newline.
  - `output.x` means `x` (ruling 25a). Only top-level output keys are selectable.

**Rows** (`row_markers.rows_for` plus new `select(rows, spec)` and `join(steps)`):
- Pseudo-field `item` is the item's label (below).
- A resolved row gets every spec field in spec order. A field the row lacks is `null`.
- A failed row gets only the item entry and `error`, never another field.
- If any row has `error` and the spec omits it, `error` comes last. A spec may move or rename it (`status=error`).
- **Join** (`a + b`, lead ruling 1a):
  - **Matching and order.** Rows are matched on the item's canonical identity (2.3) and come in the first step's order. Items only in later steps follow, in their own order.
  - **Chosen fields.** For each chosen field f and item i:
    - f's value is taken from the latest listed step that has f and resolved i with a non-null value. This is the retry case, and a later failure never erases an earlier value.
    - If no listed step that has f resolved i, but another listed step did resolve i, f's value is the error code of the latest listed step that has f and failed i (for example `"BRT-ANS-005"`). The reader can then tell a failure from an empty value. If no listed step that has f failed i either, f is `null`.
  - **Rows with no resolution.** An item that no listed step resolved is a failed row: item and `error`, holding the latest failure's code.
  - **Unknown fields.** A field held by none of the listed steps is BRT-ORC-037.

**Item labels** (`row_markers._item_label`, replaces the id part of `_item_id`):
- **Document items** (the run has documents and the item is a part id):
  - lane manifest `names` (the default): the document's `name` as the manifest shows it (`job_a/email_001.txt`), and `<name>#<k>` for part k;
  - lane manifest `ids`: the part id, since compose was never shown names in that mode.
- **Aggregate entries.** An item that is itself an aggregate entry (a dict with `index` and `item`, as in D's retry step) is labelled by its inner `item`, once.
- **Other items** keep ruling 21's rule.
- The grader takes the basename, so `job_a/email_001.txt` scores as `email_001.txt`.
- Labels also apply to the plain `@@ROWS:<id>@@`. This is the one visible change to ruling-21 placements (tests in 2.7).

### 2.2 Requirement 2: a step's whole output

**Marker:** `@@OUTPUT:<step id>@@`, or `@@OUTPUT:<step id> | <field>@@` when the output holds more than one long string field.

**What it places** (`row_markers.long_outputs`, new):
- A long output is a resolved step's output that is a string of 2,000 characters or more, or a top-level string field of that length in a dict output. Only listed long outputs are placeable.
- The text placed is the value `_stage_output_for_prompt(env, "compose")` showed compose, character for character.
- The 2,000 threshold is my own judgement. Under it, a retype costs at most about 8% of the 6,000-token compose cap. B (10,435) does not decide it: any value from about 1,000 to 10,000 catches B.

**Rendering, string answer.** `compose_checks.scan` gives, for each marker, the bracket depth and whether it sits inside a double-quoted string. The scan starts at the innermost open fence, the end of the last closed fence, or the start of the text, and counts quotes only while depth is above 0.
- Outside brackets: the text goes on its own lines with a blank line before and after; backticks directly around the marker are dropped (ruling 21).
- Inside a fence compose opened: the fence is closed before the text and reopened after it (ruling 21's `_table_in_fence`).
- As the bare value of a key in JSON compose wrote (`{"code": @@OUTPUT:n1@@}`): the text is placed as a JSON string, `json.dumps(text)`.
- Inside a quoted JSON string compose wrote (`{"code": "@@OUTPUT:n1@@"}` or `"see @@OUTPUT:n1@@"`): the text is placed JSON-escaped without quotes, `json.dumps(text)[1:-1]`, so the string stays valid.

**Rendering, object or list answer** (decoded): a leaf that is exactly the marker becomes the string, and a marker inside a longer leaf is spliced in. Escaping happens when `_compose_text` writes the object out.

**Single pass:** placed text is never scanned again.

**A child run's result.** An orchestrate step's output is `{"response": <the child's final answer>, "child_run_id": ...}`. If `response` is 2,000 characters or more it is listed, and `@@OUTPUT:<step>@@` places the child's answer verbatim. The parent cannot choose a child's fields: it holds text, not rows.

**An output held back by the lane:**
- In raw_local and all_local, a raw output that fails `guard_envelope` becomes a failed envelope holding a code and document ids only. It is never listed; naming it is BRT-ORC-032, and its stored text is never read.
- With the defaults, a raw output never reaches 2,000 characters (`lane_max_output_chars` is 1,500).
- If an operator raises that setting, a placed raw OUTPUT text goes through `guard_text_block` (copied_rule against every run document, BRT-LANE-009, fail closed), as ruling 21b set for placed rows.
- by_reference is unchanged.
- Placement adds only text compose was shown in the run. The lane never loosens.

### 2.3 Requirement 3: what compose is told

**Identities, "no result" and "no value"** (`compose_checks.ItemModel`, built once per compose):

- **Canonical identity:** the inner item value (the part id for documents).
- **Identity tokens:** the canonical id, plus for documents the label (name or `name#k`) and its basename.
  - **A document id is not an identity of its parts.** `doc_005` or `job_c/doc_05.md` without `#k` matches only a single-part document, whose part id equals its document id.
  - Tokens match whole, case ignored: not preceded by a word character, `#`, `.`, `/` or `-`, and not followed by a word character, `#` or `-`.
- **Value:** anything other than `null` or an all-blank string.
- **No result (what the block tells compose)** (lead ruling, round 2). Item i has no result when:
  - a list step failed it, and
  - no other step resolved it, meaning no other list step resolved i, and no resolved step's output pairs one of i's identity tokens with a value in a JSON object.

  Pairing is the value-level source C4 also exempts (2.4.4). A failures entry naming the item is not a value. Stored figures:
  - A: 0. classify_emails failed 4; the answerer `fix_and_complete_classifications` pairs all four with values.
  - D: 31. The retry re-ran only items already resolved.
  - C: 2. extract_q1_to_q8 failed 8 parts and extract_contradictions resolved 6 of them. The 2 left (`doc_005#6`, `doc_011#6`) failed both steps.
  - D after 32d: if the retry over the 31 resolves 4, the count is 27. A note that fails both steps stays in the set.
- **Rows with no value (what the block tells compose).** For each field that at least one resolved row of some list step holds a value for: the count of items that some list step resolved, and that no list step gives a value for that field.
  - A field no row holds a value for at all is left out: defer_reason on A and D.
  - Stored figures: A none; D none; C 74 for each of the 8 extract_q1_to_q8 fields (75 resolved, one value each), and any_contradictions_or_inconsistencies 23, contradiction_topics 24, conflicting_values_quoted 24 (of 81 resolved).
- **No value for a field (C4's wider set, 2.4.4; not shown to compose).** Per item and per field: item i has no value for f when no resolved row of any list step gives i a value for f. This covers both failed items and items resolved with `null` or `""`. Stored figures:
  - A round 2: bucket 4, confidence 4, defer_reason 200.
  - D round 2: bucket 31, confidence 31, defer_reason 60.
  - C round 2: each extract_q1_to_q8 field 82, any_contradictions_or_inconsistencies 25, contradiction_topics 26, conflicting_values_quoted 26.
  - A round 1: bucket 11, confidence 11, defer_reason 199, urgency 0, category 0.

**Gate.** The block is sent only when the run has at least one list step, one long output, or one asked-but-not-produced entry. Otherwise every compose request stays byte-identical.
- **Shapes that now get a block because of a long output alone:**
  1. an ordinary answerer run whose answer is 2,000 characters or more: reports, code generation as in B, long research or web-search write-ups;
  2. a parent run with an orchestrate step whose child answer is that long;
  3. any mix of these with list steps (those already got a block).
- **Worst case for those runs goes from 6 to 9 compose posts.** With a block the cut-off pointer retry always runs (ruling 21), so up to 3 posts in each of up to 3 invocations: the first, the 029 re-ask and the repair re-ask. Without a block it was 2 posts each.
- The block adds about 130 tokens per compose call, about US$0.00016 on grok-4.20, and C3 can fire on these runs (2.4.3).
- The sentence "Do not retype rows or long outputs yourself" and the cut-off pointer stay although C2 is dropped (planner): they are the only guard left against a retype, which now costs tokens but no re-ask.
- I keep the 2,000 gate: B needs it, and the cost is the extra posts and that token line.
- No existing test has a step output over 2,000 characters feeding compose while asserting compose's user message. I searched every `* <n>` string builder in tests/ and every `.user ==` and `.user.startswith` assertion. The one 50,000-character text, in test_privacy_lane_by_reference_review.py, is a document, not a step output.

**Exact wording.** Lines in angle brackets are filled in by code. A bullet, line or section with nothing to list is left out, except the "Items with no result" line, which is always shown when list steps exist.

```
Mode: compose. Reply with the final answer.
The reader sees only your answer. They cannot see any step, step id or step result, so never refer them to one: everything they need must be in your answer, written by you or placed by the code.
The code can place stored results for you, exactly as stored. Do not retype rows or long outputs yourself; naming a few items in your own text is fine.
- Rows: write @@ROWS:<step id>@@ where a step's rows belong (a JSON array, one row per line), or @@ROWS_TABLE:<step id>@@ for a Markdown table. Put the marker on its own line, outside any brackets you write: the code writes the brackets. To choose fields, their order and their names, add them after a bar: @@ROWS:<step id> | <name>=<field>, <field>@@ (for example @@ROWS:s1 | file=item, label=bucket@@). To join steps on the item, list them with +: @@ROWS:<step id> + <step id> | ...@@; when both give a field, the later step's value is used. Use only the fields listed for those steps. An item with no result keeps its error and gets no other value.
- Whole output: write @@OUTPUT:<step id>@@ where a step's whole output belongs; the code writes it in word for word.
Steps with rows:
- <id>: <n> rows, <input order|stored order>, <k> failed; fields: <item (file name)|item (document id)|item>, <field>, ..., error
Steps with a long output:
- <id>: <n> characters (field <field>)
Items with no result (a step failed them and no other step gave them a value): <m>. Never give them a value: say they have no result.
Rows with no value (a step returned the item but left the field empty): <field> <m>, <field> <m>. Leave those fields empty; do not supply a value yourself.
Asked for but not produced (the plan asked a step for these fields and the step does not return them under these names):
- <step> (skill <skill>): <name>, <name>. The step's own fields: <item (file name)|item (document id)|item>, <field>, ...
- <step> (skill <skill>): <name>. The step has no rows to place, so these were not produced.
For each of these names, choose from the step's own fields by name alone: where one of them holds what the name asks for, place it under that name with a rows marker, for example @@ROWS:<first step with rows> | <its first name>=<field>, ...@@. Where none does, leave the name out and tell the reader it was not produced. Do not supply values for these fields yourself.
```

Further rules:
- The "Rows with no value" line lists only fields with a count above 0, in step order.
- With two or more long fields in one step, the line reads `- n1: 10,435 characters (field files; write @@OUTPUT:n1 | files@@)`.
- The asked-but-not-produced section comes from `engine.asked_but_not_produced(run_id)`, grouped by step in list order.
- **Fitting (planner ruling 35d).** Fitting is decided from names only, and by compose, not by the code. Each asked name is listed beside the step's own field names, exactly as its "Steps with rows" line shows them, without `error`; compose chooses which of them, if any, holds what the name asks for, and places it under that name with a rows marker. The code writes no pairing (no `file=item`, no `category=bucket`), reads no values, and guesses nothing. A step that is not in "Steps with rows" (it has no rows to place) gets the "no rows to place, so these were not produced" line. The last line's example names the first asked step that has rows and its first asked name; with no such step it reads "Do not supply values for these fields yourself: tell the reader they were not produced." C4 case 2 is unchanged: a value compose types itself under an asked name, for an item of that step, with no stored source, is still caught.

**Golden texts** (everything after the first line).

Job A, round 2, with the asked-for list the plan gives (the fixture supplies it, because the recorded run predates 32c).
```
Steps with rows:
- classify_emails: 200 rows, input order, 4 failed; fields: item (file name), bucket, confidence, defer_reason, error
Items with no result (a step failed them and no other step gave them a value): 0. Never give them a value: say they have no result.
Asked for but not produced (the plan asked a step for these fields and the step does not return them under these names):
- classify_emails (skill classify_into_buckets): file, category, urgency. The step's own fields: item (file name), bucket, confidence, defer_reason
For each of these names, choose from the step's own fields by name alone: where one of them holds what the name asks for, place it under that name with a rows marker, for example @@ROWS:classify_emails | file=<field>, ...@@. Where none does, leave the name out and tell the reader it was not produced. Do not supply values for these fields yourself.
```

Trial 32g, job A, round-2 data (ruling 35d; pinned in `tests/orchestrator/test_compose_placement.py` and, end to end, `tests/orchestrator/test_skill_output_schema_check.py`): the first plan asked for urgency through `extra_fields`, so urgency is produced and only `file` and `category` are listed.
```
Asked for but not produced (the plan asked a step for these fields and the step does not return them under these names):
- classify_emails (skill classify_into_buckets): file, category. The step's own fields: item (file name), bucket, confidence, defer_reason, urgency
For each of these names, choose from the step's own fields by name alone: where one of them holds what the name asks for, place it under that name with a rows marker, for example @@ROWS:classify_emails | file=<field>, ...@@. Where none does, leave the name out and tell the reader it was not produced. Do not supply values for these fields yourself.
```
The three fields are what `validate_plan` reports: the plan's output_schema `{file, category, urgency}`, none of which `classify_into_buckets` returns.

Job B: `Steps with a long output:` / `- n1: 10,435 characters (field files)`.

Job C:
```
Steps with rows:
- extract_q1_to_q8: 83 rows, input order, 8 failed; fields: item (file name), tamarack_pilot_end_date, kessler_falls_loading_bays, group_compliance_contact, project_osprey_approved_budget, audit_logs_retention_days, refrigeration_units_vendor, emergency_shutdown_codeword, pell_harbour_crane_inspection_frequency, error
- extract_contradictions: 83 rows, input order, 2 failed; fields: item (file name), any_contradictions_or_inconsistencies, contradiction_topics, conflicting_values_quoted, error
Items with no result (a step failed them and no other step gave them a value): 2. Never give them a value: say they have no result.
Rows with no value (a step returned the item but left the field empty): tamarack_pilot_end_date 74, kessler_falls_loading_bays 74, group_compliance_contact 74, project_osprey_approved_budget 74, audit_logs_retention_days 74, refrigeration_units_vendor 74, emergency_shutdown_codeword 74, pell_harbour_crane_inspection_frequency 74, any_contradictions_or_inconsistencies 23, contradiction_topics 24, conflicting_values_quoted 24. Leave those fields empty; do not supply a value yourself.
```

Job D:
```
Steps with rows:
- classify_notes: 60 rows, input order, 31 failed; fields: item (file name), bucket, confidence, defer_reason, error
- retry_failed_notes: 29 rows, input order, 0 failed; fields: item (file name), bucket, confidence, defer_reason
Items with no result (a step failed them and no other step gave them a value): 31. Never give them a value: say they have no result.
```

**Cut-off retry.** ROWS_POINTER becomes `Mode: compose. Your last reply was cut off at the length limit. Do not type out a step's rows or long output: put @@ROWS:<step id>@@ or @@OUTPUT:<step id>@@ where they belong and the code writes them in. Reply with {"response": "<the final answer>"} only.`, followed by the "Steps with rows" and "Steps with a long output" lines.

**Law 1:** the block is content in the user message; governance reaches compose through `build_system_prompt()` unchanged.

### 2.4 Requirement 4: checks after compose

**Compose's own text** (lead ruling 9). It is the reply value with every valid marker removed, kept in its structure:
- a string answer stays a string;
- an object or list answer is the decoded value, walked as a tree, with leaves never joined.

Inside any string (the answer, or a leaf), JSON values are found with a `raw_decode` scan, as the grader does. Markdown table rows are lines starting with `|`. Every other line is a "line".

**Pairs** (`compose_checks.pairs`). An identity-value pair is:
- (a) the identity as a scalar value in a JSON object, paired with each other scalar value of that object (key kept);
- (b) the identity as one cell of a table row, paired with each other cell (key = the column header);
- (c) the identity in a line of at most 160 characters outside (a) and (b), paired with the one cell left once the leading bullet is dropped, the identity removed and the line split on `|`, `,`, `:`, `;`, tab and ` - ` (no key; see 2.4.4 for "single");
- (d) the identity as an element of a JSON list that is the value of a key, paired with that key (the grouped form `{"medium": [...]}`).

C4 reads (a) to (d). (C2, dropped, read (a) to (c).)

**Order inside `_handle_compose_result`**, only when the block was sent:
1. Pick the answer (`_compose_text`).
2. Validate markers: BRT-ORC-032 and BRT-ORC-037 (2.4.6).
3. Run C1.
4. Build the own text.
5. Run C3 and C4.
6. Collect every problem.
7. None: place (with the guards) and return.
8. Some, with the repair re-ask unused: send it.
9. Otherwise salvage (2.4.5).

**One repair re-ask per run** covers 032, 037, 038, 040 and 041 together, so the worst case stays at 3 invocations and 9 posts. A lane refusal (BRT-LANE-009) is not a check: it fails closed.

**`action: fail`** (lead ruling 16). On a declined rationale, steps 2 and 3 and placement run, as ruling 21 does today. C3 and C4 do not run: a decline is not an answer, and a repair message asking for one would overrule compose's choice. The run stays "declined".

**Repair message (exact).** After the problem lines and the "Fix these" line, it repeats the block sent with the first compose call, from its second line ("The reader sees only your answer...") to its last line, byte for byte. The `Mode: compose. Reply with the final answer.` line is not repeated.

```
Mode: compose. Your last reply cannot be used as it is:
- <one sentence per problem, in the order 032, 037, 038, 040, 041>
Fix these and reply with {"response": "<the final answer>"} only.
<the block, from its second line to its last line>
```

#### 2.4.1 C1: a rows marker that is an element of a list holding other elements (BRT-ORC-038)

Lead ruling 3.

**Nested** when:
- **String answer:** the innermost open bracket before a valid ROWS or ROWS_TABLE marker is `[`, and between that `[` and its matching `]` (or the end of the scan region) there is anything other than the marker, whitespace and commas. This is A's case.
- **Object or list answer:** a marker leaf is one element of a JSON list that holds other elements.
- **ROWS_TABLE marker:** the nearest non-blank line above it starts with `|`.

**Not nested, and how each places:**
- `{"results": @@ROWS:x@@}` (the marker as a key's value, innermost bracket `{`): placed as the compact JSON array, so the result is valid JSON.
- `{"results": "@@ROWS:x@@"}` (the marker as the whole content of a quoted value): the quotes are dropped and the compact array placed.
- A ROWS marker inside a longer quoted string: the compact array is placed JSON-escaped, `json.dumps(...)[1:-1]`.
- `[ @@ROWS:x@@ ]` (a list holding only the marker): compose's brackets are absorbed. Code drops them and places its own array, so there is no `[[...]]`.
- In an object answer, a list whose only element is the marker becomes the rows list.
- A marker alone in its own ```json fence: ruling 21 placement.

**Measured:** A's reply is nested.

**False positives:** closed prose brackets ("[see below]", Markdown links, "[1]") close before the marker. An unclosed `[` in prose followed by text is a hit; I judge that rare, and it costs one re-ask.

#### 2.4.2 C2: compose retyped stored data (BRT-ORC-039): DROPPED by the planner

**Dropped (planner reading (4), 18-Nov-2026).** C2 is not built and BRT-ORC-039 stays unused; 037, 038, 040 and 041 keep their numbers. The planner's reasons: a faithful retype is not a wrong answer; firing costs a compose call (about US$0.022 to 0.029) to save about US$0.01; values with no source are C4's job, and a cut-off reply is the pointer retry's (ruling 21); C2 had the most false-positive forms. What stays: the block sentence "Do not retype rows or long outputs yourself", the cut-off pointer, and the join-marker hint, which moves into C4's case-1 sentence (2.4.4). Job D is caught by C4 alone (2.7). The analysis below is kept as history only; nothing in it is built.

Lead ruling 1b. **Fired** (as designed) when, for a list step S with at least 10 resolved items, more than half of S's resolved items appear in compose's own text in a pair of form (a), (b) or (c) whose value equals one of that item's own stored values.
- "Stored value" means any field value some list step resolved for that item, so a typed join counts for both steps.
- Equality is case-insensitive and trimmed; numbers compare as numbers; booleans and the identity itself are never values.
- A reformatted answer with no stored values next to identities does not fire. The grouped form (d) is not read.

**Thresholds (my own choices):**
- "More than half": the work-order brief named it.
- **Denominator: S's resolved items, not all of S's items.** With all items, a step with half or more failures could never fire, since a failed item has no stored value to pair with. Stored D would give 29 of 60 and miss. The lead's ruling text says "the step's items", so the lead must confirm (section 6, question 3).
- **Floor: at least 10 resolved items,** matching the denominator. Below that, a reader expects prose naming each item, and no bench job has fewer than 29 resolved items in a step.
- **Lines of 160 characters** are a GUESS: they separate one-line rows (D about 43 characters, A about 62) from prose paragraphs written as one long line.
- Not applied to steps whose identities are all numbers or shorter than 3 characters.

**After the repair re-ask (my own choice, section 6, question 2):** if C2 is the only problem left in the second reply, the reply is accepted, BRT-ORC-039 is recorded on the run as a warning, and answer_kind is "composed". C2 finds only stored values: a faithful retype holds no invented value and costs only tokens, and salvage would destroy a format the operator asked for that no marker can write, such as CSV. With any other problem, salvage applies.

**Measured** (r2.py; numerator over S's resolved items):

| Reply | Step | Pairs with own stored value | Fires |
|---|---|---|---|
| A round 2 (recorded) | classify_emails | 9 of 196 (email_004 has no stored value) | no |
| D round 2 (recorded) | classify_notes | 29 of 29 | yes |
| D round 2 (recorded) | retry_failed_notes | 29 of 29 | yes |
| C round 2 (recorded) | both steps | 0 | no |
| Round-1 A, stand-in typed join (`{file, category, urgency}` from the stored rows) | classify_emails | 189 of 189 | yes |
| Round-1 A, stand-in typed join | extract_urgency | 200 of 200 | yes |
| Round-1 A, same rows as CSV | classify_emails | 189 of 189 | yes |
| Round-1 A, same rows as one bullet per email | classify_emails | 189 of 189 | yes |
| Round-1 A, grouped `{"billing": [...]}` | both steps | 0 | no |
| D grouped `{"low":[...],"medium":[...],"high":[...]}` | classify_notes | 0 | no |
| D as CSV | classify_notes | 29 of 29 | yes |
| D, lines "needs a human to label it low, medium or high" | classify_notes | 0 | no |

**False positives:**
- (i) A few urgent files named next to a placed block: about 2.5%, no hit.
- (ii) A top-20 of 60: 33% at most, no hit.
- (iii) A subset over half listed with its stored labels (the 40 high-risk notes): a hit. The marker cannot filter; it costs one re-ask, and the second reply is accepted with a warning.
- (iv) **CSV of stored data, which no marker can write: a hit.** Same cost; accepted on the second reply.
- (v) One bullet per item with its stored label: a hit. The join marker can express it as a table, so the re-ask is right.
- (vi) The typed join (round 1): a hit, rightly, since the join marker expresses it.
- (vii) Grouped answers: no hit.
- (viii) Prose paragraphs per document: lines over 160 characters, no hit.
- (ix) Cited sources: no stored value next to them, no hit.
- (x) **D after 32d** (a case I constructed, not stored). Suppose the retry resolves 4 of the 31 and compose writes one line naming 3 of those 4 with their labels ("Rechecked: note_04.txt low, note_07.txt high, note_09.txt medium"). No hit:
  - retry_failed_notes has 4 resolved items, under the 10 floor, so C2 does not apply to it;
  - classify_notes sees 0 of its 29 resolved items, because the 3 were resolved by the retry, not by classify_notes.

#### 2.4.3 C3: a long output neither placed nor present while the answer points at it (BRT-ORC-040)

**Kept narrow (planner).** It fires for a listed long output L when all three hold:
1. no OUTPUT marker placed L;
2. L is not present: fewer than 50% of L's distinct normalised word 8-grams appear in compose's own text (a faithful retype is not a wrong answer, the planner's reason for dropping C2);
3. compose's own text points at a step: it says `step result(s)` or `stage result(s)` (also `step_result(s)` and `stage_results`), or names a step id of the run as `(id)`, as `` `id` ``, or after `step `, `stage ` or `node `.

- **B:** not placed, coverage 0.000, pointer "the stage result (n1)". It fires.
- The 50% coverage figure is my own choice. Condition 2 is my reading of "not placed" in the planner's wording: without it a faithful retype that also says "(n1)" would fire.
- Precision is acceptable only because of condition 3. **Recall is low, a stated limit (user guide):** C3 does not catch a long output dropped without such a pointer; only the 2.3 wording covers that case.
- **Known false positive:** a deliberate summary that says "Based on the stage results". The re-ask lets compose keep its summary and drop the pointer.

#### 2.4.4 C4 (requirement 5): a value with no source (BRT-ORC-041)

`compose_checks.invented_values(own, model, asked)`. A pair (a) to (d) for item i with value v is a hit in either case below.

**Case 1: a field no step gave this item.** All of these hold:
- v is a single whole stored value of some field f. The vocabulary of f is its whole stored values (string values of at most 40 characters), never words split out of them.
- i has no value for f (C4's wider set, 2.3).
- v is not one of i's own stored values in any field.
- No resolved step output pairs i with v. Pairing means a JSON object in any resolved output (an answerer's included, never a failures entry) holding one of i's identity tokens and v. This is the value-level exemption: A's fix step pairs `doc_004` with "shipping" and 5.
- For a line (form c), "single" means that once the identity and separators are removed, exactly one cell is left and it equals a vocabulary value. "needs a human to label it low, medium or high" leaves no such cell, so it is not a hit.

**Case 2: an asked-but-not-produced field** (planner addition, field-level). All of these hold:
- The pair's key (form a, or the column header in form b) equals, case ignored, a field of a BRT-ORC-035 entry in `asked_but_not_produced` (an output field; a BRT-ORC-036 entry is a key nothing takes and is never read here, review round 1) for a step that holds i. (The note's "or for any step if i is in no list step" is not built: an item in no list step has no identity the checks can match.)
- v is a value and not empty-like.
- v is not the identity, not one of i's stored values (so `category: billing`, equal to the stored bucket, is a rename and passes), and not paired with i in any resolved step output.
- Forms (c) and (d) carry no key and are not checked for case 2 (a stated limit).

**Empty-like values** (never a hit): null, "", "-", "n/a", "none", "unknown", "no result", "no label", "no value", "missing", "error", "failed", "not produced", or a code matching `BRT-[A-Z]+-\d{3}`.

**Sentences** (in this order, under 041):
- Case 1: `You gave values to <k> items that no step gave a value for that field (for example <id1>, <id2>, <id3>). Do not give them values: in placed rows they show their error or stay empty, and in your own text say they have no result or no value. To show the stored values, place the rows instead of typing them: @@ROWS:<steps> | <name>=<field>, <field>@@.`
  - `<steps>` is the run's list steps in run order joined with ` + ` (one step: its id alone). This is the join-marker hint that was C2's (planner); for D it reads `@@ROWS:classify_notes + retry_failed_notes | <name>=<field>, <field>@@`.
- Case 2: `You gave values for <field> to <k> items (for example <id1>, <id2>, <id3>), but no step produced <field>. Leave the field out, or tell the reader it was not produced.`

Identities are written in the form compose was shown, the first three in input order.

**Measured:**
- D recorded reply: 31 hits (case 1). The D grouped and CSV forms also give 31.
- A recorded reply, with the 32c list supplied: 9 hits (case 2, urgency). email_004's typed 5 and "shipping" are exempt, because the fix step pairs doc_004 with both.
- C: 0.
- Round-1 A stand-in join: 0. Every typed value is one of the item's own stored values.

One hit is enough to fire: such a value has no source.

#### 2.4.5 Salvage after a failed repair (my own choice; lead ruling on question 1: S2 yes)

Neither change touches the other salvage paths: 030, 029 twice, a compose call that raises, and a cut pointer retry. `_salvage_finished_results` stays as it is; a new `_salvage_after_check(results, lane, reply, problems)` wraps it.

**S2: keep what code placed, drop what compose typed.** It applies to salvage after a failed repair re-ask (codes 032, 037 to 041).
- The second reply's valid markers that also have valid specs are placed from stored results, with the 21b guard on rows and `guard_text_block` on OUTPUT text.
- The answer is `SALVAGE_NOTE`, then the placed blocks (each under `## <step id>`), then the existing sections for finished steps that were not placed.
- Compose's own text is dropped. A marker that failed 032 or 037 is skipped.

**S1: long string fields verbatim.** It applies only to salvage after a failed check 038 to 041 (lead ruling 5).
- A finished, unplaced, non-aggregate step with no answer-text key, whose output holds one or more long string fields, is written as those strings verbatim under `## <step id>`, with `### <field>` when there are several. Its other fields follow as JSON.
- The raw rule is unchanged: a raw step is withheld unless `lane_guarded`.
- A shown raw long field goes through `guard_text_block` (BRT-LANE-009, fail closed), the same guard OUTPUT gets.

**Existing tests touched by S1 or S2: none.**
- `test_invalid_marker_twice_is_salvaged_with_032`: its second reply has no valid marker, so S2 places nothing; the test still passes, but the salvage now writes the list step in the plain marker's form (flat fields, file-name labels, under `## <step id> (n item(s) finished, k failed)`), not the nested index/item/output shape. This holds for every salvage after a failed repair (032 and 037 included), not only after 038, 040 or 041; S1 (long fields verbatim) is the part that waits for 038, 040 or 041.
- The cut-pointer, 030 and 029 salvage tests do not take this path.
- The one behaviour change against ruling 21: a 032 second reply holding one valid and one invalid marker now keeps the valid block. No current test covers that case.

**Run record:**
- The run is "salvaged". The warning event carries every problem code of the second reply; its headline code is the first in the order 032, 037, 038, 040, 041.
- **List steps with no usable marker (planner).** In this salvage a finished list step that no marker placed is written in the plain marker's form (`@@ROWS:<id>@@`: flat fields, file-name labels, failed rows as item and `error`), not the nested index/item/output shape, under `## <step id>`, with the 21b guard. The other salvage paths (030, 029 twice, a raising compose call, a cut pointer retry) keep today's form.
- Bench scoring of salvaged runs is unchanged.
- Placed blocks in salvage now get the 21b guard. That narrows, but does not close, 21b's open question.

#### 2.4.6 Marker errors and near-misses (lead ruling 11)

Two codes, an exact sentence per case. The re-ask echoes only listed step ids and compose's own field words, cut to 40 characters.

| Case | Code | Sentence |
|---|---|---|
| target names no listed step (also any part of a join) | 032 | `A marker names no step the code can place. Use only the step ids listed below.` |
| OUTPUT on a list step | 032 | `@@OUTPUT:<id>@@ names a step that has rows, not a long output: use @@ROWS:<id>@@ for it.` |
| ROWS or ROWS_TABLE on a long-output step | 032 | `@@ROWS:<id>@@ names a step that has a long output, not rows: use @@OUTPUT:<id>@@ for it.` |
| a near-miss (a LOOSE hit that is not STRICT: `@@Row:x@@`, `@@OUTPUTS:x@@`) | 032 | `A marker is misspelled. Write it exactly as shown below, with two @ signs at each end.` |
| a marker over 600 characters (a LOOSE hit whose text up to the next `@@` on its line is longer) | 032 if no listed id follows the colon, else 037 | `A marker is longer than 600 characters; name fewer fields.` |
| unknown field (for a join, a field in no listed step) | 037 | `The marker for <target> names fields that step does not have: <names>. Its fields are: <fields>. No step produced the others; do not give values for them.` |
| duplicate output name | 037 | `The marker for <target> uses the name <name> twice; each name may appear once.` |
| a bar with nothing after it | 037 | `The marker for <target> has a bar but no fields after it.` |
| spec in brackets (`| [a, b]` or `| {a}`) | 037 | `The marker for <target> puts its fields in brackets; write them plainly after the bar: @@ROWS:<target> \| name=field, field@@.` |
| an entry over 64 characters or holding a forbidden character | 037 | `The marker for <target> has a field entry the code cannot read (<entry>): use plain names with no , = \| @ brackets or line break.` |

The `\|` in the last two sentences is table escaping only; the sentences carry a plain `|`. BRT-ORC-032's registry meaning widens to "a marker names no step, or the wrong kind of step, the code can place".

### 2.5 Requirement 5: what a check can and cannot catch

**Caught:**
- a single stored-vocabulary value next to an item with no value for that field, in JSON, a table, a short line or a group key (C4 case 1; D's 31);
- a value under an asked-but-not-produced field name in JSON or a table (C4 case 2; A's 9 urgencies once the 32c list exists);
- a marker naming a field no step produced (037);
- typed rows around a marker (C1).

**Not caught:**
- values under a new key name that is neither stored nor asked for (C2 is dropped);
- altered values for items that have results;
- bulk retyping of stored values: it costs tokens only, and no check fires (C2 dropped);
- an asked-for field in line or group form (no key);
- prose inventions;
- C's filler contradictions (selection, not invention) and its missing units (copied from the worker);
- a long output dropped without a pointer;
- a multi-part document named without `#k`;
- steps whose identities are numbers or shorter than 3 characters.

### 2.6 Requirement 6: compose token change on A, B and D

**Prices:** grok-4.20 costs US$1.25 per million input tokens and US$2.50 per million output (data/capability_matrix.yaml lines 57-58). Check against A's logged dollars: 23,226 x 1.25e-6 + 377 x 2.5e-6 = 0.029975, which matches.

**Method** (lead ruling 14):
- Whole replies run 0.22 to 0.32 tokens per character of reply JSON: B 271/1,219 = 0.22, A 377/1,437 = 0.26, C 168/617 = 0.27, D 1,198/3,702 = 0.32. Rows tokenize denser than prose.
- I take prose at 0.25 (inside the 0.22 to 0.27 of the prose-only replies B and C) and give the rest of the logged output to typed rows. Prose sizes: A 649 characters, D 572, C 613.
  - A: prose about 162 tokens; 10 rows = 377 - 162 = 215, about 21.5 per row.
  - D: prose about 143 tokens; 60 rows = 1,198 - 143 = 1,055, about 17.6 per row.
- Input: the new block against today's, at characters/4, is about +290 tokens on A (with the asked-but-not-produced section), about +200 on D and +131 on B (which had no block). That is about US$0.00016 to 0.00036 per call.

| Job | Logged compose call | Projected, placement on the first reply | Output change | Net per compose call |
|---|---|---|---|---|
| A | 23,226 in / 377 out, US$0.0300 | prose 162 + marker about 20 = about 182 out | -195 tokens, -US$0.00049 | +US$0.00036 input: **-US$0.00013** |
| A, correct retype | 162 + 200 x 21.5 = about 4,462 out (round 1's direct arm: 7,085 for 200 rows, on another model) | about 182 out | -4,280 tokens | **-US$0.0107** |
| B | 10,521 in / 271 out, US$0.0138 | GUESS about 120 out | -151 tokens, -US$0.00038 | +US$0.00016: **-US$0.00022** |
| B, correct retype | 10,820 escaped characters x 0.22 to 0.32 = GUESS 2,400 to 3,500 out | about 120 out | -2,280 to -3,380 tokens | **-US$0.0057 to -US$0.0085** |
| D | 17,488 in / 1,198 out, US$0.0249 | prose 143 + join marker about 25 = about 168 out | -1,030 tokens, -US$0.00258 | +US$0.00025: **-US$0.00233 (-9.4% of the call; -2.0% of D's US$0.1183 run)** |

- When a check fires, the run pays one more full compose call: about 23.5k tokens in on A (about US$0.029) and about 17.7k on D (about US$0.022). On round-2 behaviour, then, the design costs about US$0.02 to 0.03 more per run, for an answer with no invented values.
- Compose input is not trimmed (ruling 21): A's stage results are about 11k tokens and D's about 7.8k. That cost stays.

### 2.7 Requirement 7: tests

**Fixtures** (test inputs only):
- `tests/fixtures/extract_round2_compose.py` reads the four git-ignored DBs read-only (following `extract_stage1_job_a_replay.py`) and writes `tests/fixtures/round2_compose_{a,b,c,d}.json`. Each fixture holds:
  - `source` and `instruction`;
  - `lane`: mode, manifest `names`, and documents with id, name and part ids, from the manifest block;
  - `steps`: node type, data scope, status and output. A, C and D take these from the stage results of the first compose request; B from `envelopes.inbound_payload`;
  - `recorded_replies`. C's first reply is reconstructed as `{}`, because it was not logged;
  - `recorded_quality`;
  - `asked_but_not_produced`, constructed: for A, the three entries of 2.3, the list 32c would hold after a repeated fault (the recorded runs predate 32c); empty for the others.
- The script asserts that no D canary appears and that A and D hold only ids, names and model-written fields.
- Round-1 A uses the existing `tests/fixtures/stage1_job_a_replay.json`.

**Harness.** `tests/orchestrator/test_compose_placement_replay.py` calls `_handle_compose_result` with envelopes from the fixture, a LaneContext from the fixture manifest (documents with placeholder text), a temp DB, scripted re-asks, and `asked_but_not_produced` monkeypatched to the fixture's list. Scoring uses `bench.run_bench.score_classification` and `bench.checks.run_checks(answer, with_docker=False)`.

**"Usable"** (my own definition, in the grader's terms):
- **A:** a list of 200 objects with `file`; items_answered 200; category 0.925; urgency 0.0, with no urgency key in any row; no `@@`. Accuracy over both fields is 0.0 by construction on the stored data.
- **B:** the six non-Docker checks pass, and the placed text equals the stored output byte for byte.
- **C:** the answer equals the recorded one byte for byte, composed, with the fact score at 6 of 8.
- **D:** items_answered 60; risk 0.4167; every risk equals the stored bucket; the 31 no-result rows have `error` and no risk.

**Tests (red before code):**

A
1. Block golden text (2.3).
2. The recorded reply gives BRT-ORC-038 and BRT-ORC-041 (9 hits, case 2). The repair message is exact, with both sentences.
3. Compliant re-ask (`Here are all 200 emails. No step produced an urgency, so none is given.\n\n@@ROWS:classify_emails | file=item, category=bucket@@`): composed, usable (A).
4. Nested again but with that spec: salvaged (S2), same score as test 3, no typed row left.
5. First reply `@@ROWS:classify_emails | file=item, category=bucket, urgency@@`: BRT-ORC-037 with the exact unknown-field sentence; a compliant second reply is composed.
6. Nested twice with no spec: salvaged; S2 places 200 rows with `item` holding names. The grader gives items_answered 0 (stated limit).

B
1. Block golden text.
2. The recorded reply gives BRT-ORC-040.
3. `The four files follow.\n\n@@OUTPUT:n1@@`: composed, usable (B).
4. The recorded reply again: salvaged; S1 writes the files verbatim; usable (B).
5. A summary with no pointer: composed. This pins the recall limit.
6. `{"files": "@@OUTPUT:n1@@"}` written as text inside a ```json fence: the placed value is the JSON-escaped text, and `json.loads` of the fence equals `{"files": <stored text>}`.
7. The same marker as an object-answer leaf: the leaf becomes the stored text.

C
1. Block golden text. It says "Items with no result ...: 2" and gives the "Rows with no value" counts 74, 23, 24, 24; it never tells compose that 82 parts have no result.
2. `{}` gives 029. The recorded second reply fires no new check, and the answer is unchanged.

D
1. Block golden text.
2. The recorded reply gives 041 alone (C4, 31 hits, case 1) in one exact message: D is caught by C4 alone. The 041 sentence carries the join-marker hint naming both steps (`@@ROWS:classify_notes + retry_failed_notes | <name>=<field>, <field>@@`); its examples are the first three no-result notes in input order (job_d/note_04.txt, job_d/note_07.txt, job_d/note_09.txt, the labels compose was shown).
3. Compliant re-ask `@@ROWS:classify_notes + retry_failed_notes | file=item, risk=bucket@@`, the marker the 041 sentence suggests: composed, usable (D).
4. Typed again plus that marker: C4 fires again (31), so the run is salvaged. S2 is usable (D) and gives no "medium" to a no-result note.
5. Typed again with no marker and no invented values (the 29 stored rows only): no check fires, composed (a faithful retype is accepted).
6. Typed again with no marker and the 31 "medium": salvaged; the list steps are written in the plain marker's form. No row of a no-result note has a risk; the grader gives items_answered 0 (stated limit).
7. D after 32d, constructed: the retry over the 31 resolves 4. The block says "Items with no result ...: 27". A line naming 3 of the 4 with their labels fires nothing.

Round-1 A (from stage1_job_a_replay.json)
1. A stand-in typed join reply fires nothing: every typed value is one of the item's own stored values (no 041), and C2 is dropped.
2. Compliant `@@ROWS:classify_emails + extract_urgency | file=item, category, urgency@@`:
   - 200 rows in document order;
   - `category` and `urgency` equal extract_urgency's stored values (the later step wins);
   - the 11 items classify_emails failed have no `error`, because extract_urgency resolved them.
3. Grouped reply: nothing fires.
4. CSV reply: nothing fires, composed.
5. Join error code in a field: `@@ROWS:classify_emails + extract_urgency | file=item, bucket, urgency@@`.
   - The 11 items classify_emails failed carry `"bucket": "BRT-ANS-005"` (their stored failure code), not `null`, and their urgency from extract_urgency.
   - The row has no row-level `error`, because extract_urgency resolved them.
   - An item resolved with `bucket: null` would keep `null`; a unit test covers that case.

**Unit tests** (`tests/orchestrator/test_compose_placement.py`):
- **Parsing:** ids holding `|` or `+` match whole; join parsing; the 600-character capture; each row of the 2.4.6 table with its exact sentence; the diff hunk mask (`@@ -1,2 +1,2 @@ output = x` is not a hit).
- **Rows:** a failed row has only the item and `error`; `error` comes last; join precedence (a later null does not overwrite; a later failure keeps the earlier value). A field whose source step failed the item while another listed step resolved it carries the source step's error code; with no failure behind it, it is `null`. An item no listed step resolved is a failed row.
- **Labels:** names, ids mode, `name#k`, a nested entry, and a document id never matching a part of a multi-part document.
- **C1 placements:** `{"results": @@ROWS:x@@}` gives valid JSON; the quoted value; `[ @@ROWS:x@@ ]` absorbed; the nested list in an object answer.
- **OUTPUT rendering:** all four positions.
- **False-positive cases** of 2.4.1, 2.4.3 and 2.4.4, each pinned to fire or not as stated. C2's cases are not built.
- **"No result" and "rows with no value"** on hand-built steps: a failed item resolved by another list step leaves the no-result count; an item failed by both steps stays; an answerer pairing the item with a value removes it; a field with no value in any row is left out of the "Rows with no value" line.
- **`action: fail`** with typed rows: placed, no C3 or C4, declined.
- **Post counts:** the worst case of 9 posts, and 6 to 9 posts for an answerer run with a 2,500-character output.

**Lane:**
- raw_local OUTPUT of a raw step holding a 120-character passage (setting raised) gives BRT-LANE-009;
- S1 on a raw long field gets the same guard;
- an OUTPUT naming a held-back step gives 032, and its stored text appears in no request and no answer;
- ids mode: no name in any block, re-ask or row;
- plan and re_plan bodies are unchanged.

**Existing tests that change** (lead ruling 7, round-2 point 3): thirteen test functions, each with its reason (the 13th, found in the build, is listed last).

Names in rows. Each name was in the manifest compose received (`LaneContext.manifest_block` shows names unless `lane_manifest` is `ids`), so a placed name adds nothing compose had not seen. In raw_local that manifest also goes to the cloud planner with names.
1. `tests/orchestrator/test_row_markers.py::test_raw_item_is_part_id_or_index`: the raw `doc_001` row becomes the document's name under manifest `names` and stays `doc_001` under `ids`. A non-part item stays as the index. The case grows to both modes, and the fake `_Lane` gains `docs` and `settings.manifest`.
2. `tests/e2e/test_compose_row_markers.py::test_same_passage_in_compose_text_behaves_as_today`. Its `all(r["item"].startswith("doc_"))` is ruling 21 §9 test 7's "Rows carry part ids only" pin. It becomes: rows carry the manifest's names (`email_001.txt`, `email_002.txt`), or part ids under `lane_manifest: ids`, and never document text. An ids-mode case is added. The "no text" half of the pin is unchanged.
3. `tests/e2e/test_compose_row_markers.py::test_replayed_stage1_job_a_completes_with_full_rows`:
   - `[r["item"] for r in rows] == docs` compares names;
   - the line `fields: item, urgency, category` becomes `fields: item (file name), urgency, category` with ", 0 failed" after the order;
   - `REASK + "\n" + HEADER` becomes the new block.

   **This test is being changed for 32c right now** (the plan is refused first, then the repeat runs). The 32e change builds on that version, not on today's text.

Block, repair and pointer wording:
4. `test_200_rows_placed_in_order_with_a_short_compose_reply`, through the `BLOCK_200` constant.
5. `test_invalid_marker_is_asked_once_then_placed`, through the `INVALID` and `BLOCK_3` constants.
6. `test_worst_case_is_nine_compose_posts`, through the `REASK`, `INVALID` and `BLOCK_3` constants.
7. `test_cut_off_compose_retry_points_to_the_marker`, through the `POINTER` constant.
8. `test_padded_and_any_case_marker_is_valid`, through the block constants.
9. `tests/e2e/test_compose_row_markers.py::test_table_form_on_its_own_lines` (about line 203): its user message assertion `COMPOSE + "\n" + HEADER + "\n" + "- rate: 3 rows, input order; fields: item, sentiment"` becomes the new block with `- rate: 3 rows, input order, 0 failed; fields: item, sentiment` and the "Items with no result ...: 0" line. The placed table itself is unchanged.
10. `tests/e2e/test_compose_row_markers.py::test_marker_as_a_dict_key_takes_the_032_path` (about line 310): `composes[1].user == INVALID + "\n" + BLOCK_3` becomes the repair message with the 032 sentence and the new block. The 032 outcome is unchanged.
11. `tests/orchestrator/test_row_markers.py::test_fields_and_block_line` (about line 119): `"- s: 3 rows, input order; fields: item, b, c, d, error"` becomes `"- s: 3 rows, input order, 1 failed; fields: item, b, c, d, error"`. The field order is unchanged.
12. `tests/e2e/test_stage1_rootcause_bench.py::test_r2_no_final_text_asks_compose_once_more`: `startswith(_COMPOSE_REASK + "\nList steps the code")` becomes `+ "\nThe reader sees only your answer."`.
13. `tests/orchestrator/test_row_markers.py::test_scalar_state_op_list_line`: `"- v: 3 rows, stored order; fields: (values)"` becomes `"- v: 3 rows, stored order, 0 failed; fields: (values)"` (every step line carries its failed count).

**Unchanged:**
- the no-block gate test;
- `test_retyped_rows_are_accepted` (3 resolved items, under the floor);
- the guard tests;
- `tests/orchestrator/test_state_ops.py::test_counts_are_not_rows_for_the_next_op`;
- `tests/e2e/test_privacy_lane*.py` (none of them uses row markers).

Only focused tests run on the ThinkPad. The full suite runs in CI at PR time, in the one push shared with 32c.

## 3. Error codes

The registry's next free number when this note was first drafted was BRT-ORC-035; 32c took it (`dc97aa8`). 32d(ii) is expected to take BRT-ORC-036; if it takes another number, the builder shifts these and keeps their order.

Each code is logged on the re-ask event (`orchestrator_decision`, `"reask": true`). After a salvage it is recorded on the run as a warning, at the level of 029 to 032.

| Code | Meaning | Where |
|---|---|---|
| BRT-ORC-037 | A rows marker's spec cannot be used (unknown field, duplicate name, empty, in brackets, unreadable entry, over 600 characters) | row_markers.parse_target |
| BRT-ORC-038 | A rows marker is an element of a list, or sits below table rows, that compose wrote | compose_checks.nested_markers |
| BRT-ORC-039 | Unused: reserved for C2, which the planner dropped | none |
| BRT-ORC-040 | A long step output is neither placed nor present, and the answer points at a step | compose_checks.unplaced_long_outputs |
| BRT-ORC-041 | Compose gave a value no step produced: for an item with no value for that field, or for an asked-but-not-produced field | compose_checks.invented_values |

BRT-ORC-032 widens to cover a target of the wrong kind and misspellings (2.4.6). BRT-LANE-009 also covers placed or salvaged long text of a raw step.

## 4. What is not solved

- An answerer's list (A's `fix_and_complete_classifications.classifications`) is not a list step. It cannot be placed or joined; its values only exempt C4 hits and remove items from the no-result count.
- A child run's rows cannot be field-chosen by the parent.
- The rows marker cannot filter: a subset is typed by compose, at its token cost.
- No marker writes CSV. A CSV of stored data is typed by compose and accepted.
- Section 2.5's "not caught" list, and C3's recall.
- A double failure with no usable marker salvages in a form the grader cannot score (A test 6, D test 6).
- Compose input is not trimmed.
- Ruling 21b's open question is only narrowed.

## 5. Invariants (DESIGN_PRINCIPLES.md)

- Four node types: everything runs in compose post-processing.
- No Coach or critic: the checks are deterministic code with one bounded re-ask, as for 029 and 032. No model judges an answer.
- Law 1: the block and the re-asks are user-message content; governance is unchanged.
- Law 2: no scratch is read or written.
- The lane never loosens. Placement and salvage add only text compose was shown: outputs as `_stage_output_for_prompt` showed them, and names only when the manifest showed names. Raw placed or salvaged text gets the 21b guard.
- No protected file and no new framework.
- Off switch: requests stay byte-identical with no list step, no long output and no asked-but-not-produced entry. Otherwise the off switch is a plain revert. The change ships with 32c.

## 6. Open questions for the planner

Answered by the planner (BUILD.md section 8, "Planner readings under ruling 32 (4)"):

1. Settled by the lead: S2 yes; S1 only after 038, 040 or 041, with the text guard. The planner let the lead rulings stand.
2. **C2 on the second reply:** moot. C2 is dropped.
3. **C2's denominator and floor:** moot. C2 is dropped; C4 catches D alone.
4. **C3:** kept, narrow (2.4.3). The user guide says it does not catch a long output dropped without a pointer.
5. **The D score:** no grader change. The win rule reads the grader's figure over all items. The trial report gives, for any list job, items with a result / all items, accuracy over all items, and accuracy over items with a result. Round 2's D is stated as 0.60 as scored, of which 11 correct labels were guesses for notes with no result; 25 of 60 (0.42) without them; 25 of the 29 with a result.
6. **Deriving a spec from typed rows:** no. Salvage with no usable marker writes a list step's rows in the plain marker's form (2.4.5).

Still to come, after the code review (planner, before the 32g trial): a replay of C1, C3 and C4 over every stored compose reply with every fire listed here, and the dollars of a run of jobs A to D with zero, one and the worst-case number of compose posts.

## 7. Changes after review

Round 1 (16 defects, lead rulings 1 to 16, planner additions):

1. C2 narrowed to identity-value pairs whose value is the item's own stored value, in JSON, table or short-line form; grouped forms excluded. Join overlay `a + b` added (2.1). Re-measured on A, D and round-1 A's join, CSV, bullet and grouped forms. False-positive list redone; the denominator is an open question.
2. "No result" made per item and per field over list steps; exemptions on values, never on mentions. C re-derived; D after 32d stated; golden texts fixed.
3. C1 redefined as an element of a list among other elements. Placement of a marker as a key's value, as a quoted value, and as a lone list element defined.
4. OUTPUT inside a JSON string or object defined (escaped, or a quoted string) and tested (B tests 6 and 7).
5. Ruling wording corrected: thresholds, "usable" and salvage marked as my own choices. S2 accepted; S1 limited to 038 to 041, with the text guard. Tests touched listed (none).
6. The gate's run shapes, the 6-to-9 posts, the cost, and the search for affected tests (none found) stated.
7. Every test that changes with names listed, with its reason.
8. C4: whole-value vocabulary; a document id is not an identity of its parts; single-value rule for lines; the needs-human case measured.
9. Own text walked as structure; pairs read from it.
10. The repair message's repeated lines spelled out.
11. Near-misses given exact sentences under 032 and 037.
12. Diff hunk headers masked before LOOSE.
13. The 039 sentence suggests the join marker, matching D test 3.
14. Token method corrected: range 0.22 to 0.32; prose 649, 572 and 613 characters; figures recomputed.
15. Covered by 5.
16. `action: fail`: marker validation, C1 and placement run; C2 to C4 do not.
17. Planner additions: one push with 32c. The asked-but-not-produced list is read from `engine.asked_but_not_produced(run_id)` and shown under its own heading with exact wording. A value for such a field is a C4 case-2 hit with its own sentence; A's golden text covers it. Compose-side checks noted as not being plan validation.

Round 2 (four points):

18. **Block counts (2.3).** "Items with no result" now counts only items a step failed that no other step resolved, where "resolved" includes another step's output pairing the item with a value. Figures: A 0, C 2, D 31, D after 32d (4 of 31 resolved) 27.
    - Items resolved with null or "" are worded as "Rows with no value" per field (C: 74 each for the q1-q8 fields; 23, 24, 24 for the contradiction fields).
    - Fields with no value in any row (defer_reason on A and D) are left out.
    - C's golden text no longer says 82 parts have no result. The A, C and D golden texts are updated.
    - C4 case 1 keeps the wider per-field rule. Its sentence now says "no step gave a value for that field", so it does not call a null row "no result".
19. **C2's floor (2.4.2)** is at least 10 resolved items, matching the denominator. The D-after-32d case (retry resolves 4 of 31; one line naming 3 of them with values does not fire) is added as false-positive case (x) and D test 7.
20. **Tests (2.7):** the three further changed tests added (`test_table_form_on_its_own_lines`, `test_marker_as_a_dict_key_takes_the_032_path`, `test_fields_and_block_line`); the count is now twelve. Noted that `test_replayed_stage1_job_a_completes_with_full_rows` is being changed for 32c and that the 32e change builds on that version.
21. **Join (2.1).** A chosen field whose source step failed the item while another listed step resolved it now carries the source step's error code (for example "BRT-ANS-005") instead of null. Added to the row rules, round-1 A test 5 and the unit tests.

Planner reading (4), 18-Nov-2026 (applied by the builder before any code):

22. **C2 dropped.** BRT-ORC-039 stays unused; the other codes keep their numbers. 2.4.2 is kept as history. The order of checks, the repair-message order and every C2 test are revised; D is shown caught by C4 alone, and the join-marker hint moves into C4's case-1 sentence.
23. **C3 kept narrow** (2.4.3): a listed long output, not placed, not present, and a pointer to a step result or a step id. The user guide states the recall limit.
24. **Salvage with no usable marker** writes a list step's rows in the plain marker's form (2.4.5).
25. **Section 6 answered**, including how the trial report states the D score (section 1).

## 8. Replay over every stored compose reply (planner ruling 33a/(4), acceptance item 1)

Run on 18-Nov-2026 against the code after review round 1 (`8d96103`), by `tests/fixtures/replay_compose_checks.py` (read-only over the git-ignored DBs; prints one JSON row per reply and writes nothing). It runs the marker checks (032, 037), C1 (038), C3 (040) and C4 (041) exactly as `_handle_compose_result` would (`_RowContext.problems`).

**How the context is rebuilt.**
- With a compose request in `outbound_requests` (lane on): the stage results and the document manifest of the run's first compose request; the lane mode from `runs.lane_mode`, or from the manifest wording when the column is missing (round 1).
- Otherwise (lane off, no request ledger): each plan step's stored envelope. Round 1 stored no node id on envelopes; they are matched to the plan's node ids in planning order.
- Node types and data scopes come from the plan and re_plan replies. The asked-but-not-produced list is empty (every stored run predates ruling 32c), except a second pass over round-2 job A with the list 32c would hold (the fixture's).

**Sources searched.** Round 1: `bench/results/2026_11_17/dbs/*.db` (five runs with a compose reply; C had none). Round 2: `bench/results/2026_11_18/dbs/*.db` (four runs; `ruling23_live_check.db` holds no run). Demo: `demo/runs/db/demo.db` (five runs with a compose reply). Ruling-21 replay: `tests/fixtures/stage1_job_a_replay.json` (the other fixtures in `tests/fixtures/` hold no compose reply: packing items and request bodies, issues).

**Grades.** The bench grader's figure for the run (`bench/results/<day>/<job>_borg_cloud_1.json`, `quality`); for demo runs, my reading of the instruction against the stored step results (said so in the row).

| # | Source (DB, run) | Job | Reply | Checks that fire (hits) | Answer (grader) | Verdict |
|---|---|---|---|---|---|---|
| 1 | 2026_11_17 A_borg_cloud_1, 0ef867a2 | A, round 1 | `{"action": "compose"}`, no final text | none run: 029 path | BAD (run failed, BRT-ORC-009; items_answered 0) | not applicable |
| 2 | 2026_11_17 A_borg_local_workers_1, 70d0c3b1 | A, round 1 local workers | `{"action": "continue", "next_nodes": ...}`, no final text; no list step or long output in its stage results, so no block | none run | BAD (run failed; items_answered 0) | not applicable |
| 3 | 2026_11_17 B_borg_cloud_1, f8c9c899 | B, round 1 | the four files typed in full (long output n2, 10,929 characters) | none | RIGHT (18 checks passed, 0 failed) | no fire on a right answer. C3's "not present" condition is what keeps it quiet: n2 is retyped whole |
| 4 | 2026_11_17 D_borg_cloud_1, 25fb0186 | D, round 1 | `{"action": "compose"}`, no final text | none run: 029 path | BAD (run failed; items_answered 0) | not applicable |
| 5 | 2026_11_17 D_borg_local_workers_1, 2e070901 | D, round 1 local workers | `{"action": "continue", ...}`, no final text | none run: 029 path | BAD (run failed; items_answered 0) | not applicable |
| 6 | 2026_11_18 A_borg_cloud_1, 1806143d | A, round 2 | 10 typed rows, then the marker inside the same `[` | 038 (1) | BAD (items_answered 0) | true catch |
| 6b | same, with the 32c list | A, round 2 | same | 038 (1), 041 case 2 (9) | BAD | true catches: the 9 urgencies no step produced (email_004's typed values are exempt: the fix step pairs doc_004 with them) |
| 7 | 2026_11_18 B_borg_cloud_1, 4d238266 | B, round 2 | a summary ending "copy each file from the stage result (n1)" | 040 (1) | BAD (0 of 6 machine checks passed; all four files missing) | true catch |
| 8 | 2026_11_18 C_borg_cloud_1, 2fe90fe0 | C, round 2 | first reply: not logged (its `orchestrator_decision` event is missing; outbound rows 167 and 168 exist) | skipped: no reply to check | - | - |
| 8b | same | C, round 2 | second reply (after 029): bare numbers 1 to 8, then filler contradictions | none | MIXED (facts 6 of 8, contradictions 0 of 2; the judge is informational) | no fire. The filler contradictions are a miss (known limit 1 below) |
| 9 | 2026_11_18 D_borg_cloud_1, 372d53d3 | D, round 2 | all 60 rows typed, "medium" for the 31 notes with no result | 041 case 1 (31) | BAD (0.60 as scored, 11 of them guesses) | true catch, by C4 alone |
| 10 | demo.db, 19f56430 | demo: 60 feedback lines, sentiment and feature table | per-line table typed | skipped: context cannot be rebuilt. Lane off (no request ledger) and the envelopes of n1 and n4 to n13 (the apply_to_each steps) are not stored; only the two failed state_ops are | - | - |
| 11 | demo.db, e535b654 | demo: compare two remote-work policies | point-by-point comparison, risks, cost table | none run: no list step, no long output (the answerer fields are lists), so no block: the request is byte-identical to before | RIGHT by my reading (every point, risk and figure is in n1 to n3) | not applicable |
| 12 | demo.db, e31a5330 | demo: internal notes, topic, owner, follow-up (raw_local) | one typed line per note ("Dock repairs: Mara Quill, yes"), then `@@ROWS:extract_notes@@` | none | RIGHT by my reading: all ten lines equal the stored rows; note 7 comes from the answerer retry_note_07_3 | no fire on a right answer |
| 13 | demo.db, ece3d973 | demo: 60 feedback lines | counts and top features, then `@@ROWS_TABLE:n1@@` | none | RIGHT by my reading: the counts equal n2 and the features n3's top counts | no fire on a right answer |
| 14 | demo.db, d04e0d8a | demo: 60 feedback lines | counts, top features "counting related variants", then `@@ROWS:n1@@` | none | RIGHT by my reading: the counts equal n2; the top features merge n3's variants, as the reply says. Its closing note calls the JSON rows "the table above" | no fire on a right answer |
| 15 | stage1_job_a_replay.json (round-1 A, 0ef867a2) | A, ruling-21 replay | recorded `{"action": "compose"}` | none run: 029 path | BAD (as row 1) | not applicable |
| 15b | same | A, ruling-21 replay | scripted `{"response": "@@ROWS:extract_urgency@@"}` (the ruling-21 test's reply) | none | RIGHT for this test's purpose (200 rows, all stored values) | no fire on a right answer |

**False positives on a right answer: none.** All six right answers (rows 3, 11 to 14, 15b) pass every check, so no `fix:` commit was needed. Every fire (rows 6, 6b, 7 and 9) is a true catch on an answer the grader scores as bad.

**Known limits (misses on bad answers):**
1. C round 2 (row 8b): the filler contradictions, and units missing from the numbers. Compose selected and dropped values; it did not invent one next to an item, so C4 has nothing to read (2.5, "not caught").
2. A round 2 without the 32c list (row 6): only 038 fires. The 9 invented urgencies are caught only once ruling 32c has recorded "urgency" as asked for but not produced (row 6b). The live trial runs with 32c, so this holds there.
3. Round 1 (rows 1, 2, 4, 5): no final text. These are the 029 and 030 paths, outside the checks. They are bad, but no check is meant to catch them.
4. Two stored replies could not be replayed: C round 2's first reply (row 8, not logged) and demo 19f56430 (row 10, step results not stored).

## 9. Cost of extra compose posts (planner ruling 33a/(4), acceptance item 2)

**Inputs.**
- Round-2 stored figures (`bench/results/2026_11_18`): each run's total cost (`cost_dollars`, equal to its ledger), and its logged compose call (input and output tokens).
- grok-4.20 prices from `data/capability_matrix.yaml`: US$1.25 per million input tokens, US$2.50 per million output tokens.
- Non-compose cost = the run's total minus its logged compose call.
- A compose post under 32e = the logged input plus the block's growth over the request as sent (new block characters minus old block characters, divided by 4: A +342 tokens, B +132, C +364, D +242), with the logged output.

**Rows.**
- *Zero extra* is one compose post.
- *One extra* adds one post (a 029 re-ask or the repair re-ask).
- *Worst case* is 9 posts. It has two forms. "Logged" gives every post the logged output size. "Cap bound" gives every invocation a first post cut at 6,000 output tokens, then a pointer retry and a JSON-only retry at up to 12,000 each. That form is an upper bound, never observed.

| Job | Non-compose | Per post | Zero extra | One extra | Worst, logged outputs | Worst, cap bound | Direct (stored) | 70% win line |
|---|---|---|---|---|---|---|---|---|
| A | 0.1722 | 0.0304 | 0.2026 | 0.2330 | 0.4458 | 0.6624 | 0.229 | 0.1603 |
| B | 0.2060 | 0.0140 | 0.2200 | 0.2340 | 0.3319 | 0.5508 | 0.319 | 0.2233 |
| C | 0.2126 | 0.0356 | 0.2482 | 0.2838 | 0.5332 | 0.7544 | 0.800 | 0.5600 |
| D | 0.0935 | 0.0252 | 0.1186 | 0.1438 | 0.3199 | 0.5179 | none | none |

US dollars per run.

**Reading.**
- **A** is above its win line even with zero extra posts: its non-compose steps alone cost US$0.172 against a line of US$0.160. The compose posts do not decide A's cost verdict.
- **B** stays under its line only with zero extra posts (0.2200 against 0.2233). One repair re-ask (0.2340) puts it over. That is the re-ask C3 asks for on round-2 B's reply; the answer then holds the four files.
- **C** stays under its line through the worst case with logged outputs (0.5332 against 0.5600). Only the cap bound crosses it.
- **D** has no direct arm, so it has no win line.

**Caveats.**
- The stored total for C lacks its first compose call (not logged). As stored, C made two calls, so its real round-2 cost was about the "one extra" figure.
- Outputs are the logged sizes. Placement shortens them (2.6: about -195 tokens on A and -1,030 on D), so these figures are on the high side for a compliant reply.
