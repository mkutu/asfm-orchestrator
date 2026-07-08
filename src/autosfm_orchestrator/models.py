from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ImageRecord:
    """One current developed image selected from globus_file_index."""

    batch_id: str
    image_id: str
    filename: str
    source_path: Path
    timestamp: int | None = None
    file_id: int | None = None
    endpoint: str | None = None
    site: str | None = None
    storage_domain: str | None = None
    namespace: str | None = None
    storage_root: Path | None = None
    rel_path: Path | None = None
    data_state: str | None = None
    size_bytes: int | None = None
    mtime_iso: str | None = None


@dataclass(frozen=True)
class ReferenceRecord:
    """One reference file selected from globus_file_index or config fallback."""

    source_path: Path
    filename: str
    file_id: int | None = None
    endpoint: str | None = None
    site: str | None = None
    storage_root: Path | None = None
    rel_path: Path | None = None


@dataclass(frozen=True)
class ManualLogEntry:
    batch_id: str
    start_time: str
    end_time: str
    sub_batch_id: str | None = None
    notes: str | None = None
    cam_angle: str | None = None
    z_axis: str | None = None
    season: str | None = None


@dataclass
class RunPaths:
    run_root: Path
    lock_path: Path
    manifest_path: Path
    inputs_dir: Path
    images_dir: Path
    reference_dir: Path
    autosfm_dir: Path
    project_dir: Path
    refs_dir: Path
    pixel_grid_dir: Path
    logs_dir: Path
    outputs_dir: Path


@dataclass
class AutoSfmOutputs:
    project_dir: Path
    pixel_grid_dir: Path
    fov_csv: Path
    camera_reference_csv: Path
    gcp_reference_csv: Path
    error_statistics_csv: Path
    report_pdf: Path
    manifest_path: Path
    logs_dir: Path


@dataclass
class TransferItem:
    source: Path
    destination: Path


@dataclass
class TransferPlan:
    label: str
    source_endpoint: str
    destination_endpoint: str
    items: list[TransferItem] = field(default_factory=list)


@dataclass
class AutoSfmRun:
    run_id: str
    batch_id: str
    start_time: str
    end_time: str
    sub_batch_id: str
    paths: RunPaths
    image_records: list[ImageRecord]
    source_reference_path: Path | None
    ceres_output_path: Path
    reference_record: ReferenceRecord | None = None
    notes: str | None = None
    cam_angle: str | None = None
    z_axis: str | None = None
    season: str | None = None
    status: str = "planned"

    def to_manifest_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return _stringify_paths(data)


def _stringify_paths(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _stringify_paths(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_stringify_paths(item) for item in value]
    return value
