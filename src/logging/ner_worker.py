"""Async NER worker — polls log_payloads for unprocessed text and extracts
named entities and subject-verb-object relations using spaCy en_core_web_sm.

Results land in log_entities and log_relations.

Error codes:
- BRT-LOG-010: spaCy model not available
- BRT-LOG-011: NER processing failed for a payload (logged, skipped)
- BRT-LOG-012: DB write failed after NER (logged, skipped)
- BRT-LOG-013: NER worker poll error (logged, worker continues)
"""
import asyncio
import sys
from typing import Optional

import aiosqlite

from src.db.schema import DB_PATH

_POLL_INTERVAL_SEC = 5.0
_BATCH_SIZE = 20

# Module-level spaCy model — loaded lazily on first use.
_nlp = None


def _load_nlp():
    global _nlp
    if _nlp is not None:
        return _nlp
    try:
        import spacy
        _nlp = spacy.load("en_core_web_sm")
        return _nlp
    except Exception as exc:
        raise RuntimeError(f"BRT-LOG-010: spaCy model not available: {exc}") from exc


def _extract_entities(doc) -> list[dict]:
    return [
        {
            "text": ent.text,
            "label": ent.label_,
            "start_char": ent.start_char,
            "end_char": ent.end_char,
        }
        for ent in doc.ents
    ]


def _extract_relations(doc) -> list[dict]:
    """Extract simple subject-verb-object triples via dependency parsing."""
    relations = []
    for token in doc:
        if token.dep_ in ("ROOT",) and token.pos_ == "VERB":
            subjects = [c for c in token.children if c.dep_ in ("nsubj", "nsubjpass")]
            objects_ = [c for c in token.children if c.dep_ in ("dobj", "pobj", "attr", "dative")]
            for subj in subjects:
                for obj in objects_:
                    relations.append({
                        "subject": subj.text,
                        "relation": token.lemma_,
                        "object": obj.text,
                    })
    return relations


async def process_batch(db_path: str = DB_PATH) -> int:
    """Process one batch of unprocessed log_payloads. Returns count processed.

    Each payload is NER-processed; entities + relations written to DB;
    ner_processed flag set to 1. Failures are logged to stderr and skipped.
    """
    nlp = _load_nlp()

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT lp.payload_id, lp.text, l.run_id
               FROM log_payloads lp
               JOIN logs l ON lp.log_id = l.log_id
               WHERE lp.ner_processed = 0
               ORDER BY lp.payload_id ASC
               LIMIT ?""",
            (_BATCH_SIZE,),
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        return 0

    processed = 0
    for row in rows:
        payload_id = row["payload_id"]
        text = row["text"] or ""
        run_id = row["run_id"]

        try:
            doc = nlp(text[:10_000])  # cap at 10k chars to avoid OOM
            entities = _extract_entities(doc)
            relations = _extract_relations(doc)
        except Exception as exc:
            print(
                f"BRT-LOG-011: NER processing failed for payload_id={payload_id}: {exc}",
                file=sys.stderr,
            )
            # Mark as processed anyway to avoid infinite retry on bad payloads
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "UPDATE log_payloads SET ner_processed=1 WHERE payload_id=?",
                    (payload_id,),
                )
                await db.commit()
            processed += 1
            continue

        try:
            async with aiosqlite.connect(db_path) as db:
                # Get log_id for FK
                async with db.execute(
                    "SELECT log_id FROM log_payloads WHERE payload_id=?", (payload_id,)
                ) as cur:
                    lp_row = await cur.fetchone()
                log_id = lp_row[0] if lp_row else None

                if log_id is not None:
                    for ent in entities:
                        await db.execute(
                            """INSERT INTO log_entities(log_id, run_id, text, label, start_char, end_char)
                               VALUES (?,?,?,?,?,?)""",
                            (log_id, run_id, ent["text"], ent["label"],
                             ent["start_char"], ent["end_char"]),
                        )
                    for rel in relations:
                        await db.execute(
                            """INSERT INTO log_relations(log_id, run_id, subject, relation, object)
                               VALUES (?,?,?,?,?)""",
                            (log_id, run_id, rel["subject"], rel["relation"], rel["object"]),
                        )

                await db.execute(
                    "UPDATE log_payloads SET ner_processed=1 WHERE payload_id=?",
                    (payload_id,),
                )
                await db.commit()
            processed += 1
        except Exception as exc:
            print(
                f"BRT-LOG-012: DB write failed after NER for payload_id={payload_id}: {exc}",
                file=sys.stderr,
            )

    return processed


async def run_ner_worker(db_path: str = DB_PATH, stop_event: Optional[asyncio.Event] = None) -> None:
    """Continuously poll for unprocessed payloads and run NER.

    Runs until stop_event is set (or forever if None). Errors are caught
    and logged — the worker never crashes.
    """
    try:
        _load_nlp()
    except RuntimeError as exc:
        print(f"{exc}", file=sys.stderr)
        return

    while True:
        if stop_event and stop_event.is_set():
            break
        try:
            await process_batch(db_path=db_path)
        except Exception as exc:
            print(f"BRT-LOG-013: NER worker poll error: {exc}", file=sys.stderr)
        await asyncio.sleep(_POLL_INTERVAL_SEC)
