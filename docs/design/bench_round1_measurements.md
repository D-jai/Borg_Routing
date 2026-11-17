# Bench round 1: root causes and measurements for the planner (rulings 16-18)

## Ruling 16: root causes of the five failed BORG runs (diagnosis only; nothing fixed)

Reproducing tests (failing, not committed): `tests/e2e/test_stage1_rootcause_bench.py`, 6 tests.

- **R1: a re_plan step that reuses a step id already in the results is skipped** (A cloud, A local, D local). `_with_ids` (src/orchestrator/engine.py, about line 1440) keeps an explicit planner id even when it is already in `results`; the wave loop (about lines 2325-2356) treats the re-sent step as done, nothing is ready, and the run goes straight to compose with nothing run. A cloud's re_plan re-sent `fix_classify_failures`, `retry_classify`, `merge_all_classifications`; A local's re_plan re-sent `classify_emails` (after BRT-LANE-002, with `task_type: extraction` added); D local's re-sent `retry_classify`. The lead had earlier left "an explicit id equal to a finished id means that step" as is; the live runs show it is harmful. Direct fix: in the re_plan `continue` branch, rename a `next_nodes` id already in `results` (for example `classify_emails_2`) and log it.
- **R2: a compose reply in the re_plan shape has no recovery** (all four BRT-ORC-009 runs). Every compose call (grok-4.20, max_tokens 6,000, json_object) came back parsed but as `{"action": "compose" | "continue", ...}`. `_handle_compose_result` accepts only `response` or `direct_answer`; its salvage reads only string keys, so per-item apply_to_each results are discarded. The three modes share one template listing all three output shapes, and re_plan and compose send the same user message, "continue". Direct fix: when a compose reply has `action` and no `response`, re-ask compose once with a line "You are in compose mode: reply with {\"response\": ...} only" (code side, like the existing JSON-only retry); optionally let salvage serialise apply_to_each `results`.
- **R3: a non-object entry in plan or re_plan `next_nodes` crashes the run** (job C). The re_plan returned valid JSON whose `next_nodes` ended with the string `"rationale: "`; `_with_ids` passes it through and the wave loop calls `.get` on it. Direct fix: drop non-object nodes (logged) so valid siblings run; an empty list after filtering means no next nodes.
- **A local's 0 worker calls:** the plan's step had no `task_type`, so it defaulted to `reasoning` (the skill's `recommended_specialty` is null); llama3.2:3b scores 4.6 there, below the light floor, so BRT-LANE-002 refused it (correct). The planner's corrected retry hit R1. A separate decision (protected `data/skills.yaml`): tag `classify_into_buckets` and `extract_fields` with `recommended_specialty: extraction`.
- **Compose cap after R1/R2 are fixed:** 200 rows need about 7,085 output tokens plus JSON-string escaping; at 6,000 the first compose would likely stop at the cap and the BRT-ORC-018 retry would double it (the cut call is paid). Options: pin ORCHESTRATOR_MAX_TOKENS at 12,000 or more for list-writing jobs, or have compose build lists from apply_to_each results in code.
- **Side findings:** the planner asked for state_ops that do not exist (`join`) or with wrong params (`filter` without `key`); the orchestrator logs `finish_reason` only on a length retry (logging it always would settle truncation questions from the database).

# Measurements (rulings 17 and 18)

Measured on 17-Nov-2026 from the stage-1 run databases (`bench/results/2026_11_17/dbs/`, git-ignored) and result files. Measurement only: nothing was changed or tuned. Token estimates use chars/4 where no logged count exists; logged counts are used wherever they exist.

## Ruling 17: where BORG's tokens went (job A, cloud arm)

**Calls and tokens.** 405 calls: 400 item calls (gemini-3.1-flash-lite: 620,897 prompt / 23,416 output tokens) and 5 orchestrator calls (grok-4.20: plan 10,931, re_plan 23,308 / 33,470 / 33,601, compose 33,528; total 134,838 / 1,368). Total 755,735 / 24,784, matching the result file. Item calls average 1,552 prompt and 58.5 output tokens.

**One item call, by part** (first request, doc_012; logged prompt 1,567 tokens, 10.5% above chars/4):

| Part | Chars | Est. tokens | Share |
|---|---|---|---|
| Governance block (header, no global or admin rules) | 646 | 162 | 11.4% |
| Operator instruction, verbatim | 439 | 110 | 7.7% |
| Answerer system prompt (static) | 3,484 | 871 | 61.4% |
| Task ids header | 209 | 52 | 3.7% |
| Skill text (classify_into_buckets, rendered) | 276 | 69 | 4.9% |
| "Context:" and document id | 31 | 8 | 0.5% |
| **The email itself** | **217** | **54** | **3.8%** |
| "State query result (none)" | 37 | 9 | 0.7% |
| Output schema | 202 | 50 | 3.6% |
| Side effects, fence, trailer | 131 | 33 | 2.3% |
| User message "begin" | 5 | 1 | 0.1% |
| Total | 5,677 | about 1,419 | 100% |

About 96% of every item prompt is fixed overhead; the answerer system prompt alone is 61%.

**Why two passes (400 calls, not 200).** The plan used `classify_into_buckets` over all 200 documents and put `extra_fields: {urgency}` and an output schema with urgency on the node, but the skill's rendered prompt asks only for `bucket | confidence | defer_reason`: urgency was never requested. re_plan 1 then ran a second apply_to_each over all 200 documents asking for urgency and category, making pass 1 redundant. re_plan 2 and 3 tried to merge with state_ops that failed (BRT-ORC-025), and the compose call returned `{"action":"compose"}` (83 tokens) with no answer: BRT-ORC-009.

**What compose would have had to write.** The direct arm's answer, the same 200-row JSON list, is 12,279 chars and 7,085 tokens (Opus tokenizer; this JSON tokenizes at about 1.73 chars per token, so chars/4 under-counts by 2.3 times). At grok-4.20's US$2.50 per million output tokens that is US$0.018; at opus-5.5's US$20, US$0.142, which is 68% of the direct arm's US$0.2085 total. Compose ran with `max_tokens` 6,000 and `response_format: json_object`; a 7,085-token list would likely not fit (a risk, not measured: compose here wrote 83 tokens).

## Ruling 17: local timing (job D, raw_local arm)

59 calls in 4,645 s. Prompt 1,412 to 1,426 tokens per call (mean 1,419); output 47 to 349 (mean 160). Logged `latency_ms` (median 1,196 s) is mostly queue time: requests were sent up to 20 at once and CPU Ollama serves one at a time (about 17.4 in flight on average). From the gaps between completion times: service time is about 13.1 s + 0.348 s per output token (57 gaps, R^2 0.47).

| Slice of an average call (about 68 s gap) | Seconds | Share |
|---|---|---|
| Prompt evaluation, whole 1,419-token prompt | about 13 | about 17-19% |
| of which fixed overhead (about 95% of prompt chars) | about 12.4 | about 16-18% |
| of which the note itself | about 0.7 | about 1% |
| Generating about 160 output tokens | about 56 | about 82% |

On this machine the time goes mostly to output generation (about 160 tokens written where about 47 would answer); the fixed prompt overhead is about 12 s per call, about 16-18% of the run. No Ollama prompt_eval timing was logged; the split between overhead and note is by token share.

## Ruling 18: the candidate baseline formula

Candidate: (operator message tokens + each document's tokens read once) x US$4 per million + final answer tokens x US$20 per million (anthropic/claude-opus-5.5).

| Job | Real direct cost | Formula (chars/4 input, real final output) | Error |
|---|---|---|---|
| A | US$0.2085 (16,693 in / 7,085 out) | US$0.1733 (with file markers: US$0.1830) | -16.9% (-12.2%) |
| B | US$0.3281 (two attempts: 1,616 in / 16,084 out; final answer 9,084) | US$0.1837 | -44.0% |
| B, counting both attempts' output | | US$0.3237 | -1.4% |
| C | US$0.7977 (195,460 in / 791 out) | US$0.6118 | -23.3% |

Why it lands low: chars/4 under-counts the opus-5.5 tokenizer (real input / chars/4 is 2.11x on A, 1.55x on B, 1.31x on C); on B the direct arm's first attempt hit its 7,000-token cap and was billed then discarded (US$0.14, 44% of the cost), which a one-pass formula cannot see. With real input token counts, the formula reproduces A and C exactly. With chars/4 it is a floor, not an estimate.

**The current `baseline_dollars` field** is BORG's own token total priced at opus-5.5's prices. It tracks how many calls BORG made, not what one flagship call costs:

| BORG run | baseline_dollars | Real direct cost | Field vs real |
|---|---|---|---|
| A cloud (failed) | US$3.5186 | US$0.2085 | 16.9 times too high |
| A local (failed) | US$0.1520 | US$0.2085 | 0.73x |
| B cloud (completed) | US$0.5841 | US$0.3281 | +78% |
| C cloud (failed) | US$0.4301 | US$0.7977 | -46% |
| D cloud / D local (failed) | US$1.4033 / US$0.7247 | no direct arm for D | not checkable |
