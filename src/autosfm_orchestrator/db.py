from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Iterable

from autosfm_orchestrator.models import AutoSfmRun, ImageRecord, ReferenceRecord
from autosfm_orchestrator.utils import time_window_to_epoch

log = logging.getLogger(__name__)


class DatabaseError(RuntimeError):
    pass


class InventoryDb:
    """SQLite access layer for the nightly AgIR Globus inventory database.

    The important source table is `globus_file_index`. This class intentionally
    keeps SQL here so the workflow can operate on plain dataclasses.
    """

    def __init__(self, db_path: str | Path, config: dict | None = None):
        self.db_path = Path(db_path).expanduser()
        self.config = config or {}

    def connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise DatabaseError(f"SQLite database does not exist: {self.db_path}")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_images_for_time_window(
        self,
        batch_id: str,
        start_time: str,
        end_time: str,
    ) -> list[ImageRecord]:
        """Return current developed JPG/JPEG image files for a batch time window.

        This expects the nightly inventory DB schema with `globus_file_index` and
        a filename-derived epoch in `fname_ts_epoch`.
        """
        start_epoch, end_epoch = time_window_to_epoch(batch_id, start_time, end_time)
        filters = self._inventory_filters()
        data_state = filters.get("data_state", "semifield-developed-images")

        clauses = [
            "entry_type = 'file'",
            "is_current = 1",
            "batch_id = :batch_id",
            "data_state = :data_state",
            "LOWER(file_ext) IN ('jpg', 'jpeg')",
            "(parent_dir = 'images' OR rel_path LIKE '%/images/%')",
            "fname_ts_epoch IS NOT NULL",
            "fname_ts_epoch BETWEEN :start_epoch AND :end_epoch",
        ]
        params: dict[str, object] = {
            "batch_id": batch_id,
            "data_state": data_state,
            "start_epoch": start_epoch,
            "end_epoch": end_epoch,
        }
        self._add_optional_scope_filters(clauses, params, filters)

        query = f"""
            SELECT
                file_id,
                endpoint,
                site,
                storage_domain,
                namespace,
                storage_root,
                rel_path,
                full_path,
                file_name,
                batch_id,
                data_state,
                size_bytes,
                mtime_iso,
                fname_ts_epoch
            FROM globus_file_index
            WHERE {' AND '.join(clauses)}
            ORDER BY fname_ts_epoch, file_name
        """
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()

        records = [self._row_to_image_record(row) for row in rows]
        log.info(
            "Found %s current developed JPG records for %s between %s and %s",
            len(records),
            batch_id,
            start_time,
            end_time,
        )
        return records

    def find_gcp_reference(self, batch_id: str, season: str | None = None) -> ReferenceRecord | None:
        """Find a likely GCP reference CSV from globus_file_index.

        Multiple sites (NCSU, CERES, JUNO, ...) may each hold an indexed copy of
        the same reference file. `reference_filters.site_preference` in config
        controls which copy is preferred when more than one match is found, e.g.:

            reference_filters:
              site_preference:
                - CERES
                - NCSU
                - JUNO

        Sites not listed are ranked after all listed sites, in whatever order
        SQLite returns them. If site_preference is empty/unset, ties are broken
        arbitrarily (previous behavior) and a warning is logged when it happens.
        """
        filters = self._reference_filters()
        if not filters.get("enabled", True):
            return None

        state = batch_id.split("_", 1)[0]
        season = season or filters.get("season")

        clauses = [
            "entry_type = 'file'",
            "is_current = 1",
            "LOWER(file_ext) = 'csv'",
            "rel_path LIKE :marker_like",
            "file_name LIKE :state_like",
        ]
        params: dict[str, object] = {
            "marker_like": f"%{filters.get('marker_path_fragment', 'GroundControlPoints')}%",
            "state_like": f"%{state}%",
        }
        if season:
            clauses.append("rel_path LIKE :season_like")
            params["season_like"] = f"%/{season}/%"
        self._add_optional_scope_filters(clauses, params, filters)

        site_preference: list[str] = filters.get("site_preference") or []
        if site_preference:
            when_clauses = []
            for i, site in enumerate(site_preference):
                param_name = f"site_pref_{i}"
                when_clauses.append(f"WHEN site = :{param_name} THEN {i}")
                params[param_name] = site
            site_rank_sql = f"CASE {' '.join(when_clauses)} ELSE {len(site_preference)} END"
        else:
            site_rank_sql = "0"

        query = f"""
            SELECT file_id, endpoint, site, storage_root, rel_path, full_path, file_name
            FROM globus_file_index
            WHERE {' AND '.join(clauses)}
            ORDER BY
                {site_rank_sql},
                CASE WHEN file_name LIKE :state_prefix THEN 0 ELSE 1 END,
                rel_path,
                file_name
        """
        params["state_prefix"] = f"{state}%"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()

        if not rows:
            log.warning("No GCP reference CSV found in globus_file_index for %s season=%s", batch_id, season)
            return None

        if len(rows) > 1:
            sites_found = sorted({row["site"] for row in rows})
            if not site_preference:
                log.warning(
                    "Multiple GCP reference candidates for %s season=%s across sites=%s "
                    "and no reference_filters.site_preference configured; selection is "
                    "arbitrary. Selected site=%s (%s)",
                    batch_id, season, sites_found, rows[0]["site"], rows[0]["full_path"],
                )
            else:
                log.info(
                    "Multiple GCP reference candidates for %s season=%s across sites=%s; "
                    "selected site=%s per site_preference (%s)",
                    batch_id, season, sites_found, rows[0]["site"], rows[0]["full_path"],
                )

        row = rows[0]
        return ReferenceRecord(
            source_path=Path(row["full_path"]),
            filename=row["file_name"],
            file_id=row["file_id"],
            endpoint=row["endpoint"],
            site=row["site"],
            storage_root=Path(row["storage_root"]),
            rel_path=Path(row["rel_path"]),
        )

    def summarize_batch(self, batch_id: str) -> list[dict]:
        """Return current batch summary rows, useful for debugging CLI output."""
        query = """
            SELECT
                site,
                storage_domain,
                namespace,
                storage_root,
                data_state,
                file_count,
                dir_count,
                raw_count,
                jpg_count,
                metadata_count,
                detection_count,
                total_size_bytes
            FROM batch_inventory_summary
            WHERE batch_id = ?
            ORDER BY site, storage_domain, namespace, storage_root, data_state
        """
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, (batch_id,)).fetchall()]

    def upsert_run_status(self, run: AutoSfmRun, status: str, message: str | None = None) -> None:
        """Record AutoSfM orchestration status in the same SQLite DB.

        The nightly inventory tables remain untouched. This creates a small
        operational table for this app if it does not already exist.
        """
        if not self.config.get("database", {}).get("write_run_status", False):
            log.debug(
                "Skipping SQLite autosfm run status update because "
                "database.write_run_status=false"
            )
            return
        
        try:
            with self.connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS autosfm_runs (
                        run_id TEXT PRIMARY KEY,
                        batch_id TEXT NOT NULL,
                        sub_batch_id TEXT,
                        start_time TEXT,
                        end_time TEXT,
                        status TEXT NOT NULL,
                        message TEXT,
                        image_count INTEGER NOT NULL DEFAULT 0,
                        reference_path TEXT,
                        staging_path TEXT,
                        ceres_output_path TEXT,
                        created_at_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                        updated_at_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO autosfm_runs (
                        run_id, batch_id, sub_batch_id, start_time, end_time,
                        status, message, image_count, reference_path,
                        staging_path, ceres_output_path, updated_at_ts_iso
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    ON CONFLICT(run_id) DO UPDATE SET
                        status = excluded.status,
                        message = excluded.message,
                        image_count = excluded.image_count,
                        reference_path = excluded.reference_path,
                        staging_path = excluded.staging_path,
                        ceres_output_path = excluded.ceres_output_path,
                        updated_at_ts_iso = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    """,
                    (
                        run.run_id,
                        run.batch_id,
                        run.sub_batch_id,
                        run.start_time,
                        run.end_time,
                        status,
                        message,
                        len(run.image_records),
                        str(run.source_reference_path) if run.source_reference_path else None,
                        str(run.paths.run_root),
                        str(run.ceres_output_path),
                    ),
                )
                conn.commit()
        except Exception as exc:
            log.warning("Could not update autosfm run status in SQLite: %s", exc)

    def _row_to_image_record(self, row: sqlite3.Row) -> ImageRecord:
        return ImageRecord(
            batch_id=row["batch_id"],
            image_id=Path(row["file_name"]).stem,
            filename=row["file_name"],
            source_path=Path(row["full_path"]),
            timestamp=row["fname_ts_epoch"],
            file_id=row["file_id"],
            endpoint=row["endpoint"],
            site=row["site"],
            storage_domain=row["storage_domain"],
            namespace=row["namespace"],
            storage_root=Path(row["storage_root"]),
            rel_path=Path(row["rel_path"]),
            data_state=row["data_state"],
            size_bytes=row["size_bytes"],
            mtime_iso=row["mtime_iso"],
        )

    def _inventory_filters(self) -> dict:
        return {
            **self.config.get("database", {}).get("inventory_filters", {}),
            **self.config.get("inventory_filters", {}),
        }

    def _reference_filters(self) -> dict:
        base = self.config.get("database", {}).get("reference_filters", {}).copy()
        base.update(self.config.get("reference_filters", {}))
        return base


    @staticmethod
    def _add_optional_scope_filters(clauses: list[str], params: dict[str, object], filters: dict) -> None:
        optional_columns = ["endpoint", "site", "storage_domain", "namespace", "storage_root"]
        for column in optional_columns:
            value = filters.get(column)
            if value in (None, ""):
                continue
            clauses.append(f"{column} = :{column}")
            params[column] = value


def ensure_records_found(records: Iterable[ImageRecord], batch_id: str) -> list[ImageRecord]:
    records = list(records)
    if not records:
        raise DatabaseError(f"No current developed JPG records found for batch/time window: {batch_id}")
    return records