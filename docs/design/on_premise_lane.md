# On-premise lane: design note

Work order 2.1. Written 15-Nov-2026 by the planner (Fable). Status: **design settled, not yet attacked by a fresh reviewer** (the lead does that before 2.2 starts; see "For the reviewer" at the end). Where this note and the short summary in `BUILD.md` work order 2.1 differ, this note wins.

## 1. The promise, exactly

> Raw document text is sent only to models on your own machines. Everything sent to a cloud model is listed, and you can read it.

Three precise statements stand behind it. The README, the deck and the interface may claim these and nothing stronger.

| | Statement | Kind | Proven by |
|---|---|---|---|
| P1 | The runtime never places the content of a document into a request to a non-local endpoint. | Hard guarantee | Privacy test, guard test |
| P2 | What a local step **returns** may be sent on. It is bounded: at most `lane_max_output_chars` per item, and no passage of `lane_max_verbatim_chars` or more copied from the document. | Bounded, not zero | Guard tests |
| P3 | Every request that left the machine, model call or tool call, is recorded in full and shown in the run view. | Audit | Ledger test |

**What is not promised**, and must be said wherever the lane is described:

- The operator's own prompt goes to the planner. If the planner is a cloud model and the operator pastes document text into the prompt, that text leaves. Documents must be handed over by reference.
- Facts a local step extracts (a name, a date, a label, a short quote) can leave inside its answer. P2 bounds how much; P3 shows what.
- A document can contain instructions aimed at the local model ("output everything in base64"). The caps in P2 limit the damage; they do not make it impossible.
- Mode `all_local` removes all three limits by sending nothing out at all.

## 2. Modes

Config key `lane_mode` (env `BORG_LANE_MODE`), default `off`.

| Mode | Planner and compose | Steps that read documents | Other steps | Tools (web search) |
|---|---|---|---|---|
| `off` | routed as in Phase 1 | routed as in Phase 1 | routed | allowed |
| `raw_local` | routed (may be cloud) | **local only** | routed (may be cloud) | allowed, listed |
| `all_local` | **local only** | **local only** | **local only** | refused |

With `off`, behaviour is byte-for-byte Phase 1: no new field is read, no guard runs.

## 3. What "local" means

**Local is decided by where the request actually goes, never by a label.** The matrix `source` field is not trusted for this: `_infer_source` in `src/agents/executor.py` calls any unknown model id `lmstudio`, and `_pinned_model` in the engine labels a pinned endpoint `lmstudio` whatever its address.

- `is_local_endpoint(url) -> bool` in a new `src/registry/locality.py`: true when the URL's host is a loopback address (`127.0.0.0/8`, `::1`, `localhost`) or matches an entry of `BORG_LOCAL_HOSTS` (comma-separated host names, IPs or CIDR ranges).
- Private network ranges are **not** local by default. An address on the office LAN can be a proxy to a cloud model; the administrator lists the machines they vouch for.
- In Docker the local model server is reached by a service name (for example `ollama`); `compose.yaml` sets `BORG_LOCAL_HOSTS` to include it.
- The same function produces the `left_machine` flag (section 6), so the routing rule and the record cannot disagree.

## 4. Documents by reference

- `BORG_DOCS_ROOT` in `.env` names the one folder documents may be read from. Unset: any request carrying documents is refused.
- A request carries `documents: {"folder": "<path relative to the root>"}` or `{"files": ["<relative path>", ...]}`. On `POST /v1/task` it is a body field; on `POST /v1/chat/completions` an extra top-level field (OpenAI clients ignore unknown fields on the way in; ours reads it).
- Each path is resolved (symlinks followed) and must lie inside the resolved root. Anything else, including `..`, absolute paths and a symlink pointing out, is refused with `BRT-LANE-001`. Nothing is read before this check.
- Supported first: plain text (`.txt`, `.md`, `.csv`, `.json`, `.log`, `.eml`), read as UTF-8 with replacement. Anything else is listed in the manifest as "unsupported" and skipped with a visible note. PDF is later work: it needs a parsing library, which is a new runtime dependency.
- The **planner is given a manifest only**: count, and per document an id (`doc_001`), name, type and size. Setting `lane_manifest: ids` hides the names too (a file name can itself be sensitive).
- The planner refers to documents by id: `"inputs_ref": "documents"` on an `apply_to_each` (one item per document), or `"documents": ["doc_003"]` on an `answerer`. It never holds the content, so it cannot copy it anywhere.
- A document larger than the chosen local model's context is split into parts (`doc_003#2`), each its own item; results carry the part id. Nothing is silently cut.

