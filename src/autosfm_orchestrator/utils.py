from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

LOG_FORMAT = "[%(asctime)s][%(name)s][%(levelname)s] - %(message)s"


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=getattr(logging, level.upper()), format=LOG_FORMAT)


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
