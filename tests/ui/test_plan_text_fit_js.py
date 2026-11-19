"""Lead ruling (plan-node text): text in a plan node is cut to the room it has, so a model name never
runs into the cost and a long title stays inside its box."""
from pathlib import Path

import pytest

from tests.ui.test_app_js import NODE
from tests.ui.test_workspace_js import js

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH")

SEVEN = "const m = (t) => t.length * 7;"


def test_text_that_fits_is_left_whole(tmp_path):
    assert js(tmp_path, SEVEN + "return W.fitText('gpt-5.4-nano', 100, m);") == "gpt-5.4-nano"


def test_text_that_does_not_fit_is_cut_to_the_room_with_an_ellipsis(tmp_path):
    out = js(tmp_path, SEVEN + "return W.fitText('Apply state_op: count the tickets', 100, m);")
    assert out.endswith("…") and len(out) * 7 <= 100 and len(out) * 7 > 100 - 14
    assert not out[:-1].endswith(" ")


def test_without_a_measure_or_room_the_text_is_left_whole(tmp_path):
    out = js(tmp_path, "return [W.fitText('abc', 10, () => 0), W.fitText('abc', 0, (t) => 99), W.fitText(null, 10, null)];")
    assert out == ["abc", "abc", ""]


def test_the_run_page_fits_model_names_against_the_cost_after_the_drawing_is_placed():
    src = Path("ui/assets/pages/run.js").read_text(encoding="utf-8")
    assert "room(cm, 0, cc)" in src and "room(smod, 0, sc)" in src
    placed = src.index("A.clear(A.$('plan-box')).appendChild(svg);")
    assert src.index("fits.forEach", placed) > placed
