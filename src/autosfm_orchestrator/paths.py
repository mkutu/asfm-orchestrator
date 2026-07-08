from __future__ import annotations

from pathlib import Path

from autosfm_orchestrator.models import AutoSfmRun, ImageRecord, ManualLogEntry, ReferenceRecord, RunPaths
from autosfm_orchestrator.utils import sanitize_for_path


def make_run_id(entry: ManualLogEntry) -> str:
    start = sanitize_for_path(entry.start_time.replace(":", ""))
    end = sanitize_for_path(entry.end_time.replace(":", ""))
    sub = sanitize_for_path(entry.sub_batch_id) if entry.sub_batch_id else f"{start}_{end}"
    return f"{entry.batch_id}__{sub}"


def make_run_paths(config: dict, entry: ManualLogEntry) -> RunPaths:
    staging_root = Path(config["paths"]["sunny_staging_root"]).expanduser()
    run_id = make_run_id(entry)
    run_root = staging_root / entry.batch_id / run_id.split("__", 1)[1]

    inputs_dir = run_root / "inputs"
    autosfm_dir = run_root / "autosfm"
    refs_dir = autosfm_dir / "reference"

    return RunPaths(
        run_root=run_root,
        lock_path=run_root / ".lock",
        manifest_path=run_root / "manifest.yaml",
        inputs_dir=inputs_dir,
        images_dir=inputs_dir / "images",
        reference_dir=inputs_dir / "reference",
        autosfm_dir=autosfm_dir,
        project_dir=autosfm_dir / "project",
        refs_dir=refs_dir,
        pixel_grid_dir=refs_dir / "pixel_world_grids",
        logs_dir=run_root / "logs",
        outputs_dir=run_root / "outputs",
    )


def build_run(
    config: dict,
    entry: ManualLogEntry,
    image_records: list[ImageRecord],
    reference_record: ReferenceRecord | None = None,
) -> AutoSfmRun:
    run_paths = make_run_paths(config, entry)
    run_id = make_run_id(entry)
    sub_batch_id = entry.sub_batch_id or run_id.split("__", 1)[1]
    ceres_output_root = Path(config["paths"]["ceres_output_root"])
    ceres_output_path = ceres_output_root / entry.batch_id / sub_batch_id

    source_reference_path = reference_record.source_path if reference_record else _reference_fallback(config, entry)

    return AutoSfmRun(
        run_id=run_id,
        batch_id=entry.batch_id,
        start_time=entry.start_time,
        end_time=entry.end_time,
        sub_batch_id=sub_batch_id,
        paths=run_paths,
        image_records=image_records,
        source_reference_path=source_reference_path,
        ceres_output_path=ceres_output_path,
        reference_record=reference_record,
        notes=entry.notes,
        cam_angle=entry.cam_angle,
        z_axis=entry.z_axis,
        season=entry.season,
    )


def _reference_fallback(config: dict, entry: ManualLogEntry) -> Path | None:
    reference_root = config["paths"].get("ceres_reference_root")
    if not reference_root:
        return None
    state = entry.batch_id.split("_", 1)[0]
    season = entry.season or config.get("database", {}).get("reference_filters", {}).get("season")
    if season:
        return Path(reference_root) / season / f"{state}.csv"
    return Path(reference_root) / f"{state}.csv"


def create_workspace(paths: RunPaths) -> None:
    for path in [
        paths.run_root,
        paths.inputs_dir,
        paths.images_dir,
        paths.reference_dir,
        paths.autosfm_dir,
        paths.project_dir,
        paths.refs_dir,
        paths.pixel_grid_dir,
        paths.logs_dir,
        paths.outputs_dir,
    ]:
        path.mkdir(parents=True, exist_ok=True)
