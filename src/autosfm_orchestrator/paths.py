from __future__ import annotations

import logging
import shutil
from dataclasses import replace
from pathlib import Path

from autosfm_orchestrator.models import AutoSfmRun, ImageRecord, ManualLogEntry, ReferenceRecord, RunPaths
from autosfm_orchestrator.utils import sanitize_for_path

log = logging.getLogger(__name__)


def remove_staged_images(paths: RunPaths) -> None:
    """Delete the staged full-resolution input images from NFS staging
    (paths.images_dir) after a successful run + promotion, to avoid
    accumulating large duplicated imagery on NFS. Only removes images_dir --
    reference_dir, autosfm outputs, logs, and the manifest are left alone.
    """
    if not paths.images_dir.exists():
        log.info("remove_staged_images: %s does not exist, nothing to remove.", paths.images_dir)
        return
    log.info("Removing staged NFS input images: %s", paths.images_dir)
    shutil.rmtree(paths.images_dir, ignore_errors=True)

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
        pixel_grid_samples_dir=refs_dir / "pixel_grid_samples",
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
        paths.pixel_grid_samples_dir,
        paths.logs_dir,
        paths.outputs_dir,
    ]:
        path.mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------
# Local-scratch execution paths
#
# SUNNY is a single shared node with no scheduler and no job isolation
# beyond what we build ourselves. /mnt/... paths (sunny_staging_root, used
# by make_run_paths above) are NFS-mounted CERES/NCSU research storage.
# /home/mkutuga is local disk on SUNNY, not NFS. Metashape's heavy I/O
# (the .psx project, depth maps, dense cloud, resized photos) should run
# against local disk, not NFS — NFS is slow for this and Metashape's
# project locking is unreliable over it.
# ----------------------------------------------------------------------


def make_execution_paths(config: dict, run: AutoSfmRun) -> RunPaths:
    """RunPaths for Metashape execution.

    images_dir and reference_dir stay pointed at the NFS-staged sources —
    full-resolution images are never copied to local disk in full; they're
    read once (by resize_photo_directory) and only the downscaled copies
    land locally. reference_dir is a single small CSV, cheap enough to read
    directly off NFS too.

    autosfm_dir / project_dir / refs_dir / pixel_grid_dir / pixel_grid_samples_dir
    are redirected to local scratch (config["paths"]["local_scratch_root"]), since that's
    where Metashape does its heavy I/O: the .psx project file, depth maps,
    dense cloud, and the resized photos themselves.

    Everything else (run_root, lock_path, manifest_path, inputs_dir,
    logs_dir, outputs_dir) stays on NFS unchanged — those are workflow-level
    / final-output concerns (locking, the manifest, promoted outputs), not
    Metashape compute scratch.
    """
    local_run_root = local_run_root_for(config, run.batch_id, run.sub_batch_id)
    local_autosfm_dir = local_run_root / "autosfm"
    local_refs_dir = local_autosfm_dir / "reference"

    return replace(
        run.paths,
        autosfm_dir=local_autosfm_dir,
        project_dir=local_autosfm_dir / "project",
        refs_dir=local_refs_dir,
        pixel_grid_dir=local_refs_dir / "pixel_world_grids",
        pixel_grid_samples_dir=local_refs_dir / "pixel_grid_samples",
    )


def local_run_root_for(config: dict, batch_id: str, sub_batch_id: str) -> Path:
    """The local-scratch root directory for a given batch/sub-batch, e.g.
    config["paths"]["local_scratch_root"] / batch_id / sub_batch_id. Shared
    by make_execution_paths (Metashape compute scratch) and workflow.py
    (local copy of the run's log file), so both agree on where a run's
    local-disk files live.
    """
    scratch_root = Path(config["paths"]["local_scratch_root"]).expanduser()
    return scratch_root / batch_id / sub_batch_id


def local_log_path_for(config: dict, run: AutoSfmRun) -> Path:
    """Where the local-scratch copy of a run's log file lives, mirroring the
    batch_id/sub_batch_id structure used by make_execution_paths, e.g.
    <local_scratch_root>/<batch_id>/<sub_batch_id>/logs/<run_id>.log.
    """
    local_run_root = local_run_root_for(config, run.batch_id, run.sub_batch_id)
    return local_run_root / "logs" / f"{run.run_id.replace('/', '_')}.log"


def prune_dir_keep_only(root: Path, keep_paths: list[Path]) -> None:
    """Delete everything under root except keep_paths.

    Used to honor workspace.keep_local_report: when keep_local_scratch is
    false but keep_local_report is true, we still want to wipe local scratch
    (the .psx project, depth maps, dense cloud, resized photos, etc.) but
    leave the local report file and reference folder in place. Ancestor
    directories of a kept path are preserved (pruned of their other
    children) so the surviving paths remain valid; anything unrelated to a
    kept path is removed outright. keep_paths that don't exist are ignored.
    """
    if not root.exists():
        return
    resolved_keep = [p.resolve() for p in keep_paths if p.exists()]

    def _prune(current: Path) -> None:
        for entry in list(current.iterdir()):
            entry_r = entry.resolve()
            if entry_r in resolved_keep:
                continue
            if any(kp.is_relative_to(entry_r) for kp in resolved_keep):
                _prune(entry)
            else:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)

    _prune(root)


def local_scratch_root_for(exec_paths: RunPaths) -> Path:
    """The top-level local scratch directory for this run, derived from
    exec_paths.autosfm_dir (its parent). Used for cleanup after a run —
    RunPaths doesn't carry a separate 'local run_root' field since only the
    autosfm subtree is actually local.
    """
    return exec_paths.autosfm_dir.parent


def sync_dir(src: Path, dst: Path) -> None:
    """Copy the contents of src into dst. dst is created if missing; existing
    files at the destination are overwritten. No-ops (with a warning) if src
    is missing — callers decide whether that's an error.
    """
    dst.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        log.warning(f"sync_dir: source {src} does not exist, nothing to copy.")
        return

    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)