from __future__ import annotations

import logging
from pathlib import Path

from autosfm_orchestrator.models import AutoSfmOutputs, AutoSfmRun
from autosfm_orchestrator.utils import write_yaml

log = logging.getLogger(__name__)


class AutoSfmError(RuntimeError):
    pass


class AutoSfmRunner:
    """Thin AutoSfM execution boundary.

    This is where the existing Metashape/SfM code should be ported. The important
    design constraint is that this class receives staged paths from AutoSfmRun. It
    should not query SQLite, parse manual logs, or discover LTS locations.
    """

    def __init__(self, config: dict):
        self.config = config
        self.autosfm_config = config.get("autosfm", {})

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

        for filename in [
            "gcp_reference.csv",
            "camera_reference.csv",
            "error_statistics.csv",
            "fov.csv",
        ]:
            path = run.paths.refs_dir / filename
            path.write_text("dry_run,true\n", encoding="utf-8")

        report_path = run.paths.outputs_dir / f"{run.run_id}_asfm_report.pdf"
        report_path.write_text("dry-run report placeholder\n", encoding="utf-8")

        return self._outputs(run, report_path=report_path)

    def _run_metashape(self, run: AutoSfmRun) -> AutoSfmOutputs:
        # TODO: port current SemiF-Preprocessing SfM class here.
        # Expected staged inputs:
        #   run.paths.images_dir
        #   run.paths.reference_dir
        # Expected outputs:
        #   run.paths.autosfm_dir
        #   run.paths.refs_dir
        #   run.paths.pixel_grid_dir
        raise AutoSfmError("Metashape execution is not implemented yet. Use autosfm.dry_run=true.")

    def _outputs(self, run: AutoSfmRun, report_path: Path) -> AutoSfmOutputs:
        return AutoSfmOutputs(
            project_dir=run.paths.project_dir,
            pixel_grid_dir=run.paths.pixel_grid_dir,
            fov_csv=run.paths.refs_dir / "fov.csv",
            camera_reference_csv=run.paths.refs_dir / "camera_reference.csv",
            gcp_reference_csv=run.paths.refs_dir / "gcp_reference.csv",
            error_statistics_csv=run.paths.refs_dir / "error_statistics.csv",
            report_pdf=report_path,
            manifest_path=run.paths.manifest_path,
            logs_dir=run.paths.logs_dir,
        )