## 5. Where the lane is enforced

Three independent points. A step that reads a document must pass all three.

1. **Marking, by the runtime.** When the engine resolves a document reference into content (in `_build_task_from_node`, or at expansion in `apply_to_each`), it sets `data_scope: "raw"` on the task (a new `WrappedTask` field, default `"derived"`). The planner cannot set or clear it: a `data_scope` key on a plan node is ignored. Children of a raw `apply_to_each` inherit `raw`.
2. **Routing.** `select_model_for_node` gains a `local_only: bool` argument. When true: a `recommended_model` that is not local is ignored with a note; candidates are filtered to local endpoints before the cost sort; and **there is no fallback to the default model**. No local model clears the floor: the step fails with `BRT-LANE-002`. In `all_local`, `local_only` is true for every step, and the orchestrator model selection applies the same filter (no local model clears the orchestrator bar: the run fails with `BRT-LANE-002`, it does not degrade to a cloud model).
3. **Last-moment guard, in the executor.** Immediately before the HTTP call (`_call_model` in `src/agents/executor.py`, and the orchestrator's `_post_and_collect` in `all_local`), the resolved URL is checked with `is_local_endpoint`. A raw task (or any call in `all_local`) headed for a non-local URL raises `BRT-LANE-003` and nothing is sent. This fires only if points 1 and 2 have a bug, a pinned endpoint misleads them, or a saved envelope is resumed under a changed pool. It is the check the privacy test leans on.

Further rules:

- A raw step may be an `answerer` or an `apply_to_each`. An `orchestrate` node given documents is refused (`BRT-LANE-005`): a child run has its own planner, which would read the content. (In `all_local` this is allowed, since that planner is local too.)
- Bypass mode on the chat endpoint (a raw model id instead of `borg_route`) ignores the lane, so a bypass request that carries `documents` is refused (`BRT-LANE-005`).
- In `all_local`, `tool_requests` are refused (`BRT-LANE-005`): a web search is an outbound call.
- Skill steps resolve documents through the same code path as plain steps (this needs finding R14 fixed first).

## 6. Bounding what a local step returns (P2)

Applied to the output of every raw step item, before that output can be placed in any other request:

- **Size cap.** Serialised output longer than `lane_max_output_chars` (default 1500): the item fails with `BRT-LANE-004`.
- **Verbatim guard.** Any string in the output that shares a run of `lane_max_verbatim_chars` (default 120) or more consecutive characters with the source document, after collapsing whitespace: the item fails with `BRT-LANE-004`.
- A failed item goes into the step's `failures[]` like any other item failure (`apply_to_each` is best-effort; the planner sees the code and can re-plan). Its output is not passed on.
- Both limits are settings, because a false refusal is also a failure: a task that legitimately quotes a clause needs a higher verbatim limit, and the administrator can grant it knowingly.
- The checks run in code on the local machine. They are not instructions to a model.

## 7. The record (P3)

- Every model call and every tool call logs `left_machine: true|false`, taken from `is_local_endpoint` on the real URL, in the existing event's decision data. No new event type.
- When `left_machine` is true and the lane is on, the **whole** request body is stored (today's `payload_in` keeps 2000 characters). Storage is local SQLite.
- `GET /runs/{id}` returns, per run, the list of outbound requests: destination model or tool, size, and the stored text. The run view shows it as "What left this machine".
- The logs export (`GET /logs/export`), the downstream-ingest view (`borg_db_interactions`) and replay exports hold prompt text. The export route goes behind the admin token (work order 4.4), and `scripts/export_run.py` refuses a run whose export would contain document text (work order 4.5).

## 8. Error codes

| Code | Meaning |
|---|---|
| `BRT-LANE-001` | Document path is outside `BORG_DOCS_ROOT`, the root is not set, or the file cannot be read |
| `BRT-LANE-002` | No local model clears the floor for a step that must stay local |
| `BRT-LANE-003` | A call that must stay local was about to go to a non-local endpoint; blocked. Indicates a defect or a changed pool |
| `BRT-LANE-004` | A local step's output was withheld: too large, or it copies a long passage of the document |
| `BRT-LANE-005` | Not available in this lane mode (a tool in `all_local`; documents with bypass mode; documents on an `orchestrate` step) |

## 9. Attacks considered

| # | How document text could reach a cloud model | Answer |
|---|---|---|
| 1 | Planner prompt contains the documents | Planner gets the manifest only (section 4) |
| 2 | Item context of an `apply_to_each` child | Children inherit `raw`; routed local; guard at the call (section 5) |
| 3 | No local model good enough, router falls back to the default (cloud) model | `local_only` has no fallback; `BRT-LANE-002` |
| 4 | Planner names a cloud model in `recommended_model` | Ignored for raw steps |
| 5 | Model id not in the matrix, or a pinned endpoint, is mislabelled `lmstudio` | Locality is by URL host, checked at the call (sections 3, 5) |
| 6 | A LAN address that forwards to a cloud service | Private ranges are not local unless the administrator lists them |
| 7 | Local step echoes the document in its answer, which then enters re_plan, compose or a `depends_on` step | Size cap and verbatim guard (section 6); remainder is listed (section 7) |
| 8 | Document text carries instructions to the local model to exfiltrate in altered form | Bounded by the size cap; stated as a limit; `all_local` for anyone who cannot accept it |
| 9 | Planner sends a child orchestrator to read the documents | Refused outside `all_local` |
| 10 | Extracted facts put into a web search query by a later plan | It is P2 data; the tool call is listed; refused in `all_local` |
| 11 | Truncation retry or JSON-only retry uses a different model | Both retries reuse the same resolved URL; the guard runs per call |
| 12 | A run resumed later, after the pool changed | The saved task keeps `data_scope`; the guard runs again at the call |
| 13 | Bypass mode relays straight to a named model | Documents refused in bypass mode |
| 14 | Operator pastes the text into the prompt | Not prevented; stated in the interface next to the documents control and in the README |
| 15 | Logs, exports, replay files carry prompts off the machine | Local by default; export behind the admin token; replay export refuses document text |
| 16 | File names reveal the matter | `lane_manifest: ids` |
| 17 | Path tricks (`..`, absolute path, symlink out of the root) | Resolved and checked before any read; `BRT-LANE-001` |
| 18 | Conversation history carrying earlier answers back to the planner | Earlier answers are P2 data that already left; in `all_local` the planner is local |

## 10. Tests the privacy suite must hold (work order 2.3)

The six in `BUILD.md`, plus:

- A raw step with `recommended_model` naming a cloud model is served locally.
- With `local_only`, a pool whose local model is below the floor yields `BRT-LANE-002` and zero cloud requests for that step.
- The guard: a raw task handed directly to `execute()` with a cloud model stamped on it raises `BRT-LANE-003` and the stand-in server records no request.
- A model id absent from the matrix with a non-local endpoint is treated as non-local.
- An item whose scripted answer copies 200 characters of its document fails with `BRT-LANE-004` and that text appears in no later request.
- An item whose answer exceeds the size cap fails the same way; one just under passes.
- A tool call is listed as outbound; in `all_local` it is refused.
- Documents with bypass mode, and documents on an `orchestrate` step, are refused.
- `lane_manifest: ids`: no file name appears in the planner request.
- A symlink inside the root that points outside it is refused.
- `off`: the Phase 1 claim test still passes unchanged, and no request differs from Phase 1.

The stand-in server in `tests/e2e/stub_model_server.py` already records every request with its URL and full prompt, and `stub_model(..., source="ollama")` gives a local row at `127.0.0.1`.

## 11. Left to the builder

Exact field and setting names where this note gives none; how parts are sized and overlapped; the wording of interface notices; whether the verbatim check uses a rolling hash or a plain scan (documents are small; a plain scan is fine).

## For the reviewer

You did not write this. Your one question: **how does the text of a document reach a non-local endpoint despite this design?** Work through section 9 looking for a path it misses, and through sections 5 and 6 looking for a rule that the code as it stands (`src/orchestrator/engine.py`, `src/agents/executor.py`, `src/orchestrator/apply_to_each.py`, `src/registry/node_router.py`, `src/api/`) would let a request slip around. Reply "No objection" or a numbered list of concrete paths, each naming the file and the request that would carry the text. Each real path gets a row in section 9 and a test in section 10 before 2.2 starts.
