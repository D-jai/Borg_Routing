"""The lane's settings: config key first, then env, then the default.

Same resolution order as every other engine setting (``get_config`` pattern).
Read once per run, when the run starts (``prepare_lane``). ``lane_mode`` is
read first; while it is ``off`` no other lane setting is read or validated.

| Config key              | Env                          | Default | Meaning |
|-------------------------|------------------------------|---------|---------|
| lane_mode               | BORG_LANE_MODE               | off     | off, raw_local, all_local or by_reference |
| lane_max_output_chars   | BORG_LANE_MAX_OUTPUT_CHARS   | 1500    | P2 size cap per raw item |
| lane_max_verbatim_chars | BORG_LANE_MAX_VERBATIM_CHARS | 120     | longest passage copied from a document |
| lane_max_ngram_share    | BORG_LANE_MAX_NGRAM_SHARE    | 0.10    | backstop coverage limit (word 8-grams) |
| lane_manifest           | BORG_LANE_MANIFEST           | names   | names, or ids (the planner sees no file names) |
| lane_local_allowlist    | BORG_LANE_LOCAL_ALLOWLIST    | (unset) | model ids and/or host:port that count as local |
| lane_part_chars         | BORG_LANE_PART_CHARS         | 8000    | a document longer than this is split into parts |

``BORG_DOCS_ROOT`` and ``BORG_LOCAL_HOSTS`` are env only (``.env``).

Error codes:
- BRT-LANE-005: an unknown lane_mode value (refused, never guessed)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from src.registry.locality import local_hosts_from_env, parse_list

from .errors import LaneError

LANE_MODES = ("off", "raw_local", "all_local", "by_reference")
MANIFEST_MODES = ("names", "ids")

# Defaults. Primary: single developer running locally with small text files.
# Override: the config keys / env variables in the table above.
_DEFAULTS = {
    "lane_mode": "off",
    "lane_max_output_chars": 1500,
    "lane_max_verbatim_chars": 120,
    "lane_max_ngram_share": 0.10,
    "lane_manifest": "names",
    "lane_local_allowlist": "",
    "lane_part_chars": 8000,
}
_ENV = {
    "lane_mode": "BORG_LANE_MODE",
    "lane_max_output_chars": "BORG_LANE_MAX_OUTPUT_CHARS",
    "lane_max_verbatim_chars": "BORG_LANE_MAX_VERBATIM_CHARS",
    "lane_max_ngram_share": "BORG_LANE_MAX_NGRAM_SHARE",
    "lane_manifest": "BORG_LANE_MANIFEST",
    "lane_local_allowlist": "BORG_LANE_LOCAL_ALLOWLIST",
    "lane_part_chars": "BORG_LANE_PART_CHARS",
}
SETTING_KEYS = tuple(_DEFAULTS)


@dataclass(frozen=True)
class LaneSettings:
    mode: str = "off"
    max_output_chars: int = 1500
    max_verbatim_chars: int = 120
    max_ngram_share: float = 0.10
    manifest: str = "names"
    local_allowlist: tuple[str, ...] = ()
    part_chars: int = 8000
    local_hosts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def on(self) -> bool:
        return self.mode != "off"

    @property
    def keeps_raw_local(self) -> bool:
        """True in ``raw_local`` and ``all_local``: steps that read documents
        stay local and the output guard and content backstop run. False in
        ``by_reference``, which hands documents over the same way but lets
        any checked model read them and makes no privacy promise."""
        return self.mode in ("raw_local", "all_local")


def validate_value(key: str, value: object) -> Optional[str]:
    """None when ``value`` is acceptable for lane setting ``key``, else why not."""
    text = str(value).strip().lower()
    if key == "lane_mode":
        return None if text in LANE_MODES else f"lane_mode must be one of {list(LANE_MODES)}"
    if key == "lane_manifest":
        return None if text in MANIFEST_MODES else f"lane_manifest must be one of {list(MANIFEST_MODES)}"
    if key in ("lane_max_output_chars", "lane_max_verbatim_chars", "lane_part_chars"):
        try:
            return None if int(float(text)) >= 1 else f"{key} must be a positive whole number"
        except ValueError:
            return f"{key} must be a positive whole number"
    if key == "lane_max_ngram_share":
        try:
            share = float(text)
        except ValueError:
            return f"{key} must be a number between 0 and 1"
        return None if 0 < share <= 1 else f"{key} must be a number between 0 and 1"
    return None


async def _raw(key: str, db_path: str):
    from src.db.state import get_config  # noqa: PLC0415 - avoid import cycle

    configured = await get_config(key, db_path=db_path)
    if configured is not None and str(configured).strip() != "":
        return configured
    env = os.getenv(_ENV[key])
    if env is not None and env.strip() != "":
        return env
    return _DEFAULTS[key]


async def load_lane_settings(db_path: str) -> LaneSettings:
    """Resolve the lane settings. An invalid value raises (G4: no guessing).

    ``lane_mode`` is read and validated first. When it is ``off`` nothing
    else is read or validated (no other config row, no other env variable),
    so a run with the lane off is exactly Phase 1 whatever the other lane
    settings hold.

    Raises:
        LaneError: BRT-LANE-005 for an unknown lane_mode or manifest value,
            or a limit that is not a valid number (the last two only while
            the lane is on).
    """
    mode_raw = await _raw("lane_mode", db_path)
    problem = validate_value("lane_mode", mode_raw)
    if problem:
        raise LaneError("BRT-LANE-005", f"invalid lane setting: {problem}")
    if str(mode_raw).strip().lower() == "off":
        return LaneSettings(mode="off")
    values = {"lane_mode": mode_raw}
    for key in SETTING_KEYS:
        if key == "lane_mode":
            continue
        raw = await _raw(key, db_path)
        problem = validate_value(key, raw) if key != "lane_local_allowlist" else None
        if problem:
            raise LaneError("BRT-LANE-005", f"invalid lane setting: {problem}")
        values[key] = raw
    return LaneSettings(
        mode=str(values["lane_mode"]).strip().lower(),
        max_output_chars=int(float(values["lane_max_output_chars"])),
        max_verbatim_chars=int(float(values["lane_max_verbatim_chars"])),
        max_ngram_share=float(values["lane_max_ngram_share"]),
        manifest=str(values["lane_manifest"]).strip().lower(),
        local_allowlist=tuple(parse_list(
            ",".join(map(str, values["lane_local_allowlist"]))
            if isinstance(values["lane_local_allowlist"], list)
            else values["lane_local_allowlist"]
        )),
        part_chars=int(float(values["lane_part_chars"])),
        local_hosts=tuple(local_hosts_from_env()),
    )
