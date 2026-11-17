"""bench/judge.py: arms are never named, order is swapped, a split is a tie, the sanity case gates judging."""
from __future__ import annotations

import json
import re

import pytest

from bench import judge

KEY = {
    "questions": ["When does the pilot end?", "How many bays?", "Contradictions?"],
    "facts": [
        {"question": "When does the pilot end?", "answer": "14 March 2027", "doc": "d1"},
        {"question": "How many bays?", "answer": "412 loading bays", "doc": "d2"},
    ],
    "contradictions": [
        {"topic": "opening year", "doc_a": "d1", "value_a": "2011", "doc_b": "d2", "value_b": "2014"},
    ],
}


def _answers_in(user: str) -> tuple[str, str]:
    one = re.search(r"=== ANSWER 1 ===\n(.*?)\n=== END OF ANSWER 1 ===", user, re.DOTALL).group(1)
    two = re.search(r"=== ANSWER 2 ===\n(.*?)\n=== END OF ANSWER 2 ===", user, re.DOTALL).group(1)
    return one, two


class Fake:
    """A scripted judge. ``rule(one, two) -> "1"|"2"|"tie"``; records every prompt."""

    def __init__(self, rule):
        self.rule = rule
        self.prompts: list[tuple[str, str]] = []

    async def __call__(self, system: str, user: str) -> dict:
        self.prompts.append((system, user))
        one, two = _answers_in(user)
        return {"content": json.dumps({"better": self.rule(one, two), "reason": "x"}), "dollars": 0.01}


def longer_wins(one: str, two: str) -> str:
    return "1" if len(one) > len(two) else "2"


def test_parse_verdict():
    assert judge.parse_verdict('{"better": "1", "reason": "a"}') == "1"
    assert judge.parse_verdict('text {"better": 2} more') == "2"
    assert judge.parse_verdict('{"better": "TIE"}') == "tie"
    assert judge.parse_verdict("no json") is None
    assert judge.parse_verdict('{"better": "3"}') is None


async def test_truncated_answer_loses_both_times_and_judge_passes():
    ask = Fake(longer_wins)
    sanity = await judge.sanity_check(ask, KEY)
    assert sanity["passed"] is True
    assert [r["winner"] for r in sanity["rounds"]] == ["a", "a"]  # the good answer won in both orders
    assert len(ask.prompts) == 2


async def test_sanity_failure_refuses_and_no_real_answer_is_judged():
    always_first = Fake(lambda one, two: "1")  # position bias: wins by being shown first
    with pytest.raises(judge.JudgeRefused):
        await judge.judge_all(always_first, KEY, [{"id": "C_1", "direct": "x" * 50, "borg": "y" * 60}])
    assert len(always_first.prompts) == 2, "only the two sanity calls were made; the real pair was not judged"


async def test_a_split_is_a_tie():
    ask = Fake(lambda one, two: "1")
    res = await judge.judge_pair(ask, KEY, "answer a", "answer b")
    assert [r["winner"] for r in res["rounds"]] == ["a", "b"]
    assert res["winner"] == "tie"
    tie_then_win = Fake(lambda one, two: "tie")
    assert (await judge.judge_pair(tie_then_win, KEY, "a", "b"))["winner"] == "tie"


async def test_each_pair_is_judged_twice_with_the_order_swapped():
    ask = Fake(longer_wins)
    res = await judge.judge_pair(ask, KEY, "short", "a much longer answer")
    assert res["winner"] == "b"
    firsts = [_answers_in(u)[0] for _s, u in ask.prompts]
    assert firsts == ["short", "a much longer answer"], "the order swaps between the two rounds"


async def test_arm_names_never_enter_a_prompt():
    ask = Fake(longer_wins)
    pairs = [{"id": f"C_{i}", "direct": "The pilot ends 14 March 2027 and there are 412 loading bays; 2011 vs 2014.",
              "borg": "Pilot ends in March."} for i in (1, 2, 3)]
    out = await judge.judge_all(ask, KEY, pairs)
    assert out["comparisons"] == 3 and out["direct_wins"] == 3
    assert out["direct_preferred_in_majority"] is True
    for system, user in ask.prompts:
        text = (system + user).lower()
        for banned in ("direct", "borg", "arm"):
            assert banned not in text, f"{banned!r} leaked into a judge prompt"
        assert "answer 1" in text and "answer 2" in text


async def test_majority_flag_when_borg_does_not_lose():
    # the judge always prefers the shorter text: BORG's answer is the shorter one
    def shorter_wins(one, two):
        if "I could not find it" in one or "I could not find it" in two:
            return longer_wins(one, two)  # the sanity case still has to pass
        return "1" if len(one) < len(two) else "2"

    ask = Fake(shorter_wins)
    out = await judge.judge_all(ask, KEY, [{"id": "C_1", "direct": "long answer here", "borg": "short"}])
    assert out["borg_wins"] == 1 and out["direct_preferred_in_majority"] is False
    assert out["dollars"] == pytest.approx(0.01 * len(ask.prompts))
