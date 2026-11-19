"""Where the project's files are, found from this file and not from the working directory.

``data/`` (the matrix, the skills, the database), ``ui/`` and ``demo/runs/`` sit beside
``main.py``. The code names several of them as relative paths (``data/borg_route.db``,
``data/skills.yaml``); ``use_project_root`` makes those resolve from the project folder, so
the app starts the same from any working directory (a service manager, a container, an
editor). ``main.py`` calls it before anything else loads.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
UI_DIR = PROJECT_ROOT / "ui"
DATA_DIR = PROJECT_ROOT / "data"
DEMO_RUNS_DIR = PROJECT_ROOT / "demo" / "runs"


def use_project_root() -> Path:
    """Make the project folder the working directory. Idempotent."""
    if Path.cwd().resolve() != PROJECT_ROOT:
        os.chdir(PROJECT_ROOT)
    return PROJECT_ROOT
