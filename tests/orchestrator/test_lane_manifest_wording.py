"""The documents manifest names "documents" as the one reserved exception to
the planner prompt's rule that inputs_ref is always a step id (review of the
ruling 9 prompt edit, 16-Nov-2026)."""
from src.lane.context import LaneContext
from src.lane.settings import LaneSettings


def test_manifest_says_documents_is_the_one_reserved_name_not_a_step_id():
    block = LaneContext(settings=LaneSettings(mode="raw_local"), docs=None, db_path=":memory:").manifest_block()
    flat = " ".join(block.split())
    assert '"inputs_ref": "documents" runs once per document' in flat
    assert '"documents" is the one reserved name that is not a step id' in flat
    assert 'do not copy document ids into "items"' in flat
