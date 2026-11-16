# On-premise lane: design note

Work order 2.1. Written 15-Nov-2026 by the planner (Fable). Attacked by a fresh reviewer on 16-Nov-2026: 24 findings, all accepted, all closed below (section 11). Status: **design settled and reviewed; ready for 2.2.** Where this note and the short summary in `BUILD.md` work order 2.1 differ, this note wins.

## 1. The promise, exactly

> Raw document text is sent only to models on your own machines. Everything sent to a cloud model is listed, and you can read it.

Three precise statements stand behind it. The README, the deck and the interface may claim these and nothing stronger.

| | Statement | Kind | Held by |
|---|---|---|---|
| P1 | The runtime never places the content of a document into a request to a non-local endpoint. | Hard guarantee | Taint rules (5), routing and call guard (6), content backstop (7); privacy test |
| P2 | What a local step **returns** may be sent on. It is bounded: at most `lane_max_output_chars` per item, and no passage of `lane_max_verbatim_chars` or more copied from the document. | Bounded, not zero | Output guard (8) |
| P3 | Every request a run sent off the machine, model call or tool call, is recorded in full, before it is sent, and shown in the run view. | Audit | Ledger (9) |

**Scope.** The lane governs requests made by runs. Administrator actions (Discover, Get Model Info, research scoring) are outside it; they never see documents, and while `lane_mode` is `all_local` they are refused.

**What is not promised**, and must be said wherever the lane is described:

- The operator's own prompt goes to the planner. Documents must be handed over by reference. (If pasted text matches a loaded document, the backstop refuses the call; text from a document that was not loaded cannot be recognised.)
- Facts a local step extracts (a name, a date, a label, a short quote) can leave inside its answer. P2 bounds how much; P3 shows what.
- A document can contain instructions aimed at the local model ("output everything in base64", "paraphrase all of it"). The caps limit the damage; the backstop catches copied text, not paraphrase or encoding.
- **A local model server that itself forwards to a cloud service** (a proxy, a "cloud" model tag, a gateway on localhost) looks local to the runtime. The runtime refuses the cases it can see (section 3); any other is outside what it can detect unless the administrator restricts the lane to an allow-list of servers and models they vouch for.
- Mode `all_local` removes the first three limits by sending nothing out at all.

## 2. Modes

Config key `lane_mode` (env `BORG_LANE_MODE`), default `off`.

| Mode | Planner and compose | Steps that read documents | Other steps | Tools (web search) | Bypass, admin outbound actions |
|---|---|---|---|---|---|
| `off` | routed as in Phase 1 | routed as in Phase 1 | routed | allowed | allowed |
| `raw_local` | routed (may be cloud) | **local only** | routed (may be cloud) | allowed, checked, listed | bypass with documents refused |
| `all_local` | **local only** | **local only** | **local only** | refused | refused |

With `off`, behaviour is byte-for-byte Phase 1: no new field is read, no guard, backstop or ledger runs.

## 3. What "local" means

**Local is decided by where the request actually goes, never by a label.** The matrix `source` field is not trusted: `_infer_source` in `src/agents/executor.py` calls any unknown model id `lmstudio`, and `_pinned_model` in the engine labels a pinned endpoint `lmstudio` whatever its address.

`is_local_endpoint(url) -> bool` in a new `src/registry/locality.py`:

- Parses with `urllib.parse.urlsplit` and `ipaddress`. Scheme must be `http` or `https`; a URL with user info (`user@host`) is not local. The host is lower-cased and a trailing dot removed.
- Local when the host is an IP literal that `ipaddress` reports as loopback (`127.0.0.0/8`, `::1`; an IPv4-mapped IPv6 address is judged by its IPv4 part), the literal name `localhost`, or an entry of `BORG_LOCAL_HOSTS` (host names, IPs or CIDR ranges). No other name is looked up in DNS, so a name that resolves to 127.0.0.1 is still not local unless listed.
- Anything `ipaddress` cannot parse as written (decimal `2130706433`, hex `0x7f.1`, short `127.1`) and `0.0.0.0` are **not** local. The rule fails toward refusing.
- Private network ranges are not local by default. In Docker the model server is reached by a service name (`ollama`); `compose.yaml` adds it to `BORG_LOCAL_HOSTS`.

