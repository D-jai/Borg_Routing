"""The claims checker (scripts/check_claims.py), run on temp files only."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_claims", Path(__file__).resolve().parents[1] / "scripts" / "check_claims.py"
)
cc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cc)

TABLE = """# Build

## 6. Plan

| C99 | not in section 7 | test:nowhere.py::test_x |

## 7. Claims table

| Id | Claim | Backing |
|---|---|---|
{rows}

## 8. Rulings log
"""

TEST_FILE = """
def test_alpha():
    pass


class TestBeta:
    def test_gamma(self):
        pass
"""


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text(TEST_FILE, encoding="utf-8")
    (tmp_path / "bench").mkdir()
    (tmp_path / "bench" / "res.json").write_text(
        json.dumps({"a": {"b": [10, 42.5]}}), encoding="utf-8")
    return tmp_path


def run(tree, rows, doc_text, capsys):
    (tree / "BUILD.md").write_text(TABLE.format(rows="\n".join(rows)), encoding="utf-8")
    (tree / "guide.md").write_text(doc_text, encoding="utf-8")
    code = cc.main(["--root", str(tree), "--build", str(tree / "BUILD.md"),
                    "--doc", str(tree / "guide.md")])
    return code, capsys.readouterr().out


ROW1 = "| C01 | First promise | `test:tests/test_sample.py::test_alpha` |"
ROW2 = "| C02 | Second promise | `test:tests/test_sample.py::TestBeta::test_gamma` |"
DOC = "One promise. <!-- claim C01 -->\n\nAnother. <!-- claim C02 -->\n"


def test_passing_case(tree, capsys):
    code, out = run(tree, [ROW1, ROW2], DOC, capsys)
    assert code == 0, out
    assert "OK" in out and "FAIL" not in out


def test_removing_one_row_fails(tree, capsys):
    code, out = run(tree, [ROW1], DOC, capsys)
    assert code == 1
    assert "C02" in out and "no row" in out


def test_a_missing_test_function_fails(tree, capsys):
    bad = "| C02 | Second | `test:tests/test_sample.py::test_missing` |"
    code, out = run(tree, [ROW1, bad], DOC, capsys)
    assert code == 1
    assert "C02" in out and "test_missing" in out


def test_a_missing_test_file_fails(tree, capsys):
    bad = "| C02 | Second | `test:tests/nope.py::test_alpha` |"
    code, out = run(tree, [ROW1, bad], DOC, capsys)
    assert code == 1
    assert "nope.py" in out


def test_a_missing_class_fails(tree, capsys):
    bad = "| C02 | Second | `test:tests/test_sample.py::TestZeta::test_gamma` |"
    code, out = run(tree, [ROW1, bad], DOC, capsys)
    assert code == 1
    assert "TestZeta" in out


def test_results_value_mismatch_fails(tree, capsys):
    row = "| C03 | Costs 40 | `result:bench/res.json#a.b.1=42.5` |"
    code, out = run(tree, [ROW1, ROW2, row], DOC + "It costs $40 per run. <!-- claim C03 -->\n", capsys)
    assert code == 1
    assert "C03" in out and "40" in out and "42.5" in out


def test_results_value_match_passes(tree, capsys):
    row = "| C03 | Costs 42.5 | `result:bench/res.json#a.b.1=42.5` |"
    code, out = run(tree, [ROW1, ROW2, row], DOC + "It costs $42.50 per run. <!-- claim C03 -->\n", capsys)
    assert code == 0, out


def test_results_row_must_match_its_own_expected_value(tree, capsys):
    row = "| C03 | Costs 7 | `result:bench/res.json#a.b.1=7` |"
    code, out = run(tree, [ROW1, ROW2, row], DOC + "It costs 7 per run. <!-- claim C03 -->\n", capsys)
    assert code == 1
    assert "expects 7" in out


def test_an_unused_row_warns_but_passes(tree, capsys):
    code, out = run(tree, [ROW1, ROW2], "Only one. <!-- claim C01 -->\n", capsys)
    assert code == 0
    assert "WARN" in out and "C02" in out


def test_a_backing_without_a_kind_fails(tree, capsys):
    code, out = run(tree, [ROW1, "| C02 | x | tests/test_sample.py::test_alpha |"], DOC, capsys)
    assert code == 1
    assert "must start with" in out


def test_the_real_files_are_not_touched_by_these_tests(tree, capsys):
    # main() with explicit paths never reads the repository's own BUILD.md
    code, out = run(tree, [ROW1, ROW2], DOC, capsys)
    assert "C99" not in out


# ---- review round 1 additions ----

REPO = Path(__file__).resolve().parents[1]


def test_the_repository_claims_pass():
    assert cc.main([]) == 0


def test_removing_a_row_from_the_real_table_fails(tmp_path, capsys):
    text = (REPO / "BUILD.md").read_text(encoding="utf-8")
    kept = [ln for ln in text.splitlines() if not ln.startswith("| C01 |")]
    assert len(kept) == len(text.splitlines()) - 1
    build = tmp_path / "BUILD.md"
    build.write_text("\n".join(kept), encoding="utf-8")
    guide = tmp_path / "guide.md"
    guide.write_text((REPO / "docs" / "user-guide.md").read_text(encoding="utf-8"), encoding="utf-8")
    code = cc.main(["--root", str(REPO), "--build", str(build), "--doc", str(guide)])
    out = capsys.readouterr().out
    assert code == 1
    assert "C01" in out and "no row" in out


def test_a_duplicate_id_fails(tree, capsys):
    code, out = run(tree, [ROW1, ROW2, ROW1], DOC, capsys)
    assert code == 1
    assert "duplicate" in out


@pytest.mark.parametrize("marker", [
    "<!-- claim c01 -->", "<!--claim C01-->x <!-- Claim C01 -->", "<!-- claim C1x -->",
    "<!-- claim -->",
])
def test_a_malformed_marker_fails(tree, capsys, marker):
    code, out = run(tree, [ROW1, ROW2], DOC + marker + "\n", capsys)
    assert code == 1
    assert "malformed" in out


def test_a_lower_case_id_in_a_table_row_fails(tree, capsys):
    code, out = run(tree, [ROW1, ROW2, "| c03 | x | test:tests/test_sample.py::test_alpha |"], DOC, capsys)
    assert code == 1
    assert "upper case" in out


def test_a_row_with_too_few_cells_fails(tree, capsys):
    code, out = run(tree, [ROW1, ROW2, "| C03 | only a claim |"], DOC, capsys)
    assert code == 1
    assert "fewer than three cells" in out


def test_a_def_inside_a_string_or_comment_does_not_count(tree, capsys):
    (tree / "tests" / "test_tricky.py").write_text(
        '"""\ndef test_in_docstring():\n    pass\n"""\n'
        "# def test_in_comment():\n"
        "def helper():\n    def test_nested():\n        pass\n", encoding="utf-8")
    for name in ("test_in_docstring", "test_in_comment", "test_nested"):
        row = f"| C03 | x | test:tests/test_tricky.py::{name} |"
        code, out = run(tree, [ROW1, ROW2, row], DOC, capsys)
        assert code == 1, name
        assert name in out


def test_a_method_must_sit_in_the_named_class(tree, capsys):
    bad = "| C02 | Second | test:tests/test_sample.py::test_gamma |"
    code, out = run(tree, [ROW1, bad], DOC, capsys)
    assert code == 1 and "test_gamma" in out
    bad2 = "| C02 | Second | test:tests/test_sample.py::TestBeta::test_alpha |"
    code, out = run(tree, [ROW1, bad2], DOC, capsys)
    assert code == 1 and "test_alpha" in out


def test_a_number_with_an_attached_dash_is_not_the_number(tree, capsys):
    row = "| C03 | Costs 42.5 | result:bench/res.json#a.b.1=42.5 |"
    doc = DOC + "Warns BRT-ORC-019 and costs 42.5 <!-- claim C03 -->\n"
    code, out = run(tree, [ROW1, ROW2, row], doc, capsys)
    assert code == 0, out
    # and the code-like token alone does not count as the number
    doc2 = DOC + "Warns BRT-ORC-019 <!-- claim C03 -->\n"
    code, out = run(tree, [ROW1, ROW2, row], doc2, capsys)
    assert code == 1 and "no number" in out


def test_every_result_backing_of_a_row_is_compared(tree, capsys):
    row = "| C03 | x | result:bench/res.json#a.b.1=42.5; result:bench/res.json#a.b.0=10 |"
    code, out = run(tree, [ROW1, ROW2, row], DOC + "Costs 42.5 <!-- claim C03 -->\n", capsys)
    assert code == 1
    assert "says 42.5" in out and "says 10" in out


def test_a_marker_inside_a_fence_is_ignored(tree, capsys):
    doc = DOC + "```\n<!-- claim C77 -->\n<!-- claim bad -->\n```\n"
    code, out = run(tree, [ROW1, ROW2], doc, capsys)
    assert code == 0, out


def test_a_missing_document_or_build_file_is_a_fail_line(tree, capsys):
    (tree / "BUILD.md").write_text(TABLE.format(rows=ROW1), encoding="utf-8")
    code = cc.main(["--root", str(tree), "--build", "BUILD.md", "--doc", "nope.md"])
    out = capsys.readouterr().out
    assert code == 1 and "FAIL" in out and "nope.md" in out
    code = cc.main(["--root", str(tree), "--build", "gone.md", "--doc", "nope.md"])
    out = capsys.readouterr().out
    assert code == 1 and "gone.md" in out


def test_relative_paths_resolve_against_root(tree, capsys):
    (tree / "BUILD.md").write_text(TABLE.format(rows=ROW1), encoding="utf-8")
    (tree / "guide.md").write_text("Fine. <!-- claim C01 -->\n", encoding="utf-8")
    assert cc.main(["--root", str(tree), "--build", "BUILD.md", "--doc", "guide.md"]) == 0


# ---- review round 2 additions ----

def test_a_one_line_fence_does_not_toggle_the_fence(tree, capsys):
    doc = DOC + "```python x = 1```\n<!-- claim C77 -->\n"
    code, out = run(tree, [ROW1, ROW2], doc, capsys)
    assert code == 1 and "C77" in out and "no row" in out


def test_a_file_ending_inside_a_fence_fails(tree, capsys):
    code, out = run(tree, [ROW1, ROW2], DOC + "```\nnever closed\n", capsys)
    assert code == 1
    assert "open" in out and "fence" in out


@pytest.mark.parametrize("shown", ["**42.5**", "`42.5`", "*42.5*", "(`42.5`)"])
def test_a_number_in_bold_or_code_still_counts(tree, capsys, shown):
    row = "| C03 | x | result:bench/res.json#a.b.1=42.5 |"
    code, out = run(tree, [ROW1, ROW2, row], DOC + f"Costs {shown} <!-- claim C03 -->\n", capsys)
    assert code == 0, out
    code, out = run(tree, [ROW1, ROW2, row], DOC + f"Costs {shown.replace('42.5', '41')} <!-- claim C03 -->\n", capsys)
    assert code == 1




BANNED_PHRASES = (
    "twice", "half as", "a half", "a third", "a quarter", "double", "percent", "per cent",
    "factor of", "times", "less than", "costs less", "cheaper", "saves", "saved", "saving",
    "fraction", "faster", "lower cost", "lower spend", "less spend", "less cost",
)
NUMBER_WORDS = (
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred"
).split()


def readme_problems(text: str) -> list[str]:
    """Planner ruling 38a: the README states no result number and no saving.

    `%` and `$` fail anywhere, inside code spans too. Other digits are checked after
    stripping what may carry them (comments, fences, inline code, claim ids, codes, dates,
    list numbers, local addresses). Banned phrases are whole words, any letter case.
    "fast", "cheap" and "lower" alone are allowed."""
    s = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    s = re.sub(r"```.*?```", "", s, flags=re.S)
    problems = [f"symbol {c!r} in README" for c in "%$" if c in s]
    s = re.sub(r"`[^`]*`", "", s)
    s = re.sub(r"\bBRT-[A-Z]+-\d+\b|\bC\d+\b", "", s)
    s = re.sub(r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\b", "", s)
    s = re.sub(r"(?m)^\s*\d+\.\s", "", s)
    s = re.sub(r"(?:localhost|127\.0\.0\.1):\d+", "", s)
    problems += [f"number left in prose: {m.group(0)!r}" for m in re.finditer(r".{0,12}\d+.{0,12}", s)]
    for ph in BANNED_PHRASES:
        if re.search(r"\b" + re.escape(ph) + r"\b", s, re.I):
            problems.append(f"banned phrase: {ph}")
    nums = "|".join(NUMBER_WORDS)
    if re.search(rf"\b(?:{nums})\s+(?:times|percent|%)", s, re.I):
        problems.append("number word before times or percent")
    return problems


def test_the_readme_carries_no_result_number_or_saving_claim():
    assert readme_problems((REPO / "README.md").read_text(encoding="utf-8")) == []


PLANTED = [
    "Runs are twice as fast.",
    "It costs half as much as one call.",
    "Ten times lower spend.",
    "It cuts cost by forty percent.",
    "It costs less than one direct call.",
    "It is cheap, a third of the price.",
    "Spend drops by a factor of three.",
    "Saved `40%` on job A.",
    "It cuts spend by 40 % on long lists.",
    "It costs 3x less than one call.",
    "A run took 12 seconds.",
    "That is cheaper than calling a model directly.",
    "Cost is a quarter, a fraction of the price.",
    "The cap is double.",
    "Runs are faster and it saves money, a real saving.",
    "It costs `$5` a run.",
    "Five % off.",
]


@pytest.mark.parametrize("planted", PLANTED)
def test_the_readme_guard_fails_on_a_planted_example(planted):
    assert readme_problems("# Title\n\n" + planted + "\n") != []


def test_the_readme_guard_lets_dates_codes_ids_and_ports_through():
    ok = "1. Step one <!-- claim C01 -->\n\nBRT-ORC-019 on 18-Nov-2026 at localhost:8000, see C27.\n"
    assert readme_problems(ok) == []


def test_the_readme_guard_lets_the_routing_line_through():
    line = ('"Cheapest" means the lowest cost counting both dollars and waiting time, among the '
            "models that clear the step's quality floor. A slow local model can lose to a fast "
            "cheap cloud model unless the private lane or the local-first setting "
            "(`borg_route:local`) is on.")
    assert readme_problems("# T\n\n" + line + "\n") == []
