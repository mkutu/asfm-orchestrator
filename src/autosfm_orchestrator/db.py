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
        print(f"Searching for images in batch {batch_id} between {start_time} and {end_time} (epoch {start_epoch}-{end_epoch})")
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

    def compute_batches_ready_for_asfm(
        self,
        site: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        source_storage_sites: tuple[str, ...] = ("CERES", "JUNO"),
    ) -> list[dict]:
        """Live cross-database computation of batches that have developed JPGs
        somewhere in `source_storage_sites` but no promoted output yet under
        data_state='semifield-asfm' at the CERES destination.

        This walks the full globus_file_index.sqlite3 inventory (large, and
        only refreshed nightly), so it's meant to be called by the
        `refresh-ready` CLI command to rebuild the `batches_ready_for_asfm`
        snapshot table in AsfmRunStatusDb, not on every `list-ready` lookup -
        see AsfmRunStatusDb.list_ready_batches() for the fast filtered read
        path.

        Developed images for a batch can be indexed at more than one storage
        site - e.g. CERES's /90daydata/dash_agir/semifield-developed-images
        (the active copy get_images_for_time_window() actually stages from)
        and JUNO's /LTS/project/dash_agir/semifield-developed-images
        long-term archive - so unlike that staging query, this check spans
        `source_storage_sites` rather than pinning to database.inventory_filters'
        single site/namespace/storage_root. Promoted AutoSfM output, by
        contrast, only ever lands at the CERES destination
        (paths.ceres_output_root), so the "already has output" check stays
        scoped to database.inventory_filters.

        The semifield-asfm rel_path tree also holds the SUNNY-side `staging/`
        working copy and the `db/` run-status DB, neither of which means a
        batch has actually landed at its CERES destination, so both are
        excluded from the "already has output" check.

        `site` filters on batch_state (the field site code, e.g. NC/MD/TX -
        distinct from the storage `site` column CERES/NCSU/JUNO/ATLAS, exposed
        per row below as `storage_site`).
        `start_date`/`end_date` filter batch_date (YYYY-MM-DD), inclusive.

        Returns one row per (batch_id, storage_site, storage_domain, namespace,
        storage_root) combination the source images were found at, rather than
        one row per batch - a batch indexed at both CERES and JUNO gets two
        rows, each with its own consistent storage_domain/namespace/storage_root
        (these differ by site, so collapsing to a single row would require
        picking one arbitrarily or GROUP_CONCAT-ing each column independently,
        which wouldn't keep values aligned across columns).

        Each row also carries `has_legacy_reference`: some batches already
        went through AutoSfM under an older version, before output was
        promoted to the semifield-asfm destination the way it is now: for
        those, the only trace left is a `reference/` folder under
        semifield-developed-images/<batch_id>/ itself. Such a batch has no
        semifield-asfm output, so it still isn't excluded from this "ready"
        set - the flag just lets a caller tell "genuinely never processed"
        apart from "processed before, possibly just needs a rerun under the
        current version/output convention" before deciding to run it again.
        """
        filters = self._inventory_filters()
        source_data_state = filters.get("data_state", "semifield-developed-images")

        params: dict[str, object] = {"source_data_state": source_data_state}

        scope_clauses = []
        if source_storage_sites:
            site_params = []
            for i, storage_site in enumerate(source_storage_sites):
                key = f"source_storage_site_{i}"
                site_params.append(f":{key}")
                params[key] = storage_site
            scope_clauses.append(f"site IN ({', '.join(site_params)})")

        storage_domain = filters.get("storage_domain")
        if storage_domain:
            scope_clauses.append("storage_domain = :storage_domain")
            params["storage_domain"] = storage_domain

        source_clauses = [
            "entry_type = 'file'",
            "is_current = 1",
            "batch_id IS NOT NULL",
            "batch_id != ''",
            "data_state = :source_data_state",
            "LOWER(file_ext) IN ('jpg', 'jpeg')",
            "(parent_dir = 'images' OR rel_path LIKE '%/images/%')",
            *scope_clauses,
        ]

        if site:
            source_clauses.append("batch_state = :field_site")
            params["field_site"] = site
        if start_date:
            source_clauses.append("batch_date >= :start_date")
            params["start_date"] = start_date
        if end_date:
            source_clauses.append("batch_date <= :end_date")
            params["end_date"] = end_date

        legacy_reference_clauses = [
            "is_current = 1",
            "batch_id IS NOT NULL",
            "batch_id != ''",
            "data_state = :source_data_state",
            "(parent_dir = 'reference' OR rel_path LIKE '%/reference/%')",
            *scope_clauses,
        ]

        dst_clauses = [
            "is_current = 1",
            "batch_id IS NOT NULL",
            "batch_id != ''",
            "data_state = 'semifield-asfm'",
            "rel_path NOT LIKE '%/staging/%'",
            "rel_path NOT LIKE '%/db/%'",
        ]
        self._add_optional_scope_filters(dst_clauses, params, filters)

        query = f"""
            WITH source_batches AS (
                SELECT
                    batch_id,
                    batch_state,
                    site            AS storage_site,
                    storage_domain,
                    namespace,
                    storage_root,
                    MIN(batch_date) AS batch_date,
                    COUNT(*)        AS jpg_count
                FROM globus_file_index
                WHERE {' AND '.join(source_clauses)}
                GROUP BY batch_id, batch_state, site, storage_domain, namespace, storage_root
            ),
            dst_batches AS (
                SELECT DISTINCT batch_id
                FROM globus_file_index
                WHERE {' AND '.join(dst_clauses)}
            ),
            legacy_reference_batches AS (
                SELECT DISTINCT batch_id
                FROM globus_file_index
                WHERE {' AND '.join(legacy_reference_clauses)}
            )
            SELECT
                s.batch_id,
                s.batch_state AS site,
                s.batch_date,
                s.storage_site,
                s.storage_domain,
                s.namespace,
                s.storage_root,
                s.jpg_count,
                CASE WHEN lr.batch_id IS NOT NULL THEN 1 ELSE 0 END AS has_legacy_reference
            FROM source_batches s
            LEFT JOIN dst_batches d ON s.batch_id = d.batch_id
            LEFT JOIN legacy_reference_batches lr ON s.batch_id = lr.batch_id
            WHERE d.batch_id IS NULL
            ORDER BY s.batch_date ASC, s.batch_id ASC, s.storage_site ASC
        """
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

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


