"""
BORG_ROUTE prompt layer.

Every model call in the system MUST route through
`build_system_prompt()` in `base.py`. This is how Law of Uncompacted
Governance is enforced — there is no other path.

Exports:
- build_system_prompt
- load_governance
- ORCHESTRATOR_SYSTEM_PROMPT (unified plan/re_plan/compose)
- ANSWERER_SYSTEM_PROMPT
"""

from .base import build_system_prompt, load_governance
from .orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
from .answerer import ANSWERER_SYSTEM_PROMPT

__all__ = [
    "build_system_prompt",
    "load_governance",
    "ORCHESTRATOR_SYSTEM_PROMPT",
    "ANSWERER_SYSTEM_PROMPT",
]
