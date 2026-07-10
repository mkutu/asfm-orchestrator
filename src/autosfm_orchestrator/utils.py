from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

LOG_FORMAT = "[%(asctime)s][%(name)s][%(levelname)s] - %(message)s"


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=getattr(logging, level.upper()), format=LOG_FORMAT)


@contextmanager
def log_to_file(log_paths: Path | list[Path], level: str = "INFO") -> Iterator[list[Path]]:
    """Attach a file handler for each path in `log_paths` (a single Path or
    list of Paths) to the root logger for the duration of the `with` block,
    so any logging done via the standard `logging` module (which everything
    in this codebase already uses) is also written to each of those files.
    All handlers are removed and closed on exit, even on error.

    This is meant to be used around a unit of work (e.g. a single AutoSfM
    run) whose log files live under the run's `logs_dir` (NFS, promoted to
    CERES) and/or its local-scratch equivalent (for on-SUNNY review).
    """
    paths = [log_paths] if isinstance(log_paths, Path) else list(log_paths)
    numeric_level = getattr(logging, level.upper())

    handlers: list[logging.FileHandler] = []
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setLevel(numeric_level)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handlers.append(handler)

    root = logging.getLogger()
    # The root logger's own level gates messages before handlers ever see
    # them, so if it's currently less verbose than `level` (e.g. console
    # logging was configured at WARNING), lower it for the duration of the
    # block or these handlers would never receive anything.
    previous_level = root.level
    if root.level == logging.NOTSET or root.level > numeric_level:
        root.setLevel(numeric_level)

    for handler in handlers:
        root.addHandler(handler)
    try:
        yield paths
    finally:
        for handler in handlers:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(previous_level)


def sanitize_for_path(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")


def normalize_time_str(time_str: str) -> str:
    clean = time_str.strip().upper()
    clean = re.sub(r"(?<=\d)(AM|PM)$", r" \1", clean)
    for fmt in ("%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(clean, fmt).strftime("%H:%M:%S")
        except ValueError:
            continue
    raise ValueError(f"Unrecognized time format: {time_str}")


def time_window_to_epoch(batch_id: str, start_time: str, end_time: str) -> tuple[int, int]:
    batch_date = _batch_date(batch_id)
    start = _combine_utc(batch_date, normalize_time_str(start_time))
    end = _combine_utc(batch_date, normalize_time_str(end_time))
    if end < start:
        end += timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def _batch_date(batch_id: str) -> date:
    try:
        date_part = batch_id.split("_")[-1]
        return datetime.strptime(date_part, "%Y-%m-%d").date()
    except Exception as exc:
        raise ValueError(f"Batch ID must end with YYYY-MM-DD: {batch_id}") from exc


def _combine_utc(day: date, hms: str) -> datetime:
    parsed = datetime.strptime(hms, "%H:%M:%S").time()
    return datetime(day.year, day.month, day.day, parsed.hour, parsed.minute, parsed.second, tzinfo=timezone.utc)


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        yaml.safe_dump(data, file, sort_keys=False)


def read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}
