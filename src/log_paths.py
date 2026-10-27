"""Date-partitioned experiment paths, consistently using Beijing time."""

from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo


LOG_TIMEZONE = ZoneInfo("Asia/Shanghai")


def dated_directory(root: str | Path, *, timestamp: datetime | None = None) -> Path:
    """Create a YYYY-MM-DD partition; an explicit timestamp must be timezone-aware."""
    timestamp = timestamp if timestamp is not None else datetime.now(LOG_TIMEZONE)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    directory = Path(root) / timestamp.astimezone(LOG_TIMEZONE).strftime("%Y-%m-%d")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def create_run_directory(root: str | Path = "workspace", *, timestamp: datetime | None = None) -> Path:
    """Create one experiment, including its logs directory, at the current date."""
    directory = dated_directory(root, timestamp=timestamp) / f"run_{uuid4().hex}"
    directory.mkdir()
    (directory / "logs").mkdir()
    return directory


def new_log_path(name: str, *, root: str | Path = "workspace",
                 run_dir: str | Path | None = None, suffix: str = ".json") -> Path:
    """Return a unique path; callers open it with 'x' to prevent overwrites."""
    if suffix not in {".json", ".log"}:
        raise ValueError("log suffix must be .json or .log")
    timestamp = datetime.now(LOG_TIMEZONE)
    if run_dir is None:
        run_dir = create_run_directory(root, timestamp=timestamp)
    directory = Path(run_dir) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name) or "agent"
    return directory / f"{safe_name}_{timestamp:%Y%m%d_%H%M%S_%f}_{uuid4().hex}{suffix}"
