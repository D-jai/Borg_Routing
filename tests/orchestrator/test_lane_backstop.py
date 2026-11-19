"""TC-LANE-030..: the content checks (src/lane/backstop.py), the document
splitter and the lane settings. Design note sections 2, 4 and 7."""
import pytest

from src.lane.backstop import DocIndex, find_leak, normalise, serialise
from src.lane.documents import split_text
from src.lane.errors import LaneError
from src.lane.settings import load_lane_settings, validate_value

LONG_DOC = (
    "The relationship desk met the treasurer on the ninth. Two lease payments "
    "were late in the third quarter and the board has asked for a recovery plan "
    "before any new credit line is approved for the equipment division."
)
CSV_DOC = "\n".join(
    f"{i},customer {name},account {1000 + i},balance {i * 37} dollars,region north"
    for i, name in enumerate(["alder", "birch", "cedar", "dogwood", "elm", "fir",
                              "ginkgo", "hazel", "ironwood", "juniper"])
)


def _leak(body, *docs, verbatim=120, share=0.10):
    return find_leak(body, [DocIndex(f"doc_{i:03d}", d, verbatim) for i, d in
                            enumerate(docs, start=1)], verbatim, share)


# Happy path ---------------------------------------------------------------------

def test_an_ordinary_request_passes():
    body = {"messages": [{"role": "user", "content": "Which clients are high risk?"}]}
    assert _leak(body, LONG_DOC, CSV_DOC) is None


def test_short_facts_from_a_document_pass():
    body = {"messages": [{"role": "system", "content": "Acme: high risk, treasurer resigned."}]}
    assert _leak(body, LONG_DOC) is None


def test_serialise_walks_keys_and_values_without_json_escapes():
    text = serialise({"a": ["x\ny", {"b": 3, "c": True}]})
    # keys and values one per line, then the values alone (round 2): "x\ny 3"
    assert text.split("\n") == ["a", "x", "y", "b", "3", "c", "x", "y 3"]


def test_split_text_keeps_every_character_and_prefers_line_breaks():
    text = ("line of text\n" * 50) + "tail"
    parts = split_text(text, 100)
    assert "".join(parts) == text
    assert all(len(p) <= 100 for p in parts)
    assert all(p.endswith("\n") for p in parts[:-1])
    assert split_text("short", 100) == ["short"]


async def test_settings_default_to_off(tmp_path, monkeypatch):
    from src.db.schema import create_all

    db = str(tmp_path / "s.db")
    await create_all(db)
    monkeypatch.delenv("BORG_LANE_MODE", raising=False)
    settings = await load_lane_settings(db)
    assert settings.mode == "off" and not settings.on
    assert (settings.max_output_chars, settings.max_verbatim_chars) == (1500, 120)


async def test_config_key_beats_env(tmp_path, monkeypatch):
    from src.db.schema import create_all
    from src.db.state import set_config

    db = str(tmp_path / "s.db")
    await create_all(db)
    monkeypatch.setenv("BORG_LANE_MODE", "all_local")
    await set_config("lane_mode", "raw_local", db_path=db)
    assert (await load_lane_settings(db)).mode == "raw_local"


# Unhappy path -------------------------------------------------------------------

def test_a_long_passage_is_caught_verbatim():
    body = {"messages": [{"role": "user", "content": "Notes: " + LONG_DOC[10:180]}]}
    assert _leak(body, LONG_DOC) == ("doc_001", "verbatim")


def test_changed_case_punctuation_and_line_breaks_are_still_caught():
    mangled = LONG_DOC.upper().replace(" ", "\n").replace(".", " ;; ")
    assert _leak({"q": mangled}, LONG_DOC) is not None


def test_a_short_document_copied_whole_is_caught():
    short = "Vault code orchid 4471 is kept by the treasurer only, never the desk."
    assert 24 <= len(normalise(short)) < 120
    assert _leak({"q": f"Please check: {short}"}, short) == ("doc_001", "short_document")


def test_csv_lines_passed_in_pieces_are_caught_by_coverage():
    lines = CSV_DOC.split("\n")
    assert all(len(line) < 120 for line in lines)
    body = {"messages": [{"role": "user", "content": line} for line in lines[:4]]}
    assert _leak(body, CSV_DOC) == ("doc_001", "coverage")


def test_a_tiny_document_is_skipped_as_a_stated_limit():
    assert _leak({"q": "yes it is high risk"}, "high risk") is None


def test_an_unknown_lane_mode_is_refused(monkeypatch):
    assert validate_value("lane_mode", "everything_local")
    assert validate_value("lane_max_ngram_share", "2")
    assert validate_value("lane_mode", "raw_local") is None


async def test_an_unknown_lane_mode_fails_loudly(tmp_path, monkeypatch):
    from src.db.schema import create_all

    db = str(tmp_path / "s.db")
    await create_all(db)
    monkeypatch.setenv("BORG_LANE_MODE", "mostly_local")
    with pytest.raises(LaneError) as info:
        await load_lane_settings(db)
    assert info.value.code == "BRT-LANE-005"
