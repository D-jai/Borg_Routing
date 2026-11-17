"""bench/make_data.py: seeded, committed output matches, and the shapes the work order asks for."""
from __future__ import annotations

import json
import re
from pathlib import Path

from bench import make_data

DATA = make_data.DATA_DIR


def _json(name):
    return json.loads((DATA / "answers" / name).read_text(encoding="utf-8"))


def test_generator_is_seeded_and_the_committed_data_matches():
    assert make_data.build_all() == make_data.build_all()
    assert make_data.differs(DATA) == []
    assert make_data.build_all(1) != make_data.build_all(2)


def test_job_a_200_emails_five_classes_urgency_1_to_5():
    files = sorted((DATA / "job_a").glob("*.txt"))
    assert len(files) == 200
    ans = _json("job_a_answers.json")["answers"]
    assert set(ans) == {f.name for f in files}
    assert {a["category"] for a in ans.values()} == set(make_data.CATEGORIES)
    assert {a["urgency"] for a in ans.values()} == {1, 2, 3, 4, 5}
    assert max(f.stat().st_size for f in files) < 1000


def test_job_c_fifteen_documents_about_150k_tokens_with_planted_items():
    docs = sorted((DATA / "job_c").glob("*.md"))
    assert len(docs) == 15
    corpus = "\n".join(d.read_text(encoding="utf-8") for d in docs)
    assert 500_000 <= len(corpus) <= 700_000
    key = _json("job_c_answers.json")
    assert len(key["facts"]) == 8 and len(key["contradictions"]) == 2
    for f in key["facts"]:
        assert corpus.count(f["answer"]) == 1
        assert f["answer"] in (DATA / "job_c" / f["doc"]).read_text(encoding="utf-8")
    for c in key["contradictions"]:
        assert c["value_a"] in (DATA / "job_c" / c["doc_a"]).read_text(encoding="utf-8")
        assert c["value_b"] in (DATA / "job_c" / c["doc_b"]).read_text(encoding="utf-8")
        assert c["value_a"] != c["value_b"]
    assert len(key["questions"]) == 9  # eight facts and the contradiction question


def test_job_d_sixty_notes_with_canaries_and_labels():
    notes = sorted((DATA / "job_d").glob("*.txt"))
    assert len(notes) == 60
    key = _json("job_d_answers.json")
    assert len(set(key["canaries"])) == 60
    assert {a["risk"] for a in key["answers"].values()} == {"low", "medium", "high"}
    for n in notes:
        canary = key["answers"][n.name]["canary"]
        assert re.fullmatch(r"ACCT-CANARY-\d{4}-\d{4}", canary)
        assert canary in n.read_text(encoding="utf-8")


def test_answer_keys_are_outside_the_folders_handed_to_runs():
    for folder in ("job_a", "job_c", "job_d"):
        names = [p.name for p in (DATA / folder).rglob("*") if p.is_file()]
        assert not any("answer" in n for n in names)
    assert not any(p.name == "answers" for p in DATA.glob("job_*/*"))


def test_job_b_spec_is_all_python_and_names_its_four_files():
    spec = (Path(make_data.__file__).parent / "jobs" / "job_b_spec.md").read_text(encoding="utf-8")
    for name in ("schema.sql", "app.py", "openapi.yaml", "test_app.py"):
        assert name in spec
    assert "FastAPI" in spec and "sqlite3" in spec
