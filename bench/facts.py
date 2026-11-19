"""Job C machine fact score. INFORMATIONAL ONLY: it is recorded, never quoted; the judge decides job C.

Answers and the key are normalised the same way: lower case, number words to
digits, thousands commas dropped, "US$"/"USD" to "$", "19-day" to "19 day".
Each planted fact is matched by its kind:

- a date (the key's "14 March 2027") matches the same day in "D Month YYYY",
  "Month D, YYYY" or "YYYY-MM-DD" form;
- money (the key has "$" or "million") matches the same amount written as
  money ("$3,850,000", "3.85m USD", "US$ 3.85 million"), never a bare number;
- a count ("412 loading bays", "2,317 days") matches the same number with the
  key's unit word (singular or plural) within the next three words;
- anything else (a name, a code word) matches as text, ignoring case, hyphens
  and spacing.

A planted contradiction counts only when both conflicting values appear
(each matched like a fact) within ``WINDOW`` characters of each other and a
word of its topic appears within ``WINDOW`` characters of one of them.
"""
from __future__ import annotations

import math
import re
from typing import Optional

WINDOW = 300

_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_MULT = {"thousand": 1e3, "k": 1e3, "million": 1e6, "mn": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9}
_MONEY_WORDS = {"$", "dollar", "dollars"}
_TOPIC_STOP = {"year", "years", "size", "date", "number", "total", "project", "with", "from", "that", "this"}

_MONTH = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH + r"\s*,?\s+(\d{4})\b")
_MDY = re.compile(r"\b" + _MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s+(\d{4})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_QTY = re.compile(r"(?P<cur>\$\s*)?(?P<num>\d+(?:\.\d+)?)\s*(?P<mult>thousand|million|billion|mn|bn|k|m)?\b")
_TOKEN = re.compile(r"\$|[a-z]+|\d+(?:\.\d+)?")


def normalise(text: str) -> str:
    t = (text or "").lower().replace("–", "-").replace("—", "-")
    t = t.replace("us$", "$")
    t = re.sub(r"\busd\b", "$", t)
    t = re.sub(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", lambda m: str(_NUMBER_WORDS[m.group(1)]), t)
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)
    t = re.sub(r"(?<=\d)-(?=[a-z])", " ", t)
    return t


def _month(word: str) -> int:
    return _MONTHS.index(word[:3]) + 1


def dates(text: str) -> list[tuple[tuple[int, int, int], int]]:
    """``[((year, month, day), position)]`` in normalised ``text``."""
    out = []
    for m in _DMY.finditer(text):
        out.append(((int(m.group(3)), _month(m.group(2)), int(m.group(1))), m.start()))
    for m in _MDY.finditer(text):
        out.append(((int(m.group(3)), _month(m.group(1)), int(m.group(2))), m.start()))
    for m in _ISO.finditer(text):
        out.append(((int(m.group(1)), int(m.group(2)), int(m.group(3))), m.start()))
    return out


def quantities(text: str) -> list[dict]:
    """Every number in normalised ``text`` with its value, position, money flag and the next three words."""
    out = []
    for m in _QTY.finditer(text):
        mult = m.group("mult")
        value = float(m.group("num")) * (_MULT[mult] if mult else 1.0)
        after = _TOKEN.findall(text[m.end(): m.end() + 80])[:3]
        nxt = after[0] if after else None
        money = bool(m.group("cur")) or (nxt in _MONEY_WORDS) or (
            mult in ("million", "mn", "m", "billion", "bn") and (nxt is None or not nxt.isalpha()))
        out.append({"value": value, "pos": m.start(), "money": money, "after": after})
    return out


def _stem(word: str) -> str:
    return word[:-1] if word.endswith("s") and len(word) > 3 else word


def _spec(value: str) -> dict:
    """How to match one key value: a date, money, a count with its unit, a bare number, or text."""
    t = normalise(value)
    ds = dates(t)
    if ds:
        return {"kind": "date", "date": ds[0][0]}
    qs = quantities(t)
    if qs:
        q = qs[0]
        if "$" in t or "million" in t:
            return {"kind": "money", "value": q["value"]}
        tail = [w for w in _TOKEN.findall(t[q["pos"]:]) if w.isalpha()]
        if tail:
            return {"kind": "count", "value": q["value"], "unit": _stem(tail[-1])}
        return {"kind": "number", "value": q["value"]}
    return {"kind": "text", "text": re.sub(r"[\s_-]+", " ", t).strip()}


def positions(spec: dict, text: str) -> list[int]:
    """Where in normalised ``text`` the value of ``spec`` appears."""
    kind = spec["kind"]
    if kind == "date":
        return [p for d, p in dates(text) if d == spec["date"]]
    if kind == "text":
        flat = re.sub(r"[\s_-]+", " ", text)
        out, i = [], flat.find(spec["text"])
        while i >= 0:
            out.append(i)
            i = flat.find(spec["text"], i + 1)
        return out
    hits = []
    for q in quantities(text):
        if not math.isclose(q["value"], spec["value"], rel_tol=1e-9):
            continue
        if kind == "money" and not q["money"]:
            continue
        if kind == "count" and spec["unit"] not in {_stem(w) for w in q["after"]}:
            continue
        hits.append(q["pos"])
    return hits


def _topic_positions(topic: str, text: str) -> list[int]:
    words = [w for w in re.findall(r"[a-z]+", topic.lower()) if len(w) >= 4 and w not in _TOPIC_STOP]
    out = []
    for w in words:
        out.extend(m.start() for m in re.finditer(r"\b" + re.escape(_stem(w)), text))
    return out


def score_facts(answer: str, key: dict, window: int = WINDOW) -> dict:
    """``facts_found``/``facts_total``, ``contradictions_found``/``contradictions_total`` and what matched."""
    text = normalise(answer)
    matched = [f["question"] for f in key["facts"] if positions(_spec(f["answer"]), text)]
    contr = []
    for c in key["contradictions"]:
        pa, pb = positions(_spec(c["value_a"]), text), positions(_spec(c["value_b"]), text)
        topic = _topic_positions(c["topic"], text)
        ok = any(abs(a - b) <= window and any(min(abs(t - a), abs(t - b)) <= window for t in topic)
                 for a in pa for b in pb)
        if ok:
            contr.append(c["topic"])
    return {
        "facts_found": len(matched), "facts_total": len(key["facts"]),
        "contradictions_found": len(contr), "contradictions_total": len(key["contradictions"]),
        "facts_matched": matched, "contradictions_matched": contr,
    }


def informational(answer: str, key: Optional[dict]) -> dict:
    """The job C quality record: the fact score, labelled so nobody mistakes it for the result."""
    return {
        "informational_fact_score": score_facts(answer, key) if key else None,
        "informational_only": True,
        "note": "informational only, never quoted: the pairwise judge decides job C",
    }
