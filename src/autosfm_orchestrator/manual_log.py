from __future__ import annotations

import csv
from pathlib import Path

from autosfm_orchestrator.models import ManualLogEntry
from autosfm_orchestrator.utils import normalize_time_str

_SKIP_VALUES = {"0", "false", "no", "n", "skip", "skipped"}


class ManualLogError(ValueError):
    pass


def read_manual_log(path: str | Path) -> list[ManualLogEntry]:
    log_path = Path(path).expanduser().resolve()
    if not log_path.exists():
        raise ManualLogError(f"Manual log does not exist: {log_path}")

    entries: list[ManualLogEntry] = []
    with log_path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            raise ManualLogError(f"Manual log has no header: {log_path}")

        required = {"batch_id", "start_time", "end_time"}
        missing = required.difference(reader.fieldnames)
        if missing:
            raise ManualLogError(f"Manual log missing required columns: {sorted(missing)}")

        for row_index, row in enumerate(reader, start=2):
            if _should_skip(row):
                continue
            try:
                entries.append(_row_to_entry(row))
            except Exception as exc:
                raise ManualLogError(f"Invalid manual log row {row_index}: {row}") from exc

    return entries


def _should_skip(row: dict[str, str | None]) -> bool:
    process = (row.get("process") or "").strip().lower()
    return process in _SKIP_VALUES


def _row_to_entry(row: dict[str, str | None]) -> ManualLogEntry:
    batch_id = _required(row, "batch_id")
    start_time = normalize_time_str(_required(row, "start_time"))
    end_time = normalize_time_str(_required(row, "end_time"))
    sub_batch_id = _optional(row, "sub_batch_id")

    return ManualLogEntry(
        batch_id=batch_id,
        start_time=start_time,
        end_time=end_time,
        sub_batch_id=sub_batch_id,
        notes=_optional(row, "notes"),
        cam_angle=_optional(row, "cam_angle"),
        z_axis=_optional(row, "z_axis"),
        season=_optional(row, "season"),
    )


def _required(row: dict[str, str | None], key: str) -> str:
    value = (row.get(key) or "").strip()
    if not value:
        raise ManualLogError(f"Missing required value: {key}")
    return value


def _optional(row: dict[str, str | None], key: str) -> str | None:
    value = (row.get(key) or "").strip()
    return value or None
