"""Metashape SfM pipeline for a single staged AutoSfM run.

Ported from src/tasks/auto_sfm/metashape_utils.py (SfM class), checked
against the actual source. This version knows nothing about SQLite, Globus,
or manual logs — it only receives:

  - a RunPaths (paths.images_dir, paths.reference_dir, paths.autosfm_dir,
    paths.project_dir, paths.refs_dir, paths.pixel_grid_dir)
  - the `autosfm` config block from conf/default.yaml
  - run_id / batch_id as plain strings

Deliberately dropped from the original:
  - add_masks() / mask-aware match_photos(filter_mask=...) — no masks are
    staged by this pipeline (see staging.py / resize.py).
  - get_files(cfg, task=...) discovery — replaced with a plain glob over
    paths.images_dir / paths.reference_dir, since file selection is now
    staging.py's job, not this class's.
  - bbot_version-based marker-bit inference — replaced with an explicit
    autosfm.marker_bit config value.
  - cfg.crs (top-level Hydra key) — replaced with autosfm.crs.

Requires the `conf/default.yaml` `autosfm:` block to be extended with nested
quality-setting sub-blocks (align_photos, optimize_cameras_cfg, depth_map,
dense_cloud, model, dem, orthomosaic, pixel_grid). See accompanying proposal
for the recommended yaml.
"""

from __future__ import annotations

import logging
import os
from copy import deepcopy
from pathlib import Path
from typing import Callable, Tuple

import numpy as np
import yaml
from tqdm import tqdm

import Metashape as ms

from autosfm_orchestrator.models import RunPaths
from autosfm_orchestrator.sfm.callbacks import percentage_callback
from autosfm_orchestrator.sfm.dataframe import DataFrame
from autosfm_orchestrator.sfm.estimation import CameraStats, MarkerStats
from autosfm_orchestrator.sfm.resize import resize_photo_directory

log = logging.getLogger(__name__)


class MetashapePipelineError(RuntimeError):
    pass


_FILTER_MODES = {
    "aggressive": lambda: ms.AggressiveFiltering,
    "moderate": lambda: ms.ModerateFiltering,
    "mild": lambda: ms.MildFiltering,
    "none": lambda: ms.NoFiltering,
}