class AsfmRunStatusDb:
    """SQLite run-status tracker written on SUNNY and promoted to JUNO.

    Lives on NFS (accessible from SUNNY). The database file and its parent
    directory are created on first write. Never written from SciNet/JUNO.
    """

    def __init__(self, db_path: str | Path, config: dict | None = None):
        self.db_path = Path(db_path).expanduser()
        self.config = config or {}

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def upsert_run_status(self, run: AutoSfmRun, status: str, message: str | None = None) -> None:
        """Record AutoSfM run status, creating the database and table if needed."""
        if not self.config.get("run_status_db", {}).get("enabled", False):
            log.debug(
                "Skipping run status DB update because run_status_db.enabled=false"
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
            log.warning("Could not update run status in SQLite: %s", exc)

    def refresh_batches_ready_for_asfm(self, rows: Iterable[dict]) -> None:
        """Replaces the batches_ready_for_asfm snapshot table with `rows`, as
        produced by InventoryDb.compute_batches_ready_for_asfm(). Call this
        (e.g. the `refresh-ready` CLI command, on a cron alongside the
        nightly globus_file_index rebuild) whenever the upstream inventory
        may have changed; list_ready_batches() only ever reads this snapshot,
        it never touches globus_file_index.sqlite3.

        One row per (batch_id, storage_site, storage_domain, namespace,
        storage_root) - a batch indexed at both CERES and JUNO gets two rows.
        The table is dropped and recreated each refresh (rather than migrated
        in place) since it's a fully-derived cache, not a source of truth.
        """
        rows = list(rows)
        with self.connect() as conn:
            conn.execute("DROP TABLE IF EXISTS batches_ready_for_asfm")
            conn.execute(
                """
                CREATE TABLE batches_ready_for_asfm (
                    batch_id TEXT NOT NULL,
                    site TEXT NOT NULL,
                    batch_date TEXT NOT NULL,
                    storage_site TEXT NOT NULL,
                    storage_domain TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    storage_root TEXT NOT NULL,
                    jpg_count INTEGER NOT NULL,
                    has_legacy_reference INTEGER NOT NULL DEFAULT 0 CHECK (has_legacy_reference IN (0, 1)),
                    computed_at_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    PRIMARY KEY (batch_id, storage_site, storage_domain, namespace, storage_root)
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO batches_ready_for_asfm
                    (batch_id, site, batch_date, storage_site, storage_domain,
                     namespace, storage_root, jpg_count, has_legacy_reference)
                VALUES (:batch_id, :site, :batch_date, :storage_site, :storage_domain,
                        :namespace, :storage_root, :jpg_count, :has_legacy_reference)
                """,
                rows,
            )
            conn.commit()
        log.info("Refreshed batches_ready_for_asfm with %d rows", len(rows))

    def list_ready_batches(
        self,
        site: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        """Fast filtered read of the batches_ready_for_asfm snapshot table.
        Raises DatabaseError if the snapshot hasn't been built yet. Returns
        one row per (batch_id, storage_site, storage_domain, namespace,
        storage_root) - see refresh_batches_ready_for_asfm()."""
        clauses = []
        params: dict[str, object] = {}
        if site:
            clauses.append("site = :site")
            params["site"] = site
        if start_date:
            clauses.append("batch_date >= :start_date")
            params["start_date"] = start_date
        if end_date:
            clauses.append("batch_date <= :end_date")
            params["end_date"] = end_date
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        query = f"""
            SELECT
                batch_id, site, batch_date, storage_site, storage_domain,
                namespace, storage_root, jpg_count, has_legacy_reference,
                computed_at_ts_iso
            FROM batches_ready_for_asfm
            {where_sql}
            ORDER BY batch_date ASC, batch_id ASC, storage_site ASC
        """
        with self.connect() as conn:
            try:
                rows = conn.execute(query, params).fetchall()
            except sqlite3.OperationalError as exc:
                raise DatabaseError(
                    "batches_ready_for_asfm table not found - run "
                    "`autosfm-orchestrator refresh-ready` first"
                ) from exc
        return [dict(row) for row in rows]


def ensure_records_found(records: Iterable[ImageRecord], batch_id: str) -> list[ImageRecord]:
    records = list(records)
    if not records:
        raise DatabaseError(f"No current developed JPG records found for batch/time window: {batch_id}")
    return records