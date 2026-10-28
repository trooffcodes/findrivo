"""
core/persistence.py
Small local JSON snapshot store for pipeline job results.

Purpose: keep collected leads and job status so a page reload, stream
disconnect, or process restart can restore the most recent results instead of
showing an empty UI. Retention is bounded (default 24h) and expired snapshots
are pruned on every save and on demand.

Storage design (deliberately simple, no database):
  - one JSON file per job inside Config.RESULTS_DIR,
  - atomic replacement on save (tmp file + os.replace),
  - snapshots are small (<= 50 leads), so per-lead rewrite is cheap.
"""

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import Config
from core.logger import app_logger

_WRITE_LOCK = threading.Lock()

STATUS_RUNNING = "running"
STATUS_INTERRUPTED = "interrupted"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"


def _jobs_dir() -> Path:
    directory = Path(Config.RESULTS_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _valid_task_id(task_id: str) -> bool:
    return bool(task_id) and task_id == task_id.strip() and all(
        ch.isalnum() or ch == "-" for ch in task_id
    )


def _job_path(task_id: str) -> Optional[Path]:
    if not _valid_task_id(task_id):
        return None
    return _jobs_dir() / f"job_{task_id}.json"


def lead_identity(lead: Dict[str, Any]) -> str:
    """Stable identity key used to prevent duplicate records on reconnects."""
    maps_link = str(lead.get("maps_link") or "").split("?")[0]
    if maps_link:
        return "maps:" + maps_link
    digest_source = f"{lead.get('name')}|{lead.get('address')}|{lead.get('phone')}"
    return "fallback:" + hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:16]


def prune_expired(now: Optional[float] = None) -> int:
    """Delete snapshots older than the retention window. Returns removed count."""
    cutoff = (now if now is not None else time.time()) - Config.RESULTS_RETENTION_HOURS * 3600
    removed = 0
    try:
        directory = _jobs_dir()
    except OSError:
        return 0
    try:
        for path in directory.glob("job_*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                updated = float(data.get("updated_at", 0))
            except (OSError, ValueError, json.JSONDecodeError):
                # Unreadable garbage: remove it so it cannot accumulate.
                updated = 0.0
            if updated < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError as err:
                    app_logger.warning("Could not delete expired snapshot %s: %s", path, err)
    except OSError as err:
        app_logger.warning("Snapshot prune failed: %s", err)
    return removed


def save_job(state: Dict[str, Any]) -> Optional[Path]:
    """Atomically persist a job snapshot. Adds/refreshes `updated_at`."""
    task_id = str(state.get("task_id", ""))
    path = _job_path(task_id)
    if path is None:
        return None

    now = time.time()
    state.setdefault("created_at", now)
    state["updated_at"] = now
    payload = json.dumps(state, ensure_ascii=False, default=str)

    try:
        with _WRITE_LOCK:
            tmp = path.with_suffix(f".tmp{os.getpid()}")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
    except OSError as err:
        app_logger.warning("Could not save job snapshot %s: %s", task_id, err)
        return None

    prune_expired(now)
    return path


def load_job(task_id: str) -> Optional[Dict[str, Any]]:
    path = _job_path(task_id)
    if path is None or not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError) as err:
        app_logger.warning("Ignoring unreadable job snapshot %s: %s", task_id, err)
    return None


def latest_job() -> Optional[Dict[str, Any]]:
    """Most recent snapshot by updated_at, regardless of status."""
    try:
        candidates: List[tuple] = []
        for path in _jobs_dir().glob("job_*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    candidates.append((float(data.get("updated_at", 0)), data))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def mark_running_as_interrupted() -> int:
    """Convert 'running' snapshots into 'interrupted' at process startup.

    A job can only be 'running' while its process lives. If a snapshot still
    says running after a restart, the process died mid-job; the collected
    leads are preserved and the status corrected so the UI shows partial
    results instead of pretending a live job exists.
    Call this once during app startup, before any new job begins.
    """
    changed = 0
    try:
        snapshot_paths = list(_jobs_dir().glob("job_*.json"))
    except OSError:
        return 0
    for path in snapshot_paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("status") == STATUS_RUNNING:
            data["status"] = STATUS_INTERRUPTED
            data.setdefault("interrupted_note", "Server restarted before this job finished; partial results kept.")
            if save_job(data) is not None:
                changed += 1
    if changed:
        app_logger.info("Marked %d interrupted job snapshot(s) after restart", changed)
    return changed


def deduplicate_leads(leads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove duplicate leads (by stable identity) from a list, preserving order."""
    seen = set()
    result = []
    for lead in leads:
        key = lead_identity(lead)
        if key in seen:
            continue
        seen.add(key)
        result.append(lead)
    return result


__all__ = [
    "STATUS_RUNNING",
    "STATUS_INTERRUPTED",
    "STATUS_COMPLETE",
    "STATUS_FAILED",
    "lead_identity",
    "prune_expired",
    "save_job",
    "load_job",
    "latest_job",
    "deduplicate_leads",
    "mark_running_as_interrupted",
]
