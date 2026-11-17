"""Pairwise judge for job C (work order 3.2).

Rules, all enforced here:
- the judge model is in neither arm (the runner picks one that is not checked
  and not the baseline; this module only judges);
- answers are shown as "1" and "2": arm names never enter a prompt;
- each pair is judged twice with the order swapped; if the two verdicts do not
  name the same answer, the pair is a tie (a split is a tie);
- before judging real answers, a sanity case must pass: a deliberately
  truncated wrong answer must lose both times, or the judge refuses to judge
  (``JudgeRefused``) and the judge model should be replaced. With two judges,
  every judge must pass it;
- a reply that cannot be read is re-asked once; still unreadable, the round
  counts for the direct answer (conservative), and in the sanity case it is no
  vote, so the sanity case fails;
- every repeat pair is a comparison: a failed run against a completed one is
  won by the completed one (no judge call), two failed runs give no verdict
  (not a tie; left out of the comparisons);
- each answer's length and format markers (headings, list items, code blocks,
  document part-id references) are recorded beside its pair, so a reader can
  check whether length or format tracked the verdict;
- two-judge mode (ruling 11, only when no judge vendor outside both sides
  exists): BORG_ROUTE takes a comparison only when both judges prefer it.

``ask`` is any async callable ``(system, user) -> {"content": str, "dollars": float, ...}``.
"""
from __future__ import annotations

import json
import re
from typing import Awaitable, Callable, Optional

Ask = Callable[[str, str], Awaitable[dict]]

SYSTEM = (
    "You are a strict grader comparing two answers to the same set of questions about a set of documents. "
    "You are given the questions and the answer key. Judge only which answer is more correct and more "
    "complete against the key. Length, tone and formatting do not matter; wrong or missing facts do, and so "
    "does missing a contradiction the key lists. An answer that stops mid-sentence or leaves questions "
    "unanswered is worse. Reply with one JSON object and nothing else: "
    '{"better": "1" or "2" or "tie", "reason": "one sentence"}. Use "tie" only if the two are equally correct.'
)


_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s", re.MULTILINE)
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.MULTILINE)
_FENCE_RE = re.compile(r"^\s*```", re.MULTILINE)
_PART_ID_RE = re.compile(r"\bdoc_\d{3}(?:#\d+)?\b")   # by_reference part ids: doc_001, doc_001#2


def answer_markers(text: str) -> dict:
    """Length and format markers of one answer (recorded in judge_<job>.json; never shown to the judge)."""
    text = text or ""
    return {
        "chars": len(text),
        "words": len(text.split()),
        "headings": len(_HEADING_RE.findall(text)),
        "list_items": len(_LIST_RE.findall(text)),
        "code_blocks": len(_FENCE_RE.findall(text)) // 2,
        "part_id_refs": len(_PART_ID_RE.findall(text)),
    }


class JudgeRefused(Exception):
    """The judge failed its sanity case; it must not judge real answers."""


def key_text(key: dict) -> str:
    """The answer key as plain text for the judge (from bench/data/answers/job_c_answers.json)."""
    lines = ["ANSWER KEY"]
    for i, f in enumerate(key["facts"], 1):
        lines.append(f"{i}. {f['question']} -> {f['answer']}")
    lines.append(f"{len(key['facts']) + 1}. Contradictions planted in the documents:")
    for c in key["contradictions"]:
        lines.append(f"   - {c['topic']}: '{c['value_a']}' versus '{c['value_b']}'")
    return "\n".join(lines)


def build_prompt(key: dict, answer_1: str, answer_2: str) -> tuple[str, str]:
    """The (system, user) prompt. Answers appear only as "1" and "2"."""
    questions = "\n".join(f"{i}. {q}" for i, q in enumerate(key["questions"], 1))
    user = (
        f"QUESTIONS\n{questions}\n\n{key_text(key)}\n\n"
        f"=== ANSWER 1 ===\n{answer_1}\n=== END OF ANSWER 1 ===\n\n"
        f"=== ANSWER 2 ===\n{answer_2}\n=== END OF ANSWER 2 ===\n\n"
        'Which answer is better? Reply with the JSON object only.'
    )
    return SYSTEM, user


_JSON_RE = re.compile(r"\{.*?\}", re.DOTALL)


def parse_verdict(text: str) -> Optional[str]:
    """``"1"``, ``"2"``, ``"tie"`` or None when the reply cannot be read."""
    for m in _JSON_RE.finditer(text or ""):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        v = str(obj.get("better", "")).strip().lower()
        if v in ("1", "2", "tie"):
            return v
    return None


REASK = "\n\nYour last reply could not be read. Reply with the JSON object only, for example {\"better\": \"1\", \"reason\": \"...\"}."


async def judge_pair(ask: Ask, key: dict, answer_a: str, answer_b: str, on_unreadable: Optional[str] = None) -> dict:
    """Judge A against B twice, order swapped. ``winner`` is ``"a"``, ``"b"`` or ``"tie"``.

    A reply that cannot be read is re-asked once. If it is still unreadable, the
    round goes to ``on_unreadable`` (``"a"`` or ``"b"``; verdict ``"unreadable"``),
    or, when that is None (the sanity case), the round has no vote, which fails a
    sanity case and makes a real pair a tie.
    """
    rounds = []
    dollars = 0.0
    for first_is_a in (True, False):
        one, two = (answer_a, answer_b) if first_is_a else (answer_b, answer_a)
        system, user = build_prompt(key, one, two)
        reply = await ask(system, user)
        dollars += float(reply.get("dollars") or 0.0)
        v = parse_verdict(reply.get("content", ""))
        reasked = False
        if v is None:
            reasked = True
            reply = await ask(system, user + REASK)
            dollars += float(reply.get("dollars") or 0.0)
            v = parse_verdict(reply.get("content", ""))
        if v == "1":
            mapped = "a" if first_is_a else "b"
        elif v == "2":
            mapped = "b" if first_is_a else "a"
        elif v is None and on_unreadable in ("a", "b"):
            v, mapped = "unreadable", on_unreadable
        else:
            mapped = v  # "tie" or None
        rounds.append({"a_shown_as": "1" if first_is_a else "2", "verdict": v, "winner": mapped,
                       "reasked": reasked})
    w1, w2 = rounds[0]["winner"], rounds[1]["winner"]
    winner = w1 if (w1 == w2 and w1 in ("a", "b")) else "tie"
    return {"winner": winner, "rounds": rounds, "dollars": dollars}