A model is local only when its URL is local **and**:

- it is not an Ollama model whose tag ends in `-cloud`, and discovery did not report it as remote (Ollama lists cloud models with a remote host); such rows get `remote: true` and are never local;
- if `lane_local_allowlist` (env `BORG_LANE_LOCAL_ALLOWLIST`: model ids and/or `host:port` entries) is set, it is on the list. Unset means every local URL counts.

Every local call is built with `httpx.AsyncClient(trust_env=False)`, so a `HTTP_PROXY`/`ALL_PROXY` variable cannot carry it off the machine. The same function produces the ledger's `left_machine` flag (section 9), so routing and record cannot disagree.

## 4. Documents by reference

- `BORG_DOCS_ROOT` in `.env` names the one folder documents may be read from. Unset: any request carrying documents is refused (`BRT-LANE-001`). The root is resolved with `resolve(strict=True)` at start-up; it is refused if empty, `.`, or if it is, or contains, the application directory (which holds `.env` and the database).
- A request carries `documents: {"folder": "<path relative to the root>"}` or `{"files": ["<relative path>", ...]}`. On `POST /v1/task` it is a body field; on `POST /v1/chat/completions` an extra top-level field.
- **Path check, before any read.** The requested string must be relative: absolute paths, drive-relative (`C:notes.txt`), root-relative (`\notes.txt`), UNC (`\\server\share`) and device prefixes (`\\?\`, `\\.\`) are refused before joining. Every file, including each file found by listing a folder, is resolved with `resolve(strict=True)` (following symlinks and junctions) and must satisfy `resolved.is_relative_to(root)` on resolved paths (Windows comparison ignores case). Failures give `BRT-LANE-001`, which names the requested relative path only, never the resolved absolute path.
- **Open, then verify.** The file is opened, and `os.fstat` on the open handle must match `os.stat` of the checked path (device and inode). A file swapped between check and open is refused. **Limit:** a hard link inside the root to a file outside it cannot be told apart; the administrator controls what is in the root.
- **File types first: `.txt` and `.md` only**, read as UTF-8 with replacement. Others are listed in the manifest as "unsupported" and skipped with a visible note. `.csv`, `.json`, `.log` and `.eml` are deferred: their records are short lines, each under the verbatim limit, so a local model can pass a whole table on in pieces (finding 5); they wait for a per-record output check. PDF needs a parsing library, a new runtime dependency.
- The **planner is given a manifest only**: count, and per document an id (`doc_001`), name, type and size. `lane_manifest: ids` hides the names too.
- The planner refers to documents by id: `"inputs_ref": "documents"` on an `apply_to_each`, or `"documents": ["doc_003"]` on an `answerer`. A document larger than the chosen local model's context is split into parts (`doc_003#2`). Nothing is silently cut.

## 5. Taint follows the data

1. **Content lives only in the child.** The engine passes document and part ids, never text. Text is loaded inside the executing child task from its id, right before that task builds its own (local) request. Items, `instruction`, `dependency_input`, stage outputs, `failures[]` and `envelopes.outbound_payload` hold ids only.
2. **Marking, by the runtime.** A task is `data_scope: "raw"` (new `WrappedTask` field, default `"derived"`) when its plan node has `documents`, an `inputs_ref` to documents, or a `depends_on`/`inputs_ref` that reaches a raw step whose output has not passed the output guard. The guard stamps each output it passes; unstamped raw output keeps the reader raw. A `data_scope` key on a plan node is ignored. `_make_child_task` copies `data_scope` and the document id to every child; skill nodes go through the same path.
3. **Aggregates.** The `apply_to_each` result of a raw step lists each item by its document or part id, not by `item` content.
4. **Failures go upward as code plus id.** A raw step's failure reason, as seen by re_plan, compose or any parent, is `{"code": "BRT-...", "doc": "doc_003#2"}`. Exception text, partial model output and file paths stay in the local log.
5. **No path skips the plan.** A request with documents turns off the trivial-conversation shortcut, and a planner `direct_answer` is rejected as a plan error: an answer about documents must come from steps that read them.
6. **History carries its lane.** Each stored thread message is tagged with the lane mode of the run that made it. Messages and rolling summaries tagged `all_local` are stripped from the thread window and from compressor input of any run whose planner is not local.
7. **No resume.** `POST /runs/{id}/resume` is refused for a run that read documents (code chosen in 2.2). Start a new run.
8. A raw step may be an `answerer` or an `apply_to_each`. An `orchestrate` node given documents is refused (`BRT-LANE-005`) outside `all_local`: its child planner would read the content.

## 6. Routing and the call guard

1. **Routing.** `local_only` is computed from the plan node (section 5.2), before routing. `select_model_for_node` gains `local_only: bool`. When true: a non-local `recommended_model` is ignored with a note; candidates are filtered to local models before the cost sort; there is **no fallback to the default model**. No local model clears the floor: `BRT-LANE-002`, raised so that the router's generic fallback (`except ValueError` → `fallback_default`) never catches it. In `all_local`, `local_only` holds for every step, `default_model` is chosen from local models only, and the orchestrator selection, including its degraded 5.1 path, applies the same filter: no local model clears the bar, the run fails with `BRT-LANE-002`.
2. **Call guard.** Immediately before every HTTP call, the resolved URL is checked with the section 3 rule. A raw task, or any call in `all_local`, headed for a non-local URL raises `BRT-LANE-003` and nothing is sent. Truncation and JSON-only retries pass the guard again. This fires only if marking or routing has a bug or a pinned endpoint misleads them.
3. **Bypass.** In `all_local` the bypass mode of the chat endpoint (a raw model id instead of `borg_route`) is refused outright (`BRT-LANE-005`). In `raw_local` a bypass request carrying `documents` is refused; other bypass calls go through the backstop and ledger.
4. In `all_local`, `tool_requests` and admin outbound actions are refused (`BRT-LANE-005`).

## 7. Content backstop (the primary check on what leaves)

When `lane_mode` is not `off` and the run has documents, **every request body headed for a non-local URL** is checked against the run's loaded document text before it is sent. One helper, `send_outbound()`, owns every such call: the answerer's `_call_model`, the orchestrator's `_post_and_collect`, the Tavily and Perplexity calls in `web_search`, the bypass relay in `chat_completions`, and the thread compressor. A hit refuses the call with `BRT-LANE-006` and nothing is sent.

- **Serialise.** Walk the whole body and join every key and every string value (numbers as text) with newlines into one string. (Not `json.dumps`: its escapes, such as `\n`, would break matches.) A string that itself holds JSON, such as a prompt block built with `json.dumps`, is decoded as well, a few levels deep, and the engine builds those blocks with `ensure_ascii=False`.
- **Normalise** body and documents alike: Unicode NFKC, casefold, then every run of characters that are not letters or digits (punctuation, whitespace, line breaks) becomes one space.
- **Flag** when any of these holds for any document of the run (child runs included):
  - (a) **Verbatim:** the body shares a run of `lane_max_verbatim_chars` (default 120) or more consecutive normalised characters with the document.
  - (b) **Coverage:** the share of the document's distinct word 8-grams that appear in the body exceeds `lane_max_ngram_share` (default 0.10). This catches a document passed on in short pieces.
  - (c) **Short document:** a document shorter than the verbatim limit occurs whole in the body. Documents under 24 normalised characters are skipped (they would match ordinary words) and listed as a limit.
- **Limits, stated honestly.** Paraphrase, translation, summaries and encodings (base64, spelling out) are not caught. Copied pieces below both limits are not caught. It is a backstop behind sections 5 and 6, not proof. The verbatim scan may be a plain scan or a rolling hash (documents are small).

## 8. Bounding what a local step returns (P2)

Applied to every raw envelope **whatever its status** (success, partial or failed: the output text, each item output and any partial output), before any of it can be placed in another request:

- **Size cap.** Serialised output longer than `lane_max_output_chars` (default 1500): the item fails with `BRT-LANE-004`.
- **Verbatim guard.** Any run of `lane_max_verbatim_chars` or more characters shared with the source document, or a short document (section 7 rule c) present whole, after the section 7 serialisation and normalisation (decoded values, never escaped JSON text): the item fails with `BRT-LANE-004`. (Added 16-Nov-2026 after the 2.2 runtime review.)
- A failed item goes into `failures[]` as code plus id (section 5.4). Its output is not passed on.
- Both limits are settings, because a false refusal is also a failure. The checks run in code on the local machine; they are not instructions to a model.

## 9. The record (P3)

- **One ledger row per HTTP request** that leaves the machine, in a new `outbound_requests` table: run id, envelope id, attempt number, destination (model or tool, real URL), `left_machine`, the **exact JSON body** as sent, status, times, error. Written by `send_outbound()`.
- The row is written, and the write awaited, **before** the POST, then updated after. Under the lane, a failed write blocks the request (`BRT-LANE-007`). A call that fails or times out still has its row.
- Retries (truncation, JSON-only, provider fallback in `web_search`) are separate rows. Tools record the provider URL and body they really sent.
- `GET /runs/{id}` returns "What left this machine" for the run and every child run, found through `parent_run_id`.
- **Local copies of document-derived text:** `logs` (`payload_in`, error text), `log_entities` and `log_relations` (the NER worker), `envelopes.outbound_payload`, and this ledger. All are local SQLite. With the lane on, `GET /logs/entities`, `GET /logs/export` and the outbound payloads in run detail go behind the admin token (work order 4.4); `scripts/export_run.py` refuses a run whose export would contain document text (work order 4.5).

## 10. Error codes

| Code | Meaning |
|---|---|
| `BRT-LANE-001` | Document path refused (not relative, outside `BORG_DOCS_ROOT`, changed between check and open), root unset or unsafe, or file unreadable |
| `BRT-LANE-002` | No local model clears the floor for a step (or, in `all_local`, the planner) that must stay local |
| `BRT-LANE-003` | A call that must stay local was about to go to a non-local endpoint; blocked. Indicates a defect |
| `BRT-LANE-004` | A local step's output was withheld: too large, or it copies a long passage of the document |
| `BRT-LANE-005` | Not available in this lane mode (tools, bypass or admin outbound actions in `all_local`; documents with bypass or on an `orchestrate` step) |
| `BRT-LANE-006` | Document text found in a request to a non-local endpoint (content backstop); blocked |
| `BRT-LANE-007` | The ledger row for an outbound request could not be written; request not sent |

Resume refusal: code chosen in 2.2. 2.2 may renumber; the error registry is the master once written.

## 11. Attacks considered

**A. From the author (15-Nov-2026).**

| # | How document text could reach a cloud model | Answer |
|---|---|---|
| A1 | Planner prompt contains the documents | Manifest only (4); backstop (7) |
| A2 | Item context of an `apply_to_each` child | Items hold ids; children inherit `raw`; routed local; guard (5, 6) |
| A3 | No local model good enough, router falls back to the default | No fallback; `BRT-LANE-002` (6.1) |
| A4 | Planner names a cloud model in `recommended_model` | Ignored for raw steps (6.1) |
| A5 | Unknown model id or pinned endpoint mislabelled `lmstudio` | Locality by URL, checked at the call (3, 6.2) |
| A6 | A LAN address that forwards to a cloud service | Private ranges not local unless listed (3) |
| A7 | Local step echoes the document into re_plan, compose or a `depends_on` step | Output guard (8); taint (5.2); backstop (7) |
| A8 | Document instructs the local model to exfiltrate in altered form | Size cap; stated as a limit (1); `all_local` |
| A9 | Child orchestrator sent to read the documents | Refused outside `all_local` (5.8) |
| A10 | Extracted facts put into a web search query | P2 data; backstop and ledger (7, 9); refused in `all_local` |
| A11 | Truncation or JSON-only retry uses a different model | Guard and backstop per call; each retry its own row (6.2, 9) |
| A12 | A run with documents resumed later, after the pool or mode changed, or with tasks rebuilt from `outbound_payload` without their scope | Resume refused for runs that read documents (5.7) (rewritten after finding 12) |
| A13 | Bypass relays straight to a named model | Documents refused in bypass; bypass refused in `all_local` (6.3) |
| A14 | Operator pastes the text into the prompt | Not prevented for unloaded text; stated (1); backstop catches text of loaded documents |
| A15 | Logs, exports, replay files | Local; admin token; export refuses document text (9) |
| A16 | File names reveal the matter | `lane_manifest: ids` (4) |
| A17 | `..`, absolute path, symlink out of the root | Relative-only, resolved, checked before read (4) |
| A18 | Conversation history carries earlier answers to the planner | Lane-tagged history (5.6) |

**F. From the fresh reviewer (16-Nov-2026).** All 24 accepted.

| # | Path found | Closed by |
|---|---|---|
| F1 | Document text placed in `items`/instruction/stage outputs travels inside envelopes and aggregates to cloud steps | Ids only; content loaded in the child (5.1) |
| F2 | A derived step reading a raw step through `depends_on`/`inputs_ref` receives unguarded raw output | Taint follows data unless guard-stamped (5.2) |
| F3 | Failure reasons (exception text, partial output) of raw steps go to re_plan and compose | Code plus doc id only upward (5.4) |
| F4 | Output guard skipped for failed or partial envelopes; aggregate keeps `item` content | Guard on every raw envelope whatever status (8); aggregates by id (5.3) |
| F5 | CSV, JSON, log and email records are short lines, each under the verbatim limit | `.txt`/`.md` only first (4); coverage check (7b) |
| F6 | Any missed path leaks silently: nothing looks at what actually leaves | Content backstop on every non-local body (7) |
| F7 | Ollama `-cloud` models are served from localhost but run remotely | Refused as non-local; remote flag from discovery; allow-list (3); limit stated (1) |
| F8 | `is_local_endpoint` fooled by URL tricks or proxy variables | Strict parse rules and test cases (3, 12); `trust_env=False` (3) |
| F9 | `BRT-LANE-002` caught by the router's generic fallback; cloud `default_model` and degraded orchestrator path in `all_local` | `local_only` from the node before routing; never caught; local filter on both (6.1) |
| F10 | Bypass in `all_local`; admin Discover and research scoring call out | Bypass refused in `all_local` (6.3); admin actions out of scope and refused in `all_local` (1, 6.4) |
| F11 | Thread history from an `all_local` run fed to a later cloud planner or compressor | Messages carry their lane; stripped (5.6) |
| F12 | Resume does not re-run saved tasks: it starts a fresh run with a saved step's instruction as the planner's user message, without the run's documents or saved `data_scope`, under the current lane mode | Resume refused (5.7); A12 rewritten |
| F13 | `_make_child_task` drops `data_scope`; skill nodes bypass marking | Copied; skill-node tests (5.2, 12) |
| F14 | Trivial-conversation shortcut and planner `direct_answer` skip the plan and every lane check | Shortcut off with documents; `direct_answer` rejected (5.5) |
| F15 | Ledger keeps 2000 characters and is written after the call; a failed or timed-out call leaves no row | Full row written and awaited before the POST (9) |
| F16 | Retries share one record | One row per HTTP request (9) |
| F17 | Ledger holds a reconstructed prompt, not what was sent | Exact JSON body (9) |
| F18 | Tools record a provider name, not the real URL and body; fallback provider unrecorded | Real URL and body, one row per provider attempt (9) |
| F19 | "What left" misses child runs | Walk via `parent_run_id` (9) |
| F20 | Local copies (logs, `log_entities`, `outbound_payload`) readable without auth | Named; behind the admin token with the lane on (9) |
| F21 | Windows path forms escape the root check | Relative-only; listed forms refused; `is_relative_to` on resolved paths (4) |
| F22 | File swapped for a link between check and open | Open, then `fstat` vs `stat` (4) |
| F23 | Files listed from a folder not checked one by one; hard links; root set to the app directory, empty or `.` | Each file resolved; hard links a stated limit; unsafe root refused (4) |
| F24 | Path and file text in error messages travel upward | `BRT-LANE-001` names the relative path only; code plus id upward (4, 5.4) |

## 12. Tests the privacy suite must hold (work order 2.3)

The six in `BUILD.md`, plus:

- Routing: a cloud `recommended_model` on a raw step is served locally; `local_only` with the local model below the floor gives `BRT-LANE-002` and zero cloud requests, also when the router's fallback path is in play; in `all_local` neither `default_model` nor the degraded orchestrator path is cloud.
- Guard: a raw task handed to `execute()` with a cloud model stamped on it raises `BRT-LANE-003` and the stand-in records no request; an unknown model id at a non-local endpoint is non-local.
- Locality: an Ollama model tagged `-cloud` is refused; with an allow-list set, an unlisted local model is refused. `is_local_endpoint` cases: `http://localhost.evil.com`, `http://127.0.0.1.nip.io`, `http://127.0.0.1@evil.com`, `http://evil.com#@127.0.0.1`, `http://LOCALHOST.`, `http://[::1]`, `http://[::ffff:127.0.0.1]`, `http://2130706433`, `http://0x7f.1`, `http://127.1`, `http://0.0.0.0`, `ftp://127.0.0.1` (only `LOCALHOST.`, `[::1]` and the mapped address are local); a local call ignores a set `HTTP_PROXY`.
- Taint: the canary is absent from every re_plan and compose request; a step that `depends_on` a raw step whose output was withheld is itself raw; an `apply_to_each` aggregate holds ids, not content; a raw failure reaches re_plan as code plus id; children of a raw `apply_to_each` and skill nodes with documents are raw; the trivial shortcut does not fire and `direct_answer` is rejected when documents are present; resume of a run with documents is refused; `all_local` history does not reach a cloud planner in the same thread.
- Output guard: an item copying 200 characters fails with `BRT-LANE-004` and that text appears in no later request; over the size cap fails, just under passes; a failed raw envelope is guarded too.
- Backstop: a 90-character document copied whole into a cloud request is refused (`BRT-LANE-006`); CSV-style lines of a document, each under 120 characters and passed in pieces, are refused by coverage; the same text with changed case, punctuation and line breaks is still caught; a tool query carrying document text is refused.
- Modes: tools and bypass are refused in `all_local`; documents with bypass and on an `orchestrate` step are refused; `lane_manifest: ids` puts no file name in the planner request.
- Ledger: a failed call has its row; a retry gives two rows; each row's body equals what the stand-in received; a forced ledger write failure blocks the request (`BRT-LANE-007`); child-run requests appear in the parent's list.
- Paths (2.3 (e)): `..`, absolute, drive-relative `C:x.txt`, root-relative `\x.txt`, UNC, `\\?\` prefix, a junction and a symlink pointing out, a mixed-case path inside the root (accepted), a file swapped after the check, a root of `.` or the app directory: all refused except the mixed-case one.
- `off`: the Phase 1 claim test passes unchanged, and no request differs from Phase 1.

The stand-in server in `tests/e2e/stub_model_server.py` records every request with its URL and full prompt, and `stub_model(..., source="ollama")` gives a local row at `127.0.0.1`.

## 13. Left to the builder

Exact field and setting names where this note gives none; how parts are sized and overlapped; the wording of interface notices; the resume refusal code; whether the verbatim checks use a rolling hash or a plain scan.
