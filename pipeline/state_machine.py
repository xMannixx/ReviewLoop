"""
Pipeline State Machine.
Manages phase transitions, JSON persistence, and audit log.

State diagram per phase:
  pending -> running (start_phase)
  running -> review  (phase_complete)
  review  -> approved (approve_phase)
  review  -> rejected (reject_phase)
  rejected -> running  (retry_phase)
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.models import PipelineRun


BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
AUDIT_FILE = DATA_DIR / "audit.jsonl"
AUDIT_MAX_BYTES = 10 * 1024 * 1024
AUDIT_BACKUP_COUNT = 10

DATA_DIR.mkdir(exist_ok=True)

_lock = threading.Lock()


# ── Persistence ─────────────────────────────────────────────────────────────

def save_run(run: PipelineRun) -> None:
    path = DATA_DIR / f"{run.run_id}.json"
    with _lock:
        path.write_text(run.to_json(), encoding="utf-8")


def _find_run_file(run_id: str) -> Path | None:
    """
    Locate the JSON file for a run_id.
    Handles sync-conflict filenames like '<run_id> (# Edit conflict ...).json'
    by falling back to a glob search when the canonical path doesn't exist.
    """
    canonical = DATA_DIR / f"{run_id}.json"
    if canonical.exists():
        return canonical
    # Fallback: OneDrive / Nextcloud conflict copies
    matches = list(DATA_DIR.glob(f"{run_id}*.json"))
    if matches:
        # Prefer the canonical name if somehow it shows up; otherwise take newest
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return matches[0]
    return None


def load_run(run_id: str) -> PipelineRun | None:
    path = _find_run_file(run_id)
    if path is None:
        return None
    try:
        return PipelineRun.from_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_run_file(path: "Path") -> PipelineRun | None:
    """Load a PipelineRun directly from a file path (used by stats aggregation)."""
    try:
        return PipelineRun.from_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_runs() -> list[dict]:
    """Return all runs as summary dicts, sorted newest-first."""
    seen_ids: set[str] = set()
    runs = []
    for f in sorted(DATA_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            run = PipelineRun.from_json(f.read_text(encoding="utf-8"))
            if run.run_id in seen_ids:
                # Duplicate from a sync-conflict file — delete the conflict copy
                if f.name != f"{run.run_id}.json":
                    f.unlink()
                continue
            seen_ids.add(run.run_id)
            runs.append({
                "run_id": run.run_id,
                "title": run.title,
                "created_at": run.created_at,
                "overall_status": run.overall_status,
                "current_phase_idx": run.current_phase_idx,
            })
        except Exception:
            pass
    return runs


def delete_run(run_id: str) -> bool:
    """Delete all JSON files for a run_id (including sync-conflict copies)."""
    deleted = False
    # Remove canonical file
    canonical = DATA_DIR / f"{run_id}.json"
    if canonical.exists():
        _audit(run_id, "run_deleted")
        canonical.unlink()
        deleted = True
    # Also remove any conflict copies
    for conflict in DATA_DIR.glob(f"{run_id}*.json"):
        conflict.unlink()
        deleted = True
    return deleted


def abort_run(run_id: str) -> PipelineRun | None:
    """Abort all running phases in a run (sets them back to pending)."""
    run = load_run(run_id)
    if run is None:
        return None
    changed = False
    for i, phase in enumerate(run.phases):
        if phase.status == "running":
            phase.status = "pending"
            phase.error = None
            phase.started_at = None
            changed = True
    if changed:
        save_run(run)
        _audit(run_id, "run_aborted")
    return run


# ── Audit Log ────────────────────────────────────────────────────────────────

def _rotate_audit_if_needed() -> None:
    if not AUDIT_FILE.exists():
        return
    if AUDIT_FILE.stat().st_size < AUDIT_MAX_BYTES:
        return

    oldest = AUDIT_FILE.with_name(f"{AUDIT_FILE.name}.{AUDIT_BACKUP_COUNT}")
    if oldest.exists():
        oldest.unlink()

    for idx in range(AUDIT_BACKUP_COUNT - 1, 0, -1):
        src = AUDIT_FILE.with_name(f"{AUDIT_FILE.name}.{idx}")
        dst = AUDIT_FILE.with_name(f"{AUDIT_FILE.name}.{idx + 1}")
        if src.exists():
            src.replace(dst)

    AUDIT_FILE.replace(AUDIT_FILE.with_name(f"{AUDIT_FILE.name}.1"))


def _audit(run_id: str, action: str, phase_idx: int | None = None, extra: dict | None = None) -> None:
    entry = {
        "ts": datetime.now().isoformat(),
        "run_id": run_id,
        "action": action,
        "phase_idx": phase_idx,
    }
    if extra:
        entry.update(extra)
    with _lock:
        _rotate_audit_if_needed()
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── Run Lifecycle ────────────────────────────────────────────────────────────

def create_run(
    title: str,
    description: str = "",
    input_text: str = "",
    input_files: list[str] | None = None,
    km_system_prompt: str = "",
    context_sheet: str = "",
) -> PipelineRun:
    run = PipelineRun(
        run_id=str(uuid.uuid4()),
        title=title,
        created_at=datetime.now().isoformat(),
        description=description,
        input_text=input_text,
        input_files=input_files or [],
        km_system_prompt=km_system_prompt,
        context_sheet=context_sheet,
    )
    save_run(run)
    _audit(run.run_id, "run_created", extra={"title": title})
    return run


# ── Phase Transitions ────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().isoformat()


def mark_phase_running(run_id: str, phase_idx: int) -> PipelineRun | None:
    run = load_run(run_id)
    if run is None:
        return None
    phase = run.phases[phase_idx]
    phase.status = "running"
    phase.started_at = _now()
    phase.error = None
    save_run(run)
    _audit(run_id, "phase_started", phase_idx)
    return run


def mark_phase_review(run_id: str, phase_idx: int, result: dict[str, Any]) -> PipelineRun | None:
    """Called by the background worker when a phase finishes successfully."""
    run = load_run(run_id)
    if run is None:
        return None
    phase = run.phases[phase_idx]
    # If phase was cancelled while running, don't overwrite the pending state
    if phase.status != "running":
        return run
    phase.status = "review"
    phase.completed_at = _now()
    phase.result = result
    phase.error = None
    save_run(run)
    _audit(run_id, "phase_ready_for_review", phase_idx)
    return run


def mark_phase_error(run_id: str, phase_idx: int, error: str) -> PipelineRun | None:
    """Called when a phase fails -- puts it back to review with error flag."""
    run = load_run(run_id)
    if run is None:
        return None
    phase = run.phases[phase_idx]
    if phase.status != "running":
        return run
    phase.status = "review"
    phase.completed_at = _now()
    phase.error = error
    save_run(run)
    _audit(run_id, "phase_error", phase_idx, {"error": error[:200]})
    return run


def approve_phase(run_id: str, phase_idx: int, edited_result: dict | None = None) -> PipelineRun | None:
    """A user approves a phase. Optionally saves an edited result."""
    run = load_run(run_id)
    if run is None:
        return None
    phase = run.phases[phase_idx]
    if edited_result is not None:
        phase.result = edited_result
    phase.status = "approved"
    save_run(run)
    _audit(run_id, "phase_approved", phase_idx)
    return run


def reject_phase(run_id: str, phase_idx: int, reason: str = "") -> PipelineRun | None:
    """A user rejects a phase."""
    run = load_run(run_id)
    if run is None:
        return None
    phase = run.phases[phase_idx]
    phase.status = "rejected"
    save_run(run)
    _audit(run_id, "phase_rejected", phase_idx, {"reason": reason})
    return run


def cancel_phase(run_id: str, phase_idx: int) -> PipelineRun | None:
    """Cancel a running phase — resets to pending. Background thread checks status before writing."""
    run = load_run(run_id)
    if run is None:
        return None
    phase = run.phases[phase_idx]
    phase.status = "pending"
    phase.error = None
    phase.started_at = None
    save_run(run)
    _audit(run_id, "phase_cancelled", phase_idx)
    return run


def retry_phase(run_id: str, phase_idx: int) -> PipelineRun | None:
    """Reset phase to pending so it can be re-started."""
    run = load_run(run_id)
    if run is None:
        return None
    phase = run.phases[phase_idx]
    phase.status = "pending"
    phase.result = None
    phase.error = None
    phase.started_at = None
    phase.completed_at = None
    save_run(run)
    _audit(run_id, "phase_retry", phase_idx)
    return run


def update_phase_result(run_id: str, phase_idx: int, result: dict) -> PipelineRun | None:
    """A user edited a result in-place (without approving yet)."""
    run = load_run(run_id)
    if run is None:
        return None
    run.phases[phase_idx].result = result
    save_run(run)
    _audit(run_id, "phase_result_edited", phase_idx)
    return run
