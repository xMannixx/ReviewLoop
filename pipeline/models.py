"""
Data models for the Pipeline Dashboard.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


PHASE_IDS = [
    "review_task",
    "parallel_review",
    "consolidation",
    "cursor_task",
    "build",
]

PHASE_LABELS = {
    "review_task":     "Phase 1 — Review-Auftrag erstellen",
    "parallel_review": "Phase 2 — Parallel Review",
    "consolidation":   "Phase 3 — Konsolidierung",
    "cursor_task":     "Phase 4 — Cursor-Auftrag generieren",
    "build":           "Phase 5 — Build (Cursor-Bruecke)",
}

# Valid status values per phase
VALID_STATUSES = {"pending", "running", "review", "approved", "rejected"}


@dataclass
class Phase:
    id: str
    status: str = "pending"
    result: dict[str, Any] | None = None
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None

    @property
    def label(self) -> str:
        return PHASE_LABELS.get(self.id, self.id)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "result": self.result,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Phase":
        return cls(
            id=d["id"],
            status=d.get("status", "pending"),
            result=d.get("result"),
            started_at=d.get("started_at"),
            completed_at=d.get("completed_at"),
            error=d.get("error"),
        )


@dataclass
class PipelineRun:
    run_id: str
    title: str
    created_at: str
    description: str = ""
    input_text: str = ""
    input_files: list[str] = field(default_factory=list)
    phases: list[Phase] = field(default_factory=list)
    # Systemprompt-Override fuer den Orchestrator (alle 3 Orchestrator-Phasen)
    km_system_prompt: str = ""
    # Current State Sheet — wird allen Reviewern als Kontext mitgegeben
    context_sheet: str = ""

    def __post_init__(self):
        if not self.phases:
            self.phases = [Phase(id=pid) for pid in PHASE_IDS]

    @property
    def current_phase_idx(self) -> int:
        """Index of the first non-approved phase, or -1 if all done."""
        for i, phase in enumerate(self.phases):
            if phase.status != "approved":
                return i
        return -1

    @property
    def overall_status(self) -> str:
        statuses = [p.status for p in self.phases]
        if all(s == "approved" for s in statuses):
            return "completed"
        if any(s == "running" for s in statuses):
            return "running"
        if any(s == "review" for s in statuses):
            return "review"
        if any(s == "rejected" for s in statuses):
            return "rejected"
        return "pending"

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "title": self.title,
            "created_at": self.created_at,
            "description": self.description,
            "input_text": self.input_text,
            "input_files": self.input_files,
            "phases": [p.to_dict() for p in self.phases],
            "current_phase_idx": self.current_phase_idx,
            "overall_status": self.overall_status,
            "km_system_prompt": self.km_system_prompt,
            "context_sheet": self.context_sheet,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PipelineRun":
        run = cls(
            run_id=d["run_id"],
            title=d["title"],
            created_at=d["created_at"],
            description=d.get("description", ""),
            input_text=d.get("input_text", ""),
            input_files=d.get("input_files", []),
            phases=[Phase.from_dict(p) for p in d.get("phases", [])],
            km_system_prompt=d.get("km_system_prompt", ""),
            context_sheet=d.get("context_sheet", ""),
        )
        return run

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "PipelineRun":
        return cls.from_dict(json.loads(text))