class MetashapePipeline:
    def __init__(self, run_id: str, batch_id: str, paths: RunPaths, autosfm_config: dict):
        self.run_id = run_id
        self.batch_id = batch_id
        self.cfg = autosfm_config

        # --- Explicit paths derived from RunPaths only ---
        self.images_dir = paths.images_dir
        self.reference_dir = paths.reference_dir
        self.autosfm_dir = paths.autosfm_dir
        self.refs_dir = paths.refs_dir
        self.grid_dir = paths.pixel_grid_dir

        self.project_path = paths.project_dir / f"{run_id}.psx"
        self.downscaled_dir = self.autosfm_dir / "downscaled_photos"
        self.ortho_path = self.autosfm_dir / "ortho" / "orthomosaic.tif"
        self.dem_path = self.autosfm_dir / "dem" / "dem.tif"
        self.report_path = self.autosfm_dir / f"{run_id}_asfm_report.pdf"

        self.gcp_ref = self.refs_dir / "gcp_reference.csv"
        self.cam_ref = self.refs_dir / "camera_reference.csv"
        self.err_ref = self.refs_dir / "error_statistics.csv"
        self.fov_ref = self.refs_dir / "fov.csv"

        for d in (self.autosfm_dir, self.refs_dir, self.grid_dir,
                  self.project_path.parent, self.ortho_path.parent, self.dem_path.parent):
            d.mkdir(parents=True, exist_ok=True)

        # --- Config-driven settings (see conf/default.yaml `autosfm:` block) ---
        self.use_masking = self.cfg.get("use_masking", False)
        self.skip_first_n_images = self.cfg.get("skip_first_n_images")
        self.skip_last_n_images = self.cfg.get("skip_last_n_images")

        self.align_photos_cfg = _AttrDict(self.cfg.get("align_photos", {}))
        self.opt_cam_cfg = _AttrDict(self.cfg.get("optimize_cameras_cfg", {}))
        self.depth_map_cfg = _AttrDict(self.cfg.get("depth_map", {}))
        self.dense_cloud_cfg = _AttrDict(self.cfg.get("dense_cloud", {}))
        self.model_cfg = _AttrDict(self.cfg.get("model", {}))
        self.dem_cfg = _AttrDict(self.cfg.get("dem", {"export": {}}))
        self.ortho_cfg = _AttrDict(self.cfg.get("orthomosaic", {"export": {}}))
        self.pixel_grid_step = self.cfg.get("pixel_grid", {}).get("step", 100)

        # Recovery-mode flags, ported from the old top-level asfm config.
        self.recover_unaligned_only = self.cfg.get("recover_unaligned_only", False)
        self.recover_rematch = self.cfg.get("recover_rematch", False)
        self.align_correct = self.cfg.get("align_correct", False)

        num_gpus = self.cfg.get("num_gpus", 1)
        self.num_gpus = (
            num_gpus if num_gpus != "all" else 2 ** len(ms.app.enumGPUDevices()) - 1
        )

        self.metashape_key = self._load_license()
        self.crs, self.markerbit = self._resolve_crs_and_marker_bit()

        self.doc = self.load_or_create_project()

        # populated once export methods run; used by export_stats()
        self.camera_reference: DataFrame | None = None
        self.gcp_reference: DataFrame | None = None
        self.error_statistics: DataFrame | None = None

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _load_license(self) -> str | None:
        """Reads the Metashape license key from autosfm.metashape_key_path (yaml
        with a top-level `metashape.lic` key), falling back to the
        METASHAPE_LICENSE environment variable. The old code always read from
        cfg.paths.pipeline_keys with no fallback; that path doesn't exist in
        this orchestrator's config, so a fallback is necessary.
        """
        key_path = self.cfg.get("metashape_key_path")
        if key_path:
            with open(key_path, "r") as f:
                data = yaml.safe_load(f) or {}
            key = data.get("metashape", {}).get("lic")
            if key:
                return key
        env_key = os.environ.get("METASHAPE_LICENSE")
        if env_key:
            return env_key
        log.warning("No Metashape license key found (metashape_key_path or METASHAPE_LICENSE unset).")
        return None

    def _resolve_crs_and_marker_bit(self) -> Tuple[str, "ms.TargetType"]:
        """CRS and marker bit are no longer inferred from bbot_version — they're
        explicit config. Add `autosfm.crs` and `autosfm.marker_bit` ("12bit" or
        "14bit") to conf/default.yaml.
        """
        crs_code = self.cfg.get("crs")
        if not crs_code:
            raise MetashapePipelineError(
                "autosfm.crs is not set in config. Old code inferred this per-batch; "
                "the orchestrator needs it set explicitly."
            )
        crs = f"EPSG::{crs_code}"

        marker_bit_setting = self.cfg.get("marker_bit", "14bit")
        marker_bit = ms.CircularTarget14bit if marker_bit_setting == "14bit" else ms.CircularTarget12bit
        log.info(f"Using CRS {crs} and marker bit {marker_bit_setting} for {self.batch_id}")
        return crs, marker_bit

    def _find_reference_file(self) -> Path:
        """reference_dir is staged by staging.py to contain exactly one GCP CSV
        for this run (filename varies, e.g. GroundControlPoints_NC_32617_...).
        """
        candidates = sorted(self.reference_dir.glob("*.csv"))
        if len(candidates) == 0:
            raise MetashapePipelineError(f"No reference CSV found in {self.reference_dir}")
        if len(candidates) > 1:
            raise MetashapePipelineError(
                f"Expected exactly one reference CSV in {self.reference_dir}, found {len(candidates)}: "
                f"{[c.name for c in candidates]}"
            )
        return candidates[0]

    def load_or_create_project(self) -> "ms.Document":
        assert self.project_path.suffix == ".psx"
        if not ms.app.activated and self.metashape_key:
            ms.License().activate(self.metashape_key)
        doc = ms.Document()
        if self.project_path.exists():
            log.info("Metashape project already exists. Opening.")
            doc.open(str(self.project_path), read_only=False, ignore_lock=True)
        else:
            log.info(f"Creating new Metashape project for {self.batch_id}")
            doc.addChunk()
            doc.save(str(self.project_path))
        return doc

    def save_project(self) -> None:
        assert self.project_path.suffix == ".psx"
        self.doc.save()

    # ------------------------------------------------------------------
    # Pipeline steps
    # ------------------------------------------------------------------

    def resize_photos(self) -> Path:
        factor = self.cfg.get("downscale", {}).get("factor", 0.75)
        overwrite = self.cfg.get("downscale", {}).get("overwrite", False)
        return resize_photo_directory(self.images_dir, self.downscaled_dir, factor, overwrite=overwrite)

    def add_photos(self, source_dir: Path | None = None) -> None:
        """Matches original behavior: sets chunk.crs before addPhotos()."""
        photo_dir = source_dir or self.images_dir
        photos = sorted(str(p) for p in photo_dir.glob("*.jpg")) + sorted(str(p) for p in photo_dir.glob("*.JPG"))

        if isinstance(self.skip_first_n_images, int):
            photos = photos[self.skip_first_n_images:]
        if isinstance(self.skip_last_n_images, int) and self.skip_last_n_images > 0:
            photos = photos[: len(photos) - self.skip_last_n_images]

        log.info(f"Adding {len(photos)} photos to the project")
        if self.doc.chunk is None:
            self.doc.addChunk()
        self.doc.chunk.crs = ms.CoordinateSystem(self.crs)
        self.doc.chunk.addPhotos(photos)
        self.save_project()

    def remove_low_id_markers(self, chunk: int = 0, threshold: int = 300) -> None:
        """Remove markers with numeric labels below a threshold (e.g. < 300) for
        NC batches. Not called automatically by run() — invoke explicitly if
        needed for a given batch, same as in the original pipeline.
        """
        if not self.batch_id.startswith("NC"):
            log.info("Skipping marker filtering (not an NC batch)")
            return

        chunk_obj = self.doc.chunks[chunk]
        markers_to_remove = []
        for marker in chunk_obj.markers:
            try:
                marker_id = int(marker.label)
            except (ValueError, TypeError):
                continue
            if marker_id < threshold:
                markers_to_remove.append(marker)

        log.info(f"Removing {len(markers_to_remove)} markers with ID < {threshold}")
        if markers_to_remove:
            chunk_obj.remove(markers_to_remove)
        self.save_project()

    def detect_markers(self, chunk: int = 0, progress_callback: Callable = percentage_callback) -> None:
        self.doc.chunks[chunk].detectMarkers(
            target_type=self.markerbit,
            tolerance=50,
            filter_mask=False,
            inverted=False,
            noparity=False,
            maximum_residual=5,
            minimum_size=0,
            minimum_dist=5,
            cameras=self.doc.chunks[chunk].cameras,
            progress=progress_callback,
        )
        self.save_project()

    def import_reference(self, chunk: int = 0) -> None:
        reference_file = self._find_reference_file()
        log.info(f"Importing reference file: {reference_file.name}")
        self.doc.chunks[chunk].importReference(
            path=str(reference_file),
            format=ms.ReferenceFormatCSV,
            columns="[n|x|y|z]",
            delimiter=";",
            group_delimiters=False,
            skip_rows=1,
            ignore_labels=False,
            create_markers=True,
            threshold=0.1,
            shutter_lag=0,
            crs=ms.CoordinateSystem(self.crs),
        )
        self.save_project()

    def match_photos(
        self,
        progress_callback: Callable = percentage_callback,
        chunk: int = 0,
        reference_preselection=ms.ReferencePreselectionSource,
        reset_matches: bool = True,
        cameras=None,
    ) -> None:
        log.info(f"Matching photos in chunk {chunk}")
        ms.app.cpu_enable = False
        ms.app.gpu_mask = self.num_gpus

        chunk_obj = self.doc.chunks[chunk]
        if cameras is None:
            cameras = chunk_obj.cameras

        chunk_obj.matchPhotos(
            downscale=self.align_photos_cfg.downscale,
            generic_preselection=self.align_photos_cfg.generic_preselection,
            reference_preselection=self.align_photos_cfg.reference_preselection,
            reference_preselection_mode=reference_preselection,
            filter_mask=self.use_masking,
            mask_tiepoints=True,
            filter_stationary_points=self.align_photos_cfg.filter_stationary_points,
            keypoint_limit=600000,
            keypoint_limit_per_mpx=10000,
            tiepoint_limit=200000,
            keep_keypoints=False,
            cameras=cameras,
            guided_matching=False,
            reset_matches=reset_matches,
            subdivide_task=True,
            workitem_size_cameras=20,
            workitem_size_pairs=80,
            max_workgroup_size=100,
            progress=progress_callback,
        )
        ms.app.cpu_enable = True if ms.app.gpu_mask else False
        self.save_project()

    def get_unaligned_cameras(self, chunk: int = -1):
        return [c for c in self.doc.chunks[chunk].cameras if c.transform is None]

    def _remove_duplicate_cameras(self, chunk: int) -> None:
        chunk_obj = self.doc.chunks[chunk]
        seen_aligned = set()
        cameras_to_remove = []
        for camera in chunk_obj.cameras:
            if camera.transform is None:
                continue
            if camera.label in seen_aligned:
                cameras_to_remove.append(camera)
            else:
                seen_aligned.add(camera.label)
        if cameras_to_remove:
            chunk_obj.remove(cameras_to_remove)
        log.info(
            f"Chunk {chunk} ({chunk_obj.label}) after duplicate cleanup: "
            f"total={len(chunk_obj.cameras)}, unaligned={len(self.get_unaligned_cameras(chunk))}"
        )

    def remove_unaligned_cameras(self, chunk: int = 0) -> None:
        chunk_obj = self.doc.chunks[chunk]
        unaligned = self.get_unaligned_cameras(chunk)
        if unaligned:
            chunk_obj.remove(unaligned)
        log.info(f"Removed {len(unaligned)} unaligned cameras. Remaining: {len(chunk_obj.cameras)}")

    def reset_region(self) -> bool:
        """Reset the region and enlarge it well beyond the points; otherwise
        points outside the region get clipped when saving."""
        self.doc.chunk.resetRegion()
        region_dims = self.doc.chunk.region.size
        region_dims[2] *= 3
        self.doc.chunk.region.size = region_dims
        return True

    def align_photos(
        self,
        progress_callback: Callable = percentage_callback,
        chunk: int = 0,
        correct: bool = False,
    ) -> None:
        """Full alignment pass. If correct=True, iteratively recover only the
        currently unaligned cameras in place (rematch without resetting existing
        matches, realign without resetting the existing solution), stopping once
        unaligned count is <= 2 or stops improving.
        """
        log.debug(f"[{self.batch_id}] Aligning photos in chunk {chunk}")
        chunk_obj = self.doc.chunks[chunk]
        self.doc.chunk = chunk_obj

        ms.app.cpu_enable = False
        ms.app.gpu_mask = self.num_gpus

        chunk_obj.alignCameras(
            cameras=chunk_obj.cameras,
            min_image=2,
            adaptive_fitting=self.align_photos_cfg.adaptive_fitting,
            reset_alignment=True,
            subdivide_task=True,
            progress=progress_callback,
        )
        ms.app.cpu_enable = True if ms.app.gpu_mask else False
        self.save_project()

        if correct:
            prev_unaligned_count = float("inf")
            iteration = 0
            while True:
                cur_unaligned_count = len(self.get_unaligned_cameras(chunk))
                log.info(f"Recovery iteration {iteration}: chunk={chunk}, unaligned={cur_unaligned_count}")

                if cur_unaligned_count <= 2:
                    log.info("Stopping recovery because unaligned camera count is <= 2.")
                    break
                if cur_unaligned_count >= prev_unaligned_count:
                    log.warning(
                        "Stopping recovery because no further improvement was made. "
                        f"previous={prev_unaligned_count}, current={cur_unaligned_count}"
                    )
                    break

                prev_unaligned_count = cur_unaligned_count
                self._recover_unaligned_cameras_in_place(chunk=chunk, progress_callback=progress_callback, rematch=True)
                iteration += 1

        self._remove_duplicate_cameras(chunk)
        self.remove_unaligned_cameras(chunk)
        self.doc.chunk = self.doc.chunks[chunk]
        self.reset_region()
        self.save_project()

    def recover_unaligned_only(
        self,
        chunk: int = 0,
        rematch: bool = True,
        progress_callback: Callable = percentage_callback,
    ) -> None:
        """Recovery-only mode for an already-aligned project: no full realignment,
        just tries to recover currently unaligned cameras in place."""
        chunk_obj = self.doc.chunks[chunk]
        self.doc.chunk = chunk_obj

        before = len(self.get_unaligned_cameras(chunk))
        log.info(f"Starting recovery-only mode on chunk {chunk} ({chunk_obj.label}). Initial unaligned: {before}")

        prev_unaligned_count = float("inf")
        iteration = 0
        while True:
            cur_unaligned_count = len(self.get_unaligned_cameras(chunk))
            log.info(f"Recovery iteration {iteration}: chunk={chunk}, unaligned={cur_unaligned_count}")

            if cur_unaligned_count == 0:
                log.info("Stopping recovery because all cameras are aligned.")
                break
            if cur_unaligned_count <= 2:
                log.info("Stopping recovery because unaligned camera count is <= 2.")
                break
            if cur_unaligned_count >= prev_unaligned_count:
                log.warning(
                    "Stopping recovery because no further improvement was made. "
                    f"previous={prev_unaligned_count}, current={cur_unaligned_count}"
                )
                break

            prev_unaligned_count = cur_unaligned_count
            self._recover_unaligned_cameras_in_place(chunk=chunk, progress_callback=progress_callback, rematch=rematch)
            iteration += 1

        after = len(self.get_unaligned_cameras(chunk))
        log.info(f"Recovery-only mode finished on chunk {chunk} ({chunk_obj.label}). Final unaligned: {after}")

        self.doc.chunk = self.doc.chunks[chunk]
        self.reset_region()
        self.save_project()

    def _recover_unaligned_cameras_in_place(
        self,
        chunk: int,
        progress_callback: Callable = percentage_callback,
        rematch: bool = True,
    ) -> int:
        chunk_obj = self.doc.chunks[chunk]
        self.doc.chunk = chunk_obj

        unaligned = self.get_unaligned_cameras(chunk)
        if not unaligned:
            log.info(f"No unaligned cameras found in chunk {chunk} ({chunk_obj.label}).")
            return 0

        log.info(f"Attempting in-place recovery for {len(unaligned)} unaligned cameras in chunk {chunk} ({chunk_obj.label})")
        for cam in unaligned:
            log.info(f"Unaligned camera: {cam.label}")

        if rematch:
            log.info("Running matchPhotos on full chunk with reset_matches=False")
            self.match_photos(chunk=chunk, progress_callback=progress_callback, reset_matches=False, cameras=chunk_obj.cameras)

        ms.app.cpu_enable = False
        ms.app.gpu_mask = self.num_gpus

        log.info("Running alignCameras on unaligned cameras with reset_alignment=False")
        chunk_obj.alignCameras(
            cameras=unaligned,
            min_image=2,
            adaptive_fitting=self.align_photos_cfg.adaptive_fitting,
            reset_alignment=False,
            subdivide_task=True,
            progress=progress_callback,
        )
        ms.app.cpu_enable = True if ms.app.gpu_mask else False
        self.save_project()

        remaining = len(self.get_unaligned_cameras(chunk))
        log.info(f"After in-place recovery attempt, chunk {chunk} ({chunk_obj.label}) has {remaining} unaligned cameras remaining.")
        return remaining

    def optimize_cameras(self, progress_callback: Callable = percentage_callback) -> None:
        for camera in self.doc.chunk.cameras:
            camera.reference.enabled = False

        self.doc.chunk.optimizeCameras(
            fit_f=self.opt_cam_cfg.fit_f,
            fit_cx=self.opt_cam_cfg.fit_cx,
            fit_cy=self.opt_cam_cfg.fit_cy,
            fit_b1=self.opt_cam_cfg.fit_b1,
            fit_b2=self.opt_cam_cfg.fit_b2,
            fit_k1=self.opt_cam_cfg.fit_k1,
            fit_k2=self.opt_cam_cfg.fit_k2,
            fit_k3=self.opt_cam_cfg.fit_k3,
            fit_k4=self.opt_cam_cfg.fit_k4,
            fit_p1=self.opt_cam_cfg.fit_p1,
            fit_p2=self.opt_cam_cfg.fit_p2,
            fit_corrections=self.opt_cam_cfg.fit_corrections,
            adaptive_fitting=self.opt_cam_cfg.adaptive_fitting,
            tiepoint_covariance=self.opt_cam_cfg.tiepoint_covariance,
            progress=progress_callback,
        )
        self.save_project()

    def build_depth_map(self, progress_callback: Callable = percentage_callback) -> None:
        ms.app.cpu_enable = False
        ms.app.gpu_mask = self.num_gpus
        log.info(f"Number of cameras in chunk at depth map: {len(self.doc.chunk.cameras)}")
        log.debug(f"Chunks names: {[c.label for c in self.doc.chunks]}")

        mode_key = str(self.depth_map_cfg.filtering_mode).lower()
        if mode_key not in _FILTER_MODES:
            raise ValueError(f"Unknown filtering mode: {self.depth_map_cfg.filtering_mode}")
        filter_mode = _FILTER_MODES[mode_key]()

        self.doc.chunk.buildDepthMaps(
            downscale=self.depth_map_cfg.downscale,
            filter_mode=filter_mode,
            cameras=self.doc.chunk.cameras,
            reuse_depth=True,
            max_neighbors=self.depth_map_cfg.max_neighbors,
            subdivide_task=True,
            workitem_size_cameras=20,
            max_workgroup_size=100,
            progress=progress_callback,
        )
        if ms.app.gpu_mask:
            ms.app.cpu_enable = True
        if self.depth_map_cfg.autosave:
            self.save_project()

    def build_dense_cloud(self, progress_callback: Callable = percentage_callback) -> None:
        ms.app.cpu_enable = False
        ms.app.gpu_mask = self.num_gpus

        if self.doc.chunk.depth_maps is None:
            self.build_depth_map()

        self.doc.chunk.buildPointCloud(
            point_colors=True,
            points_spacing=self.dense_cloud_cfg.points_spacing,
            point_confidence=False,
            keep_depth=True,
            max_neighbors=100,
            uniform_sampling=True,
            subdivide_task=True,
            workitem_size_cameras=20,
            max_workgroup_size=100,
            progress=progress_callback,
        )
        if ms.app.gpu_mask:
            ms.app.cpu_enable = True
        if self.dense_cloud_cfg.autosave:
            self.save_project()

    def build_model(self, progress_callback: Callable = percentage_callback) -> None:
        self.doc.chunk.buildModel(
            surface_type=ms.HeightField,
            interpolation=ms.Extrapolated,
            face_count=ms.LowFaceCount,
            source_data=ms.PointCloudData,
            vertex_colors=True,
            vertex_confidence=False,
            volumetric_masks=False,
            keep_depth=True,
            trimming_radius=0,
            subdivide_task=True,
            workitem_size_cameras=20,
            max_workgroup_size=100,
            progress=progress_callback,
        )
        self.doc.chunk.model.closeHoles(level=100)
        self.save_project()

    def build_dem(self, progress_callback: Callable = percentage_callback) -> None:
        if self.doc.chunk.point_cloud is None:
            log.warning("Building dense cloud because it does not exist.")
            self.build_dense_cloud()

        self.doc.chunk.buildDem(
            source_data=ms.PointCloudData,
            interpolation=ms.EnabledInterpolation,
            flip_x=False,
            flip_y=False,
            flip_z=False,
            resolution=0,
            subdivide_task=True,
            workitem_size_tiles=10,
            max_workgroup_size=100,
            progress=progress_callback,
        )
        if self.dem_cfg.autosave:
            self.save_project()

        if self.dem_cfg.export.get("enabled"):
            image_compression = ms.ImageCompression()
            image_compression.tiff_big = True
            self.dem_path.parent.mkdir(parents=True, exist_ok=True)
            self.doc.chunk.exportRaster(
                path=str(self.dem_path),
                image_format=ms.ImageFormatTIFF,
                source_data=ms.ElevationData,
                progress=progress_callback,
                image_compression=image_compression,
            )

    def build_ortomosaic(self, progress_callback: Callable = percentage_callback) -> None:
        self.doc.chunk.buildOrthomosaic(
            surface_data=ms.ElevationData,
            blending_mode=ms.MosaicBlending,
            fill_holes=True,
            ghosting_filter=False,
            cull_faces=False,
            refine_seamlines=False,
            resolution=0,
            resolution_x=0,
            resolution_y=0,
            flip_x=False,
            flip_y=False,
            flip_z=False,
            subdivide_task=True,
            workitem_size_cameras=20,
            workitem_size_tiles=10,
            max_workgroup_size=100,
            progress=progress_callback,
        )
        if self.ortho_cfg.autosave:
            self.save_project()

        if self.ortho_cfg.export.get("enabled"):
            log.info(f"Exporting orthomosaic to {self.ortho_path}")
            image_compression = ms.ImageCompression()
            image_compression.tiff_big = True
            self.ortho_path.parent.mkdir(parents=True, exist_ok=True)
            self.doc.chunk.exportRaster(
                path=str(self.ortho_path),
                image_format=ms.ImageFormatTIFF,
                source_data=ms.OrthomosaicData,
                progress=progress_callback,
                image_compression=image_compression,
            )

    # ------------------------------------------------------------------
    # Exports
    # ------------------------------------------------------------------

    def _camera_parameters(self, camera) -> dict:
        row = {}
        row["f"] = camera.calibration.f
        row["cx"] = camera.calibration.cx
        row["cy"] = camera.calibration.cy
        row["k1"] = camera.calibration.k1
        row["k2"] = camera.calibration.k2
        row["k3"] = camera.calibration.k3
        row["k4"] = camera.calibration.k4
        row["p1"] = camera.calibration.p1
        row["p2"] = camera.calibration.p2
        row["b1"] = camera.calibration.b1
        row["b2"] = camera.calibration.b2
        row["pixel_height"] = camera.sensor.pixel_height
        row["pixel_width"] = camera.sensor.pixel_width
        return row

    def export_camera_reference(self) -> None:
        reference = []
        for camera in self.doc.chunk.cameras:
            stats = CameraStats(camera).to_dict()
            stats.update(self._camera_parameters(camera))
            stats.update({"Alignment": camera.transform is not None})
            reference.append(stats)
        dataframe = DataFrame(reference, "label")
        self.camera_reference = dataframe
        dataframe.to_csv(self.cam_ref, header=True, index=False)

    def export_gcp_reference(self) -> None:
        reference = []
        for marker in self.doc.chunk.markers:
            stats = MarkerStats(marker).to_dict()
            stats.update({"Detected": len(marker.projections.items()) > 0})
            reference.append(stats)
        dataframe = DataFrame(reference, "label")
        self.gcp_reference = dataframe
        dataframe.to_csv(self.gcp_ref, header=True, index=False)

    def export_stats(self) -> None:
        if self.camera_reference is None:
            self.export_camera_reference()
        if self.gcp_reference is None:
            self.export_gcp_reference()

        total_cameras = len(self.doc.chunk.cameras)
        aligned_cameras = sum(row["Alignment"] for row in self.camera_reference.content_dict)
        percentage_aligned = aligned_cameras / total_cameras if total_cameras else 0.0

        total_gcps = len(self.gcp_reference)
        detected_gcps = sum(row["Detected"] for row in self.gcp_reference.content_dict)
        percentage_detected = detected_gcps / max(1, total_gcps)

        dataframe = DataFrame(
            [{
                "Total_Cameras": total_cameras,
                "Aligned_Cameras": aligned_cameras,
                "Percentage_Aligned_Cameras": percentage_aligned,
                "Total_GCPs": total_gcps,
                "Detected_GCPs": detected_gcps,
                "Percentage_Detected_GCPs": percentage_detected,
            }],
            "Total_Cameras",
        )
        self.error_statistics = dataframe
        dataframe.to_csv(self.err_ref, header=True, index=False)

    def camera_fov(self) -> None:
        if not self.doc.chunk.shapes:
            self.doc.chunk.shapes = ms.Shapes()
            self.doc.chunk.shapes.crs = self.doc.chunk.crs

        surface = self.doc.chunk.model
        transform = self.doc.chunk.transform.matrix
        crs = self.doc.chunk.crs

        row_template = {
            "label": "", "top_left_x": "", "top_left_y": "",
            "bottom_left_x": "", "bottom_left_y": "",
            "bottom_right_x": "", "bottom_right_y": "",
            "top_right_x": "", "top_right_y": "",
            "height": "", "width": "",
        }
        rows = []

        for camera in tqdm(self.doc.chunk.cameras, desc="Calculating FOV", unit="camera"):
            if camera.type != ms.Camera.Type.Regular or not camera.transform:
                continue

            row = deepcopy(row_template)
            row["label"] = camera.label

            corners_px = [
                [0, 0],
                [camera.sensor.width - 1, 0],
                [camera.sensor.width - 1, camera.sensor.height - 1],
                [0, camera.sensor.height - 1],
            ]
            world_coords = []
            for (x, y) in corners_px:
                ray_origin = camera.unproject(ms.Vector([x, y, 0]))
                ray_target = camera.unproject(ms.Vector([x, y, 1]))
                point = surface.pickPoint(ray_origin, ray_target)
                if point is None:
                    point = self.doc.chunk.tie_points.pickPoint(ray_origin, ray_target)
                if point is None:
                    log.warning(f"Failed to get FOV corner for camera: {camera.label}")
                    break
                projected = crs.project(transform.mulp(point))
                world_coords.append((projected.x, projected.y))

            if len(world_coords) != 4:
                continue

            (top_left, top_right, bottom_right, bottom_left) = world_coords
            row["top_left_x"], row["top_left_y"] = top_left
            row["top_right_x"], row["top_right_y"] = top_right
            row["bottom_right_x"], row["bottom_right_y"] = bottom_right
            row["bottom_left_x"], row["bottom_left_y"] = bottom_left

            def distance(p1, p2):
                return ((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2) ** 0.5

            row["width"] = (distance(top_left, top_right) + distance(bottom_left, bottom_right)) / 2.0
            row["height"] = (distance(top_left, bottom_left) + distance(top_right, bottom_right)) / 2.0
            rows.append(row)

        df = DataFrame(rows, "label")
        df.to_csv(self.fov_ref, index=False, header=True)

    def export_pixel_world_grid(self, step: int | None = None) -> None:
        step = step or self.pixel_grid_step
        surface = self.doc.chunk.model
        if surface is None:
            log.warning("No model found. Cannot export pixel-world grids.")
            return

        transform = self.doc.chunk.transform.matrix
        crs = self.doc.chunk.crs
        self.grid_dir.mkdir(parents=True, exist_ok=True)

        cameras = [c for c in self.doc.chunk.cameras if c.type == ms.Camera.Type.Regular and c.transform is not None]
        log.info(f"Exporting pixel-world grids for {len(cameras)} cameras (step={step}px)")

        for camera in tqdm(cameras, desc="Exporting pixel-world grids", unit="camera"):
            w, h = camera.sensor.width, camera.sensor.height
            u_vals = np.unique(np.concatenate([np.arange(0, w, step), [w - 1]])).astype(np.float32)
            v_vals = np.unique(np.concatenate([np.arange(0, h, step), [h - 1]])).astype(np.float32)

            world_x = np.full((len(v_vals), len(u_vals)), np.nan, dtype=np.float64)
            world_y = np.full((len(v_vals), len(u_vals)), np.nan, dtype=np.float64)
            world_z = np.full((len(v_vals), len(u_vals)), np.nan, dtype=np.float64)

            nan_count = 0
            for vi, v in enumerate(v_vals):
                for ui, u in enumerate(u_vals):
                    ray_origin = camera.unproject(ms.Vector([float(u), float(v), 0]))
                    ray_target = camera.unproject(ms.Vector([float(u), float(v), 1]))
                    point = surface.pickPoint(ray_origin, ray_target)
                    if point is None and self.doc.chunk.tie_points is not None:
                        point = self.doc.chunk.tie_points.pickPoint(ray_origin, ray_target)
                    if point is not None:
                        projected = crs.project(transform.mulp(point))
                        world_x[vi, ui] = projected.x
                        world_y[vi, ui] = projected.y
                        world_z[vi, ui] = projected.z
                    else:
                        nan_count += 1

            if nan_count > 0:
                log.warning(f"{camera.label}: {nan_count}/{len(u_vals) * len(v_vals)} grid points had no surface intersection")

            np.savez_compressed(
                self.grid_dir / f"{camera.label}.npz",
                u_pixels=u_vals, v_pixels=v_vals,
                world_x=world_x, world_y=world_y, world_z=world_z,
                sensor_width=np.array([w], dtype=np.int32),
                sensor_height=np.array([h], dtype=np.int32),
                crs=np.array([str(crs)], dtype=object),
            )

        log.info(f"Pixel-world grid export complete: {len(cameras)} files -> {self.grid_dir}")

    def export_report(self, progress_callback: Callable = percentage_callback) -> None:
        self.doc.chunk.exportReport(
            path=str(self.report_path),
            title=self.batch_id,
            description="report",
            font_size=12,
            page_numbers=True,
            include_system_info=True,
            progress=progress_callback,
        )

    # ------------------------------------------------------------------
    # Orchestration — mirrors the old autosfm.py main() flag-driven flow
    # ------------------------------------------------------------------

    def run(self) -> None:
        cfg = self.cfg

        if cfg.get("add_photos", True):
            if cfg.get("resize_photos"):
                self.resize_photos()
                self.add_photos(self.downscaled_dir)
            else:
                self.add_photos()

        if cfg.get("detect_markers"):
            self.detect_markers()

        if cfg.get("import_references"):
            self.import_reference()

        if self.recover_unaligned_only:
            self.recover_unaligned_only(rematch=self.recover_rematch)
        else:
            if cfg.get("match"):
                self.match_photos()
            if cfg.get("align"):
                self.align_photos(correct=self.align_correct)

        if cfg.get("optimize_cameras"):
            self.optimize_cameras()

        self.export_gcp_reference()
        self.export_camera_reference()
        self.export_stats()

        if cfg.get("build_depth"):
            self.build_depth_map()

        if cfg.get("build_dense"):
            self.build_dense_cloud()

        if cfg.get("build_model"):
            self.build_model()

        if cfg.get("build_dem"):
            self.build_dem()

        if cfg.get("build_ortho"):
            self.build_ortomosaic()

        if cfg.get("export_fov"):
            self.camera_fov()

        if cfg.get("export_pixel_grid"):
            self.export_pixel_world_grid()

        if cfg.get("export_report"):
            self.export_report()


class _AttrDict(dict):
    """Lets nested config dicts be accessed as attrs (cfg.align_photos.downscale)
    the way the old OmegaConf DictConfig objects were, without adding an
    OmegaConf/Hydra dependency here."""

    def __getattr__(self, item):
        try:
            value = self[item]
        except KeyError as e:
            raise AttributeError(item) from e
        return _AttrDict(value) if isinstance(value, dict) else value