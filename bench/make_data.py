"""Generate the benchmark data (work order 3.2). Seeded: the same seed gives the same files.

    python bench/make_data.py            # writes bench/data/ (the output is committed)
    python bench/make_data.py --check    # regenerate in memory, exit 1 if bench/data differs

Layout written under bench/data/:

    job_a/email_001.txt ... email_200.txt     200 short support emails
    job_c/doc_01_*.md ... doc_15_*.md          15 long documents (~600k characters)
    job_d/note_01.txt ... note_60.txt          60 client notes with canary account numbers
    answers/job_a_answers.json                 category and urgency per email
    answers/job_c_answers.json                 questions, 8 planted facts, 2 planted contradictions
    answers/job_d_answers.json                 risk label per note, and the canary numbers

The answers live OUTSIDE the job folders, so a run that hands a job folder over
as documents never hands over its own answer key.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

SEED = 2109068936  # round 3: int(freeze 7db5d68[:8], 16), planner ruling 40 (round 2: 4132800769)
DATA_DIR = Path(__file__).resolve().parent / "data"

# --------------------------------------------------------------------------
# Job A: support emails
# --------------------------------------------------------------------------

CATEGORIES = ("billing", "technical", "account_access", "shipping", "feedback")

_CATEGORY_LINES = {
    "billing": [
        "I was charged twice for invoice {n} this month.",
        "My invoice {n} shows a fee I do not recognise.",
        "The refund for order {n} has not reached my card.",
        "Please update the VAT number on invoice {n}, it is wrong.",
        "My subscription renewed at the old price although the new plan was agreed.",
    ],
    "technical": [
        "The app crashes every time I open the reports tab.",
        "The export button does nothing since the last update.",
        "Your API returns error 500 on the items endpoint (request {n}).",
        "Sync with my calendar stopped working and no events come through.",
        "Uploads above 20 MB fail with a timeout message.",
    ],
    "account_access": [
        "I cannot log in and the password reset email never arrives.",
        "My account is locked after too many attempts.",
        "Two-factor codes are rejected even though my phone clock is right.",
        "I need to change the email address on my account (ref {n}).",
        "A colleague left and I must remove their access to our workspace.",
    ],
    "shipping": [
        "My parcel for order {n} has not arrived.",
        "Tracking has shown in transit for nine days on order {n}.",
        "The box for order {n} arrived damaged.",
        "I received the wrong item in order {n}.",
        "The courier left the package at a neighbour's door without telling me.",
    ],
    "feedback": [
        "I really like the new dashboard, great work.",
        "Suggestion: please add a dark mode.",
        "The onboarding guide was confusing in the third step.",
        "Thank you to the support team for the quick help last week.",
        "It would help if invoices could be downloaded in bulk.",
    ],
}

_URGENCY_LINES = {
    1: ["No rush at all, whenever you get a chance.", "Nothing time critical, reply when convenient."],
    2: ["Please reply sometime this week.", "A reply within the week would be fine."],
    3: ["I would appreciate a reply in the next couple of days.", "Please get back to me within two or three days."],
    4: ["This is blocking my team today, please prioritise it.", "We cannot work properly until this is fixed, please act today."],
    5: ["URGENT: we are losing money every hour until this is fixed.", "URGENT: our whole business is stopped, I need help right now."],
}

_GREETINGS = ["Hello,", "Hi team,", "Good morning,", "Dear support,", "Hello support team,"]
_SIGNOFFS = ["Thanks,", "Regards,", "Best,", "Kind regards,", "Thank you,"]
_NAMES = ["Priya", "Marcus", "Elena", "Tomas", "Aiko", "Noor", "Gabriel", "Sofia", "Dmitri", "Amara",
          "Lucas", "Hana", "Omar", "Ingrid", "Rafael", "Mei", "Callum", "Zainab", "Pavel", "Leila"]
_NOISE = [
    "I have been a customer for about two years.",
    "I already searched your help pages first.",
    "I am writing from my phone, sorry for typos.",
    "This is the first time I have had to write to you.",
    "My colleague suggested I contact you directly.",
]


def make_job_a(rng: random.Random) -> tuple[dict[str, str], dict]:
    files: dict[str, str] = {}
    answers: dict[str, dict] = {}
    for i in range(1, 201):
        cat = CATEGORIES[(i - 1) % 5] if i <= 100 else rng.choice(CATEGORIES)
        urgency = rng.randint(1, 3) if cat == "feedback" else rng.randint(1, 5)
        n = rng.randint(10000, 99999)
        body = [rng.choice(_GREETINGS), "", rng.choice(_CATEGORY_LINES[cat]).format(n=n)]
        if rng.random() < 0.5:
            body.append(rng.choice(_NOISE))
        body.append(rng.choice(_URGENCY_LINES[urgency]))
        body += ["", rng.choice(_SIGNOFFS), rng.choice(_NAMES)]
        name = f"email_{i:03d}.txt"
        files[name] = "\n".join(body) + "\n"
        answers[name] = {"category": cat, "urgency": urgency}
    meta = {
        "job": "A",
        "categories": list(CATEGORIES),
        "urgency_scale": "1 (lowest) to 5 (highest)",
        "answers": answers,
    }
    return files, meta


# --------------------------------------------------------------------------
# Job D: client notes with canary account numbers
# --------------------------------------------------------------------------

_RISK_LINES = {
    "high": [
        "Two payments are more than 60 days overdue and the lender has asked for a meeting.",
        "A default notice was issued last month and the owners have not answered it.",
        "Cash reserves fell below one month of costs and the auditor raised going-concern doubt.",
    ],
    "medium": [
        "One payment was ten days late this quarter, though it has since been paid.",
        "A key customer left and revenue dipped by eight percent, but payments are current.",
        "Covenant headroom is thin this year without being breached.",
    ],
    "low": [
        "All payments are on time and cash reserves are strong.",
        "There are no disputes and the three-year renewal has been signed.",
        "Debt falls every quarter and covenant headroom is wide.",
    ],
}
_CLIENT_A = ["Harlow", "Birchwood", "Quillfeather", "Marrow", "Tidewater", "Corvane", "Ostrander", "Pellham",
             "Stonebridge", "Lindgren", "Fairhaven", "Ashgrove", "Kestrel", "Brightwell", "Yarrow"]
_CLIENT_B = ["Logistics", "Holdings", "Foods", "Printworks", "Dental Group", "Engineering", "Textiles",
             "Marine", "Pharmacy", "Builders"]
_NOTE_FILLER = [
    "Prepared by the relationship desk after the quarterly call.",
    "The finance lead was cooperative and sent the figures on time.",
    "Staff numbers are stable and no restructuring is planned.",
    "The next scheduled review is in {m} months.",
    "The client asked us to keep the account number on file.",
    "The main contact is {name}, who joined the company last year.",
]


def make_job_d(rng: random.Random) -> tuple[dict[str, str], dict]:
    files: dict[str, str] = {}
    answers: dict[str, dict] = {}
    canaries: list[str] = []
    used = set()
    for i in range(1, 61):
        risk = ("low", "medium", "high")[(i - 1) % 3] if i <= 30 else rng.choice(("low", "medium", "high"))
        while True:
            client = f"{rng.choice(_CLIENT_A)} {rng.choice(_CLIENT_B)}"
            if client not in used:
                used.add(client)
                break
        canary = f"ACCT-CANARY-{rng.randint(1000, 9999)}-{rng.randint(1000, 9999)}"
        canaries.append(canary)
        filler = rng.sample(_NOTE_FILLER, 2)
        lines = [f"Client note for {client}.", f"Account {canary}.", rng.choice(_RISK_LINES[risk])]
        lines += [s.format(m=rng.choice([3, 6, 12]), name=rng.choice(_NAMES)) for s in filler]
        name = f"note_{i:02d}.txt"
        files[name] = " ".join(lines[:2]) + "\n\n" + "\n".join(lines[2:]) + "\n"
        answers[name] = {"risk": risk, "client": client, "canary": canary}
    meta = {
        "job": "D",
        "labels": ["low", "medium", "high"],
        "canaries": canaries,
        "answers": answers,
    }
    return files, meta


# --------------------------------------------------------------------------
# Job C: long documents with planted facts and contradictions
# --------------------------------------------------------------------------

_DEPTS = ["maintenance", "procurement", "dispatch", "quality", "finance", "safety", "planning", "training",
          "facilities", "customer service", "inventory", "fleet"]
_SITES = ["Northgate", "Riverside", "Eastfield", "Millbrook", "Southport", "Greystone", "Oakmont", "Westbury",
          "Lakeshore", "Hillcrest", "Ridgeway", "Fenwick"]
_THINGS = ["the pallet scanning rollout", "the winter staffing plan", "the supplier scorecards", "the dock scheduling tool",
           "the forklift battery programme", "the returns process", "the cold chain audit", "the overtime policy",
           "the driver rest-break roster", "the packaging waste review", "the barcode label migration",
           "the spare parts catalogue", "the insurance renewal", "the loading dock lighting upgrade"]
_UNITS = ["incidents", "delays", "work orders", "complaints", "late loads", "audit findings", "overtime hours", "tickets"]
_VERBS = ["reviewed", "audited", "re-planned", "simplified", "benchmarked", "costed", "documented", "rehearsed"]
_ADJ = ["steady", "uneven", "encouraging", "mixed", "slower than hoped", "better than the prior period", "within tolerance"]

_TEMPLATES = [
    "The {dept} team {verb} {thing} for the {site} site and recorded {n} {unit} against a plan of {m}.",
    "At {site}, progress on {thing} was {adj}, with {n} {unit} logged over the period.",
    "A working group from {dept} met {k} times to discuss {thing}; the outcome was {adj}.",
    "Costs for {thing} at {site} came to {n} units of budget, compared with {m} planned.",
    "The {dept} lead asked that {thing} be {verb} again before the {site} review, citing {n} open {unit}.",
    "Staff at {site} reported that {thing} was {adj}, and {dept} agreed to track {unit} weekly.",
    "No major change was made to {thing}; {dept} will keep monitoring {unit} at {site}.",
    "In the {site} report, {dept} noted {n} {unit}, which it described as {adj}.",
]

_DOC_TITLES = [
    "Quarterly Operations Report, Q1", "Quarterly Operations Report, Q2", "Safety and Compliance Handbook",
    "Fleet Management Review", "Warehouse Network Overview", "Supplier Relations Memo", "Project Osprey Charter",
    "Finance and Budget Notes", "Training and Staffing Plan", "Quality Audit Summary", "Facilities Maintenance Log",
    "Customer Service Review", "Expansion Planning Paper", "Risk Register Narrative", "Annual Review and Outlook",
]

# (doc number 1-15, sentence, question, expected answer substring)
_FACTS = [
    (3, "The Tamarack pilot ends on 14 March 2027, after which the findings go to the board.",
     "On what date does the Tamarack pilot end?", "14 March 2027"),
    (5, "The Kessler Falls depot has 412 loading bays across its two halls.",
     "How many loading bays does the Kessler Falls depot have?", "412 loading bays"),
    (6, "Questions about regulatory filings go to Ingrid Vassallo, the group compliance contact.",
     "Who is the group compliance contact for regulatory filings?", "Ingrid Vassallo"),
    (7, "The approved budget for Project Osprey is US$3.85 million, released in four tranches.",
     "What is the approved budget for Project Osprey?", "US$3.85 million"),
    (9, "Audit logs must be kept for 2,317 days before they may be destroyed.",
     "For how many days must audit logs be kept?", "2,317 days"),
    (10, "Refrigeration units for the new cold store will be supplied by Brannock Cryogenics.",
     "Which vendor will supply the refrigeration units for the new cold store?", "Brannock Cryogenics"),
    (12, "In an emergency shutdown the agreed code word is juniper-lantern.",
     "What is the code word for an emergency shutdown?", "juniper-lantern"),
    (14, "The Pell Harbour crane is inspected every 19 days by an outside engineer.",
     "How often is the Pell Harbour crane inspected?", "every 19 days"),
]

# (topic, doc A, sentence A, value A, doc B, sentence B, value B)
_CONTRADICTIONS = [
    ("Kessler Falls depot opening year",
     2, "The Kessler Falls depot opened in 2011 and has run continuously since.", "2011",
     11, "Since the Kessler Falls depot opened in 2014, volumes have risen every year.", "2014"),
    ("Project Osprey steering committee size",
     7, "The Project Osprey steering committee has seven members drawn from three divisions.", "seven members",
     13, "Project Osprey is governed by a steering committee of nine members.", "nine members"),
]

CONTRADICTION_QUESTION = (
    "Do the documents contradict each other anywhere? If so, name each topic and quote the two conflicting values."
)


def _paragraph(rng: random.Random) -> str:
    sents = []
    for _ in range(rng.randint(3, 5)):
        sents.append(rng.choice(_TEMPLATES).format(
            dept=rng.choice(_DEPTS), verb=rng.choice(_VERBS), thing=rng.choice(_THINGS),
            site=rng.choice(_SITES), unit=rng.choice(_UNITS), adj=rng.choice(_ADJ),
            n=rng.randint(3, 380), m=rng.randint(3, 380), k=rng.randint(2, 9),
        ))
    return " ".join(sents)


def make_job_c(rng: random.Random) -> tuple[dict[str, str], dict]:
    planted: dict[int, list[str]] = {}
    for doc, sent, _q, _a in _FACTS:
        planted.setdefault(doc, []).append(sent)
    for _t, da, sa, _va, db, sb, _vb in _CONTRADICTIONS:
        planted.setdefault(da, []).append(sa)
        planted.setdefault(db, []).append(sb)

    files: dict[str, str] = {}
    slug_of: dict[int, str] = {}
    for d in range(1, 16):
        title = _DOC_TITLES[d - 1]
        slug = "".join(c.lower() if c.isalnum() else "_" for c in title).strip("_")
        while "__" in slug:
            slug = slug.replace("__", "_")
        name = f"doc_{d:02d}_{slug}.md"
        slug_of[d] = name
        target = rng.randint(36000, 44000)
        paras: list[str] = []
        size = 0
        while size < target:
            p = _paragraph(rng)
            paras.append(p)
            size += len(p) + 2
        # insert planted sentences inside random paragraphs, away from the very start
        for sent in planted.get(d, []):
            idx = rng.randint(len(paras) // 5, len(paras) - 2)
            paras[idx] = paras[idx] + " " + sent
        sections: list[str] = [f"# {title}\n"]
        for i in range(0, len(paras), 12):
            sections.append(f"## Section {i // 12 + 1}\n")
            sections.append("\n\n".join(paras[i:i + 12]) + "\n")
        files[name] = "\n".join(sections)

    corpus = "\n".join(files.values())
    facts = []
    for doc, sent, q, a in _FACTS:
        assert corpus.count(a) == 1, f"planted answer {a!r} must appear exactly once, found {corpus.count(a)}"
        facts.append({"question": q, "answer": a, "doc": slug_of[doc]})
    contradictions = []
    for topic, da, _sa, va, db, _sb, vb in _CONTRADICTIONS:
        assert corpus.count(va) >= 1 and corpus.count(vb) >= 1
        contradictions.append({
            "topic": topic, "doc_a": slug_of[da], "value_a": va, "doc_b": slug_of[db], "value_b": vb,
        })
    meta = {
        "job": "C",
        "questions": [f["question"] for f in facts] + [CONTRADICTION_QUESTION],
        "facts": facts,
        "contradictions": contradictions,
        "chars": len(corpus),
        "approx_tokens": len(corpus) // 4,
    }
    return files, meta


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def build_all(seed: int = SEED) -> dict[str, str]:
    """Every output file as ``{relative path under bench/data: text}``."""
    out: dict[str, str] = {}
    files, meta = make_job_a(random.Random(f"{seed}-a"))
    out.update({f"job_a/{k}": v for k, v in files.items()})
    out["answers/job_a_answers.json"] = json.dumps(meta, indent=1) + "\n"
    files, meta = make_job_c(random.Random(f"{seed}-c"))
    out.update({f"job_c/{k}": v for k, v in files.items()})
    out["answers/job_c_answers.json"] = json.dumps(meta, indent=1) + "\n"
    files, meta = make_job_d(random.Random(f"{seed}-d"))
    out.update({f"job_d/{k}": v for k, v in files.items()})
    out["answers/job_d_answers.json"] = json.dumps(meta, indent=1) + "\n"
    return out


def write_all(dest: Path, seed: int = SEED) -> int:
    count = 0
    for rel, text in build_all(seed).items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        count += 1
    return count


def differs(dest: Path, seed: int = SEED) -> list[str]:
    bad = []
    for rel, text in build_all(seed).items():
        path = dest / rel
        if not path.exists() or path.read_bytes() != text.encode("utf-8"):
            bad.append(rel)
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(DATA_DIR))
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed data differs")
    args = ap.parse_args(argv)
    if args.check:
        bad = differs(Path(args.out), args.seed)
        print(f"{len(bad)} file(s) differ" if bad else "bench data matches the seed")
        return 1 if bad else 0
    n = write_all(Path(args.out), args.seed)
    print(f"wrote {n} files under {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
