"""Pairwise judge for job C (work order 3.2).

Rules, all enforced here:
- the judge model is in neither arm (the runner picks one that is not checked
  and not the baseline; this module only judges);
- answers are shown as "1" and "2": arm names never enter a prompt;
- each pair is judged twice with the order swapped; if the two verdicts do not
  name the same answer, the pair is a tie (a split is a tie);
- before judging real answers, a sanity case must pass: a deliberately
  truncated wrong answer must lose both times, or the judge refuses to judge
  (``JudgeRefused``) and the judge model should be replaced.

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


async def judge_pair(ask: Ask, key: dict, answer_a: str, answer_b: str) -> dict:
    """Judge A against B twice, order swapped. ``winner`` is ``"a"``, ``"b"`` or ``"tie"``.

    ``rounds`` lists both verdicts mapped back to a/b/tie (``None`` if unreadable,
    which counts as no vote and makes the pair a tie).
    """
    rounds = []
    dollars = 0.0
    for first_is_a in (True, False):
        one, two = (answer_a, answer_b) if first_is_a else (answer_b, answer_a)
        system, user = build_prompt(key, one, two)
        reply = await ask(system, user)
        dollars += float(reply.get("dollars") or 0.0)
        v = parse_verdict(reply.get("content", ""))
        if v == "1":
            mapped = "a" if first_is_a else "b"
        elif v == "2":
            mapped = "b" if first_is_a else "a"
        else:
            mapped = v  # "tie" or None
        rounds.append({"a_shown_as": "1" if first_is_a else "2", "verdict": v, "winner": mapped})
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


async def judge_all(ask: Ask, key: dict, pairs: list[dict]) -> dict:
    """Sanity first, then every pair.

    ``pairs``: ``[{"id": str, "direct": str, "borg": str}]``. Which answer is "a"
    or "b" here is internal; the model only ever sees "1" and "2".
    Returns the counts and ``direct_preferred_in_majority`` (the win rule's
    test for job C: true means BORG_ROUTE did not hold quality).
    """
    sanity = await sanity_check(ask, key)  # may raise JudgeRefused
    out_pairs = []
    dollars = sanity["dollars"]
    for p in pairs:
        r = await judge_pair(ask, key, p["direct"], p["borg"])
        dollars += r["dollars"]
        winner = {"a": "direct", "b": "borg", "tie": "tie"}[r["winner"]]
        out_pairs.append({"id": p["id"], "winner": winner, "rounds": r["rounds"]})
    n = len(out_pairs)
    direct = sum(1 for p in out_pairs if p["winner"] == "direct")
    borg = sum(1 for p in out_pairs if p["winner"] == "borg")
    return {
        "sanity": sanity,
        "pairs": out_pairs,
        "comparisons": n,
        "direct_wins": direct,
        "borg_wins": borg,
        "ties": n - direct - borg,
        "direct_preferred_in_majority": direct * 2 > n,
        "dollars": dollars,
    }
