"""CapabilityMatrix — load, query, and persist the model capability matrix.

The matrix is the authoritative store for all known models. It is backed by a
YAML file so operators can inspect and hand-edit it. At runtime, discovery and
benchmark runs call upsert() / update_score() / update_throughput() and the
file is kept in sync.

Error codes:
- BRT-REG-010: capability_matrix.yaml not found
- BRT-REG-011: malformed YAML (parse error or wrong top-level type)
- BRT-REG-012: unknown model_id on update operations
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

import yaml

from .models import ModelInfo


class CapabilityMatrix:
    def __init__(self, path: str = "data/capability_matrix.yaml") -> None:
        self._path = Path(path)
        self._models: dict[str, ModelInfo] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load (or reload) the YAML file into memory.

        Raises:
            FileNotFoundError: BRT-REG-010 — file does not exist.
            ValueError: BRT-REG-011 — file is not a valid YAML list of model records.
        """
        if not self._path.exists():
            raise FileNotFoundError(
                f"BRT-REG-010: capability matrix not found at '{self._path}'. "
                "Run 'python -m pip install -e .[dev]' and ensure data/ directory exists."
            )
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(
                f"BRT-REG-011: capability matrix YAML parse error — {exc}"
            ) from exc

        if not isinstance(raw, list):
            raise ValueError(
                "BRT-REG-011: capability_matrix.yaml must be a YAML list of model records. "
                f"Got {type(raw).__name__}."
            )

        with self._lock:
            self._models = {}
            for record in raw:
                m = ModelInfo(**record)
                self._models[m.model_id] = m

    def save(self) -> None:
        """Persist the current in-memory matrix back to YAML."""
        with self._lock:
            records = [m.model_dump(exclude_none=False) for m in self._models.values()]

        self._path.write_text(
            yaml.dump(records, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def all(self) -> list[ModelInfo]:
        with self._lock:
            return list(self._models.values())

    def get(self, model_id: str) -> Optional[ModelInfo]:
        with self._lock:
            return self._models.get(model_id)

    def query_by_task(
        self,
        task_type: str,
        min_score: float = 0.0,
        min_context_length: int = 0,
    ) -> list[ModelInfo]:
        """Return models that have a task_score >= min_score for task_type
        and context_length >= min_context_length.

        Models without a task_scores entry for task_type are excluded.
        Results are returned in descending score order.
        """
        with self._lock:
            candidates = [
                m for m in self._models.values()
                if task_type in m.task_scores
                and m.task_scores[task_type] >= min_score
                and m.context_length >= min_context_length
            ]
        candidates.sort(key=lambda m: m.task_scores[task_type], reverse=True)
        return candidates

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def upsert(self, model: ModelInfo) -> None:
        """Insert or fully replace a model record."""
        with self._lock:
            self._models[model.model_id] = model

    def update_score(self, model_id: str, task_type: str, score: float) -> None:
        """Update the task_score for a single task type on an existing model.

        Raises:
            KeyError: BRT-REG-012 — model_id not in matrix.
        """
        with self._lock:
            m = self._models.get(model_id)
            if m is None:
                raise KeyError(
                    f"BRT-REG-012: model '{model_id}' not found in capability matrix. "
                    "Run discovery first or add the model manually."
                )
            m.task_scores[task_type] = score

    def update_throughput(self, model_id: str, tps: float) -> None:
        """Update the throughput_tps for an existing model.

        Raises:
            KeyError: BRT-REG-012 — model_id not in matrix.
        """
        with self._lock:
            m = self._models.get(model_id)
            if m is None:
                raise KeyError(
                    f"BRT-REG-012: model '{model_id}' not found in capability matrix."
                )
            m.throughput_tps = tps
