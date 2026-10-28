"""
core/maintenance.py
Clear-start helpers for saved job data and application logs.

Deletion is restricted to the explicitly designated FindRivo runtime
directories (job snapshots, discovery progress, log files). Source code,
configuration, credentials, environment files, and exports are never
touched.
"""

from pathlib import Path
from typing import Dict, List

from config import Config
from core.logger import app_logger


def _within(directory: Path, path: Path) -> bool:
    """True only when `path` resolves inside `directory` (no path traversal)."""
    try:
        path.resolve().relative_to(directory.resolve())
    except (ValueError, OSError):
        return False
    return True


def clear_saved_data() -> Dict[str, object]:
    """Remove persisted lead snapshots and discovery progress.

    Exports are intentionally NOT deleted: they are user-generated output.
    """
    deleted: List[str] = []
    errors: List[str] = []

    results_dir = Path(Config.RESULTS_DIR)
    if results_dir.is_dir():
        targets = list(results_dir.glob("job_*.json")) + list(results_dir.glob("*.tmp*"))
        for path in targets:
            if not _within(results_dir, path):
                app_logger.warning("Refusing to delete outside results dir: %s", path)
                continue
            try:
                path.unlink()
                deleted.append(path.name)
            except OSError as err:
                errors.append(f"snapshot {path.name}: {err}")
                app_logger.warning("Could not delete snapshot %s: %s", path, err)

    progress = Path(Config.PROGRESS_FILE)
    for target in (progress, Path(str(progress) + ".lock")):
        try:
            if target.exists() and _within(progress.parent, target):
                target.unlink()
                deleted.append(target.name)
        except OSError as err:
            errors.append(f"progress {target.name}: {err}")

    if errors:
        app_logger.error("Clearing saved data finished with errors: %s", errors)
    else:
        app_logger.info("Cleared %d saved data file(s): %s", len(deleted), deleted)
    return {"deleted": deleted, "errors": errors}


def clear_logs() -> Dict[str, object]:
    """Truncate or remove files inside the configured log directory.

    The active log handlers keep the current log files open, so they are
    truncated in place instead of deleted (deletion fails on Windows while
    a handle is open). Rotated backups are truncated the same way.
    """
    log_dir = Path(Config.LOG_DIR)
    touched: List[str] = []
    errors: List[str] = []

    if not log_dir.is_dir():
        return {"cleared": touched, "errors": errors}

    for path in sorted(log_dir.glob("*.log*")):
        if not path.is_file() or not _within(log_dir, path):
            continue
        try:
            with open(path, "r+b") as handle:
                handle.truncate(0)
            touched.append(path.name)
        except OSError as err:
            errors.append(f"log {path.name}: {err}")
            app_logger.warning("Could not clear log file %s: %s", path, err)

    if errors:
        app_logger.error("Clearing logs finished with errors: %s", errors)
    else:
        app_logger.info("Cleared %d log file(s): %s", len(touched), touched)
    return {"cleared": touched, "errors": errors}


__all__ = ["clear_saved_data", "clear_logs"]
