from __future__ import annotations

import logging
import shutil
from pathlib import Path

from autosfm_orchestrator.models import AutoSfmOutputs, AutoSfmRun
from autosfm_orchestrator.paths import create_workspace, local_scratch_root_for, make_execution_paths, sync_dir
from autosfm_orchestrator.sfm.metashape_pipeline import MetashapePipeline
from autosfm_orchestrator.utils import write_yaml

log = logging.getLogger(__name__)


class AutoSfmError(RuntimeError):
    pass


class AutoSfmRunner:
    """Thin AutoSfM execution boundary.

    Receives staged (NFS) paths from AutoSfmRun. Metashape runs against
    "execution paths" (see paths.make_execution_paths): images_dir and
    reference_dir stay on NFS, but autosfm_dir/project_dir/refs_dir/
    pixel_grid_dir are redirected to local scratch disk. This means full-
    resolution images are read once off NFS during the resize step and never
    copied there in full — only the resized photos, the .psx project, depth
    maps, and dense cloud (all of which are compute-heavy and slow/unreliable
    over NFS) live locally. Only the lightweight outputs (reference CSVs,
    pixel-world-grid NPZs, report, rasters if export is enabled) get copied
    back to the NFS-staged run.paths afterward, which is what transfer.py's
    CERES-promotion logic expects.

    This class does not query SQLite, parse manual logs, or discover LTS
    locations.
    """

    def __init__(self, config: dict):
        self.config = config
        self.autosfm_config = config.get("autosfm", {})
        self.workspace_config = config.get("workspace", {})

    def run(self, run: AutoSfmRun) -> AutoSfmOutputs:
        if self.autosfm_config.get("dry_run", True):
            return self._dry_run(run)
        return self._run_metashape(run)

    def _dry_run(self, run: AutoSfmRun) -> AutoSfmOutputs:
        log.info("AutoSfM dry run for %s", run.run_id)
        run.paths.refs_dir.mkdir(parents=True, exist_ok=True)
        run.paths.pixel_grid_dir.mkdir(parents=True, exist_ok=True)

        placeholder = {
            "run_id": run.run_id,
            "batch_id": run.batch_id,
            "image_count": len(run.image_records),
            "message": "AutoSfM dry-run placeholder. Replace with Metashape execution.",
        }
        write_yaml(run.paths.autosfm_dir / "DRY_RUN_AUTOSFM.yaml", placeholder)

        for filename in ["gcp_reference.csv", "camera_reference.csv", "error_statistics.csv", "fov.csv"]:
            (run.paths.refs_dir / filename).write_text("dry_run,true\n", encoding="utf-8")

        report_path = run.paths.outputs_dir / f"{run.run_id}_asfm_report.pdf"
        report_path.write_text("dry-run report placeholder\n", encoding="utf-8")

        return self._outputs(run, report_path=report_path, project_dir=run.paths.project_dir)

    def _run_metashape(self, run: AutoSfmRun) -> AutoSfmOutputs:
        exec_paths = make_execution_paths(self.config, run)
        create_workspace(exec_paths)

        # NOTE: no image copy here. exec_paths.images_dir is still the
        # NFS-staged run.paths.images_dir — full-res photos are read directly
        # off NFS by MetashapePipeline.resize_photos(), which writes only the
        # downscaled copies into exec_paths.autosfm_dir/downscaled_photos
        # (local). If autosfm.resize_photos is disabled in config, Metashape
        # will read full-res images directly from NFS during matching/align —
        # same NFS I/O problem this change is meant to avoid. Flag if you need
        # a full-res local-copy fallback for that case.

        pipeline = MetashapePipeline(
            run_id=run.run_id,
            batch_id=run.batch_id,
            paths=exec_paths,
            autosfm_config=self.autosfm_config,
        )
        try:
            pipeline.run()
        except Exception as e:
            raise AutoSfmError(f"Metashape execution failed for {run.run_id}: {e}") from e

        # Copy only the lightweight, downstream-needed outputs back to the
        # NFS-staged run — not the .psx project, resized photos, depth maps,
        # or dense cloud, all of which stay in local scratch.
        run.paths.refs_dir.mkdir(parents=True, exist_ok=True)
        sync_dir(exec_paths.refs_dir, run.paths.refs_dir)

        run.paths.pixel_grid_dir.mkdir(parents=True, exist_ok=True)
        sync_dir(exec_paths.pixel_grid_dir, run.paths.pixel_grid_dir)

        if pipeline.ortho_path.exists():
            dest = run.paths.autosfm_dir / "ortho" / pipeline.ortho_path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pipeline.ortho_path, dest)

        if pipeline.dem_path.exists():
            dest = run.paths.autosfm_dir / "dem" / pipeline.dem_path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pipeline.dem_path, dest)

        run.paths.outputs_dir.mkdir(parents=True, exist_ok=True)
        report_path = run.paths.outputs_dir / f"{run.run_id}_asfm_report.pdf"
        if pipeline.report_path.exists():
            shutil.copy2(pipeline.report_path, report_path)
        else:
            log.warning("Expected report at %s but it was not written.", pipeline.report_path)

        local_run_root = local_scratch_root_for(exec_paths)
        keep_local_scratch = self.workspace_config.get("keep_local_scratch", False)
        if not keep_local_scratch:
            log.info("Removing local scratch run directory %s", local_run_root)
            shutil.rmtree(local_run_root, ignore_errors=True)
        else:
            log.info("Keeping local scratch run directory %s (workspace.keep_local_scratch=true)", local_run_root)

        # project_dir reported here points at local scratch, which may already
        # be gone by the time anything reads AutoSfmOutputs if keep_local_scratch
        # is false. If you need the .psx preserved for later re-opening/
        # debugging, say so — it should be synced back to NFS explicitly
        # rather than just reported here.
        return self._outputs(run, report_path=report_path, project_dir=exec_paths.project_dir)

    def _outputs(self, run: AutoSfmRun, report_path: Path, project_dir: Path) -> AutoSfmOutputs:
        return AutoSfmOutputs(
            project_dir=project_dir,
            pixel_grid_dir=run.paths.pixel_grid_dir,
            fov_csv=run.paths.refs_dir / "fov.csv",
            camera_reference_csv=run.paths.refs_dir / "camera_reference.csv",
            gcp_reference_csv=run.paths.refs_dir / "gcp_reference.csv",
            error_statistics_csv=run.paths.refs_dir / "error_statistics.csv",
            report_pdf=report_path,
            manifest_path=run.paths.manifest_path,
            logs_dir=run.paths.logs_dir,
        )