def sanity_pair(key: dict) -> tuple[str, str]:
    """(good answer, deliberately truncated wrong answer), built from the key."""
    good = "\n".join(f"{i}. {f['answer']}" for i, f in enumerate(key["facts"], 1))
    good += "\n" + "; ".join(
        f"{c['topic']}: {c['value_a']} versus {c['value_b']}" for c in key["contradictions"]
    )
    first = key["facts"][0]
    bad = f"1. I could not find it, but the {first['question'].split()[0].lower()} is probably 1999 and the next"
    return good, bad


async def sanity_check(ask: Ask, key: dict) -> dict:
    """Raise ``JudgeRefused`` unless the truncated wrong answer loses both times."""
    good, bad = sanity_pair(key)
    res = await judge_pair(ask, key, good, bad)
    wins = [r["winner"] for r in res["rounds"]]
    if wins != ["a", "a"]:
        raise JudgeRefused(
            "judge sanity case failed: the truncated wrong answer did not lose both times "
            f"(verdicts {wins}); replace the judge model. No real answer was judged."
        )
    return {"passed": True, "rounds": res["rounds"], "dollars": res["dollars"]}


async def judge_all(ask, key: dict, pairs: list[dict]) -> dict:
    """Sanity first (for every judge), then every repeat pair.

    ``ask`` is one judge or a list of judges (two-judge mode, ruling 11: used when
    no judge vendor outside both sides exists; one judge per side's vendor).
    ``pairs``: ``[{"id", "direct", "borg", "direct_failed"?, "borg_failed"?}]``, one
    per repeat (per-run pairing). A failed run against a completed one: the
    completed one wins, with no judge call. Both failed: no verdict (``"none"``,
    left out of the comparisons, not a tie). Otherwise each
    judge judges the pair twice, order swapped; an unreadable verdict (after one
    re-ask) counts for direct. With several judges BORG takes the comparison only
    when every judge prefers it; any judge preferring direct gives it to direct;
    anything else is a tie.

    Which answer is "a" or "b" here is internal; the model only ever sees "1" and "2".
    Returns the counts and ``direct_preferred_in_majority`` (the win rule's test for
    job C: true means BORG_ROUTE did not hold quality). Failed runs are counted.
    """
    asks = list(ask) if isinstance(ask, (list, tuple)) else [ask]
    needs_judge = any(not p.get("direct_failed") and not p.get("borg_failed") for p in pairs)
    sanity = []
    dollars = 0.0
    if needs_judge:
        for i, a in enumerate(asks, 1):
            try:
                s = await sanity_check(a, key)
            except JudgeRefused as exc:
                raise JudgeRefused(f"judge {i}: {exc}") from exc
            sanity.append(s)
            dollars += s["dollars"]
    out_pairs = []
    for p in pairs:
        df, bf = bool(p.get("direct_failed")), bool(p.get("borg_failed"))
        markers = {"direct": answer_markers(p.get("direct") or ""), "borg": answer_markers(p.get("borg") or "")}
        if df or bf:
            winner = "none" if (df and bf) else ("borg" if df else "direct")
            note = "both runs failed: no verdict" if (df and bf) else f"{'direct' if df else 'borg'} run failed"
            out_pairs.append({"id": p["id"], "winner": winner, "note": note, "judges": [], "answer_markers": markers})
            continue
        verdicts = []
        for a in asks:
            r = await judge_pair(a, key, p["direct"], p["borg"], on_unreadable="a")
            dollars += r["dollars"]
            verdicts.append({"winner": {"a": "direct", "b": "borg", "tie": "tie"}[r["winner"]], "rounds": r["rounds"]})
        ws = [v["winner"] for v in verdicts]
        winner = "borg" if all(w == "borg" for w in ws) else ("direct" if "direct" in ws else "tie")
        out_pairs.append({"id": p["id"], "winner": winner, "judges": verdicts, "rounds": verdicts[0]["rounds"],
                          "answer_markers": markers})
    return summarise(out_pairs, sanity, dollars)


def summarise(out_pairs: list[dict], sanity: list, dollars: float) -> dict:
    """The counts over judged pairs. A pair whose two runs both failed (``"none"``) is no verdict: it is left
    out of the comparisons and is not a tie."""
    no_verdict = sum(1 for p in out_pairs if p["winner"] == "none")
    n = len(out_pairs) - no_verdict
    direct = sum(1 for p in out_pairs if p["winner"] == "direct")
    borg = sum(1 for p in out_pairs if p["winner"] == "borg")
    return {
        "sanity": sanity,
        "pairs": out_pairs,
        "comparisons": n,
        "no_verdict": no_verdict,
        "direct_wins": direct,
        "borg_wins": borg,
        "ties": n - direct - borg,
        "direct_preferred_in_majority": direct * 2 > n,
        "dollars": dollars,
    }
