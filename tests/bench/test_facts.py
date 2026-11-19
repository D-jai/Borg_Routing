"""Job C informational fact score: numbers and units normalised, matched per planted fact with the key's unit;
a contradiction needs both conflicting values and the topic nearby. Never quoted; the judge decides job C."""
from __future__ import annotations

import json

from bench import facts, run_bench

KEY = json.loads((run_bench.BENCH_DIR / "data" / "answers" / "job_c_answers.json").read_text(encoding="utf-8"))


def found(answer: str) -> set[str]:
    return set(facts.score_facts(answer, KEY)["facts_matched"])


def test_money_written_any_common_way_matches_the_budget():
    budget = "What is the approved budget for Project Osprey?"
    for text in ("The budget is $3,850,000.", "Budget: US$ 3.85 million", "approved at 3.85m USD",
                 "USD 3,850,000 approved", "US$3.85 million"):
        assert budget in found(text), text
    for text in ("The budget is 3.85.", "3.85 million tonnes of cargo... no, wait", "$3.58 million"):
        # a bare number, or the wrong number, does not match; 'million tonnes' is not money
        assert budget not in found(text), text


def test_counts_need_the_keys_unit_next_to_the_number():
    bays = "How many loading bays does the Kessler Falls depot have?"
    logs = "For how many days must audit logs be kept?"
    crane = "How often is the Pell Harbour crane inspected?"
    assert bays in found("Kessler Falls has 412 bays.")
    assert bays in found("412 loading bays at Kessler Falls")
    assert bays not in found("Kessler Falls has 412 staff.")
    assert logs in found("Audit logs: 2317 days.") and logs in found("kept for 2,317 days")
    assert logs not in found("audit logs are kept for 2317 (see policy)")
    assert crane in found("The crane is inspected every 19 days.") and crane in found("a 19-day inspection cycle")
    assert crane not in found("19 cranes are inspected")


def test_dates_in_any_common_order():
    q = "On what date does the Tamarack pilot end?"
    for text in ("14 March 2027", "March 14, 2027", "on 2027-03-14", "14th March 2027", "Mar 14 2027"):
        assert q in found(text), text
    assert q not in found("March 2027") and q not in found("14 May 2027")


def test_names_and_code_words_ignore_case_and_hyphens():
    assert "What is the code word for an emergency shutdown?" in found("The code word is Juniper Lantern.")
    assert "Who is the group compliance contact for regulatory filings?" in found("ingrid vassallo")
    assert "Which vendor will supply the refrigeration units for the new cold store?" not in found("Brannock")


def test_a_contradiction_needs_both_values_close_together_and_the_topic_nearby():
    def contr(text):
        return set(facts.score_facts(text, KEY)["contradictions_matched"])

    year = "Kessler Falls depot opening year"
    size = "Project Osprey steering committee size"
    assert year in contr("The Kessler Falls depot opened in 2011 per the Q2 report, but the maintenance log says 2014.")
    assert year not in contr("Revenue grew from 2011 to 2014.")                        # no topic word nearby
    assert year not in contr("Kessler Falls depot opened in 2011." + " filler" * 80 + " Something else in 2014.")
    assert size in contr("Osprey steering committee: seven members in the charter vs 9 members in the paper.")
    assert size not in contr("The Osprey steering committee has seven members.")       # one value only
    assert size not in contr("Osprey committee: 7 vs 9 days")                           # wrong unit


def test_the_runner_records_it_as_informational_only():
    job = {"quality": {"kind": "pairwise_judge"}, "answers_file": "answers/job_c_answers.json"}
    cfg = run_bench.Config()
    q = run_bench.score_answer(job, "14 March 2027 and 412 bays", cfg)
    assert set(q) == {"informational_fact_score", "informational_only", "note"}
    assert q["informational_only"] is True and "never quoted" in q["note"]
    assert q["informational_fact_score"]["facts_found"] == 2 and q["informational_fact_score"]["facts_total"] == 8
