"""The claims checker (scripts/check_claims.py), run on temp files only."""
import importlib.util
import json
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
