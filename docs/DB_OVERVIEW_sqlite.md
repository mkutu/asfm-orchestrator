# AgIR SQLite Database Overview

**Database type:** SQLite  
**Primary database file:** configurable via the Globus inventory/indexing scripts  
**Primary purpose:** Track agricultural image file inventories discovered through Globus across storage locations, including RAW uploads, developed JPG images, detections, metadata, and semifield cutouts.

This overview replaces the older PostgreSQL-oriented `source.globus_file_index` documentation with the current SQLite design used by `scripts/admin/globus_index.py`.

---

## Design notes

The SQLite Globus index is a local operational inventory database. It is used to answer:

- What files and directories currently exist in each indexed storage tree?
- Which batches exist under each `data_state`?
- Which batches have RAWs, JPGs, metadata, detections, or cutouts?
- Which files were seen in the latest crawl, and which previously indexed files are now stale?
- Which inventory runs succeeded, failed, or produced partial results?

SQLite-specific conventions:

- There are no PostgreSQL schemas such as `source`, `logs`, or `report`.
- Table names are flat: `globus_file_index`, not `source.globus_file_index`.
- Timestamp/date values are stored as ISO-8601 `TEXT`.
- Boolean values are stored as `INTEGER` values constrained to `0` or `1`.
- Auto-incrementing IDs use `INTEGER PRIMARY KEY AUTOINCREMENT`.
- The original PostgreSQL uniqueness rule is preserved on `globus_file_index`:  
  `UNIQUE(endpoint, data_state, storage_root, rel_path)`.

---

## Table overview

| Table | Purpose | Grain |
| --- | --- | --- |
| `inventory_runs` | One row per Globus inventory crawl/run for a specific endpoint/root/data-state scope. | Inventory run |
| `globus_file_index` | File and directory inventory discovered by Globus. This is the main physical file index. | One file or directory |
| `batch_inventory_summary` | Per-run batch-level file counts and artifact counts for one indexed scope. | Run + batch + storage scope + data state |
| `storage_gap_summary` | Per-run cross-data-state summary showing whether upload/developed/cutout trees exist for each batch. | Run + batch + storage scope |

---

## `globus_file_index`

### Purpose

`globus_file_index` is the authoritative physical inventory table for files and directories found through Globus. It records storage identity, filesystem path details, file attributes, parsed batch identity, timestamps, and current/stale tracking information.

### Common use cases

- Discover batches under storage roots.
- Detect gaps such as RAW uploads with missing developed JPGs.
- Track file presence across storage systems such as JUNO, NCSU, CERES, and ATLAS.
- Audit file counts and sizes by batch, data state, endpoint, or storage root.
- Keep historical knowledge of files that were seen before but are no longer present in the latest crawl.

### Uniqueness

A file/directory is uniquely identified by:

```sql
UNIQUE (endpoint, data_state, storage_root, rel_path)
```

This lets the scanner safely upsert rows during repeated crawls without deleting and rebuilding the table.

### Column descriptions

| Table column | Grouping | Description | Example |
| --- | --- | --- | --- |
| `file_id` | Row identity | SQLite surrogate primary key. | `1234567` |
| `endpoint` | Storage location | Globus endpoint UUID that was crawled. | `f45a24f8-09ba-11ec-b342-1feaf93e3729` |
| `site` | Storage location | Human-readable site label. | `JUNO`, `NCSU`, `CERES`, `ATLAS` |
| `storage_domain` | Storage location | Logical owner/program tag for the indexed tree. | `dash_agir`, `screberg`, `national_plant_image_repository` |
| `namespace` | Storage location | Logical namespace or filesystem tier. | `LTS`, `90daydata`, `project` |
| `storage_root` | Filesystem | Absolute root path being indexed. | `/LTS/project/dash_agir` |
| `rel_path` | Filesystem | Path relative to `storage_root`. | `semifield-upload/TX_2025-08-18/raws/img_0001.raw` |
| `full_path` | Filesystem | Absolute path to the indexed file or directory. | `/LTS/project/dash_agir/semifield-upload/TX_2025-08-18/raws/img_0001.raw` |
| `parent_dir` | Filesystem structure | Immediate parent folder name for files; `NULL` for directories or root-level entries. | `images`, `metadata`, `plant-detections` |
| `file_name` | Filesystem structure | Base file or directory name returned by Globus. | `img_0001.jpg`, `metadata` |
| `entry_type` | Filesystem structure | Item type constrained to `file` or `dir`. | `file` |
| `file_ext` | File attributes | Lowercase extension without the dot; `NULL` for directories. | `jpg`, `json`, `raw` |
| `size_bytes` | File attributes | File size in bytes as reported by Globus. | `1048576` |
| `permissions` | File attributes | Permissions string from Globus metadata. | `0644`, `0755` |
| `checksum` | File integrity | Reserved checksum column. Currently preserved but usually `NULL`. | `NULL` |
| `batch_id` | Batch identity | Parsed batch ID from a path component matching `STATE_YYYY-MM-DD`. | `TX_2025-08-18` |
| `batch_state` | Batch identity | State/region parsed from `batch_id`. | `TX`, `NC`, `MD` |
| `batch_date` | Batch identity | Date parsed from `batch_id`, stored as ISO-style `TEXT`. | `2025-08-18` |
| `data_state` | Batch identity | Logical tree under `storage_root`. | `semifield-upload`, `semifield-developed-images`, `semifield-cutouts` |
| `mtime_iso` | Timestamps | File modification timestamp from Globus, stored as ISO-8601 `TEXT`. | `2025-12-01T14:22:03+00:00` |
| `fname_ts_epoch` | Timestamps | Epoch timestamp parsed from the filename when present. | `1733412345` |
| `fname_ts_iso` | Timestamps | ISO timestamp derived from `fname_ts_epoch`. | `2025-08-18T12:34:56+00:00` |
| `created_at_ts_iso` | Indexing | Timestamp when the row was first inserted. | `2026-07-07T14:15:10.123Z` |
| `first_seen_ts_iso` | Inventory tracking | First crawl timestamp when the item was observed. | `2026-07-07T14:15:10Z` |
| `last_seen_ts_iso` | Inventory tracking | Most recent crawl timestamp when the item was observed. | `2026-07-07T14:20:10Z` |
| `last_seen_run_id` | Inventory tracking | `inventory_runs.run_id` for the latest run that observed the item. | `42` |
| `missing_since_ts_iso` | Inventory tracking | Timestamp when the item was first marked missing/stale. | `2026-07-08T02:00:00Z` |
| `is_current` | Inventory tracking | `1` if seen in the latest relevant crawl, `0` if stale. | `1` |

### SQLite schema

```sql
CREATE TABLE IF NOT EXISTS globus_file_index (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,

    endpoint TEXT NOT NULL,
    site TEXT NOT NULL,
    storage_domain TEXT NOT NULL,
    namespace TEXT NOT NULL,
    storage_root TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    full_path TEXT NOT NULL,
    parent_dir TEXT,
    file_name TEXT NOT NULL,

    entry_type TEXT NOT NULL CHECK (entry_type IN ('file', 'dir')),
    file_ext TEXT,
    size_bytes INTEGER,
    permissions TEXT,
    checksum TEXT,

    batch_id TEXT,
    batch_state TEXT,
    batch_date TEXT,

    data_state TEXT NOT NULL,

    mtime_iso TEXT,
    fname_ts_epoch INTEGER,
    fname_ts_iso TEXT,
    created_at_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    first_seen_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_run_id INTEGER,
    missing_since_ts_iso TEXT,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),

    UNIQUE (endpoint, data_state, storage_root, rel_path),
    FOREIGN KEY (last_seen_run_id) REFERENCES inventory_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_gfi_batch_state_current
    ON globus_file_index(batch_id, data_state, site, is_current);
CREATE INDEX IF NOT EXISTS idx_gfi_storage_current
    ON globus_file_index(site, storage_domain, namespace, storage_root, data_state, is_current);
CREATE INDEX IF NOT EXISTS idx_gfi_ext_parent
    ON globus_file_index(data_state, file_ext, parent_dir);
CREATE INDEX IF NOT EXISTS idx_gfi_run_current
    ON globus_file_index(last_seen_run_id, is_current);
CREATE INDEX IF NOT EXISTS idx_gfi_full_path
    ON globus_file_index(full_path);
```

---

## `inventory_runs`

### Purpose

`inventory_runs` records one row per Globus crawl. It provides run-level status, timing, scope, counts, and error summaries.

### Important fields

| Column | Description |
| --- | --- |
| `run_id` | Auto-incrementing run identifier. |
| `endpoint`, `site`, `storage_domain`, `namespace`, `storage_root`, `data_state` | The indexed scope for the run. |
| `started_at_ts_iso`, `ended_at_ts_iso` | Run timing stored as ISO-8601 `TEXT`. |
| `status` | One of `running`, `success`, `partial`, `failed`, or `skipped`. |
| `total_seen`, `total_files_seen`, `total_dirs_seen` | Crawl totals. |
| `total_batches_written` | Number of upsert batches written by the scanner. |
| `total_errors` | Count of crawl/listing errors. |
| `total_marked_stale` | Number of previously current rows marked stale after the run. |
| `error_summary` | Optional text summary of errors. |

---

## `batch_inventory_summary`

### Purpose

`batch_inventory_summary` is rebuilt per inventory run and summarizes current inventory for each batch in a single storage/data-state scope.

### Key metrics

| Column | Description |
| --- | --- |
| `file_count`, `dir_count` | Number of current files and directories. |
| `total_size_bytes` | Total current file size for the batch/scope. |
| `raw_count` | Count of RAW-like image files: `raw`, `arw`, `rw2`, `nef`, `cr2`, `dng`, `raf`, `orf`. |
| `jpg_count` | Count of `jpg`/`jpeg` files. |
| `json_count`, `csv_count`, `png_count` | Basic extension counts. |
| `metadata_count` | Files under a `metadata` parent/path. |
| `detection_count` | Files under `detections` or `plant-detections`. |
| `min_mtime_iso`, `max_mtime_iso` | Modification time range for current rows. |

---

## `storage_gap_summary`

### Purpose

`storage_gap_summary` gives a per-run, per-batch cross-data-state view for one storage scope. It is useful for quickly identifying whether a batch has upload, developed image, and cutout trees.

### Key metrics

| Column | Description |
| --- | --- |
| `has_semifield_upload` | `1` if the batch exists under `semifield-upload`. |
| `has_semifield_developed_images` | `1` if the batch exists under `semifield-developed-images`. |
| `has_semifield_cutouts` | `1` if the batch exists under `semifield-cutouts`. |
| `upload_file_count` | File count under `semifield-upload`. |
| `developed_file_count` | File count under `semifield-developed-images`. |
| `cutout_file_count` | File count under `semifield-cutouts`. |
| `raw_count` | RAW-like files in upload state. |
| `jpg_count` | JPG/JPEG files in developed-images state. |
| `metadata_count` | Metadata files across current indexed rows. |
| `detection_count` | Detection files under `detections` or `plant-detections`. |
| `cutout_png_count` | PNG files under `semifield-cutouts`. |

---

## Complete SQLite schema

```sql
CREATE TABLE IF NOT EXISTS inventory_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT NOT NULL,
    site TEXT NOT NULL,
    storage_domain TEXT NOT NULL,
    namespace TEXT NOT NULL,
    storage_root TEXT NOT NULL,
    data_state TEXT NOT NULL,
    started_at_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at_ts_iso TEXT,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'success', 'partial', 'failed', 'skipped')),
    total_seen INTEGER NOT NULL DEFAULT 0,
    total_files_seen INTEGER NOT NULL DEFAULT 0,
    total_dirs_seen INTEGER NOT NULL DEFAULT 0,
    total_batches_written INTEGER NOT NULL DEFAULT 0,
    total_errors INTEGER NOT NULL DEFAULT 0,
    total_marked_stale INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT,
    UNIQUE (run_id)
);

CREATE TABLE IF NOT EXISTS globus_file_index (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,

    endpoint TEXT NOT NULL,
    site TEXT NOT NULL,
    storage_domain TEXT NOT NULL,
    namespace TEXT NOT NULL,
    storage_root TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    full_path TEXT NOT NULL,
    parent_dir TEXT,
    file_name TEXT NOT NULL,

    entry_type TEXT NOT NULL CHECK (entry_type IN ('file', 'dir')),
    file_ext TEXT,
    size_bytes INTEGER,
    permissions TEXT,
    checksum TEXT,

    batch_id TEXT,
    batch_state TEXT,
    batch_date TEXT,

    data_state TEXT NOT NULL,

    mtime_iso TEXT,
    fname_ts_epoch INTEGER,
    fname_ts_iso TEXT,
    created_at_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    first_seen_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_run_id INTEGER,
    missing_since_ts_iso TEXT,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),

    UNIQUE (endpoint, data_state, storage_root, rel_path),
    FOREIGN KEY (last_seen_run_id) REFERENCES inventory_runs(run_id)
);

CREATE TABLE IF NOT EXISTS batch_inventory_summary (
    run_id INTEGER NOT NULL,
    endpoint TEXT NOT NULL,
    site TEXT NOT NULL,
    storage_domain TEXT NOT NULL,
    namespace TEXT NOT NULL,
    storage_root TEXT NOT NULL,
    data_state TEXT NOT NULL,
    batch_id TEXT,
    batch_state TEXT,
    batch_date TEXT,

    file_count INTEGER NOT NULL,
    dir_count INTEGER NOT NULL,
    total_size_bytes INTEGER NOT NULL,
    raw_count INTEGER NOT NULL DEFAULT 0,
    jpg_count INTEGER NOT NULL DEFAULT 0,
    json_count INTEGER NOT NULL DEFAULT 0,
    csv_count INTEGER NOT NULL DEFAULT 0,
    png_count INTEGER NOT NULL DEFAULT 0,
    metadata_count INTEGER NOT NULL DEFAULT 0,
    detection_count INTEGER NOT NULL DEFAULT 0,
    min_mtime_iso TEXT,
    max_mtime_iso TEXT,
    created_at_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    PRIMARY KEY (run_id, endpoint, site, storage_domain, namespace, storage_root, data_state, batch_id),
    FOREIGN KEY (run_id) REFERENCES inventory_runs(run_id)
);

CREATE TABLE IF NOT EXISTS storage_gap_summary (
    run_id INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    batch_state TEXT,
    batch_date TEXT,
    storage_domain TEXT NOT NULL,
    namespace TEXT NOT NULL,
    site TEXT NOT NULL,
    storage_root TEXT NOT NULL,

    has_semifield_upload INTEGER NOT NULL DEFAULT 0 CHECK (has_semifield_upload IN (0, 1)),
    has_semifield_developed_images INTEGER NOT NULL DEFAULT 0 CHECK (has_semifield_developed_images IN (0, 1)),
    has_semifield_cutouts INTEGER NOT NULL DEFAULT 0 CHECK (has_semifield_cutouts IN (0, 1)),

    upload_file_count INTEGER NOT NULL DEFAULT 0,
    developed_file_count INTEGER NOT NULL DEFAULT 0,
    cutout_file_count INTEGER NOT NULL DEFAULT 0,

    raw_count INTEGER NOT NULL DEFAULT 0,
    jpg_count INTEGER NOT NULL DEFAULT 0,
    metadata_count INTEGER NOT NULL DEFAULT 0,
    detection_count INTEGER NOT NULL DEFAULT 0,
    cutout_png_count INTEGER NOT NULL DEFAULT 0,

    created_at_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    PRIMARY KEY (run_id, batch_id, storage_domain, namespace, site, storage_root),
    FOREIGN KEY (run_id) REFERENCES inventory_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_gfi_batch_state_current
    ON globus_file_index(batch_id, data_state, site, is_current);

CREATE INDEX IF NOT EXISTS idx_gfi_storage_current
    ON globus_file_index(site, storage_domain, namespace, storage_root, data_state, is_current);

CREATE INDEX IF NOT EXISTS idx_gfi_ext_parent
    ON globus_file_index(data_state, file_ext, parent_dir);

CREATE INDEX IF NOT EXISTS idx_gfi_run_current
    ON globus_file_index(last_seen_run_id, is_current);

CREATE INDEX IF NOT EXISTS idx_gfi_full_path
    ON globus_file_index(full_path);

CREATE INDEX IF NOT EXISTS idx_inventory_runs_scope_time
    ON inventory_runs(endpoint, site, storage_domain, namespace, storage_root, data_state, started_at_ts_iso DESC);

CREATE INDEX IF NOT EXISTS idx_batch_inventory_summary_batch
    ON batch_inventory_summary(batch_id, data_state, site, storage_domain, namespace);

CREATE INDEX IF NOT EXISTS idx_storage_gap_summary_batch
    ON storage_gap_summary(batch_id, site, storage_domain, namespace);
```

---

## Migration notes from old PostgreSQL overview

The old overview described `source.globus_file_index` as a PostgreSQL table using schema-qualified names, `BIGSERIAL`, `BIGINT`, `DATE`, `TIMESTAMPTZ`, and `now()` defaults. The current implementation is SQLite-oriented:

| PostgreSQL-style concept | SQLite replacement |
| --- | --- |
| `source.globus_file_index` | `globus_file_index` |
| `BIGSERIAL PRIMARY KEY` | `INTEGER PRIMARY KEY AUTOINCREMENT` |
| `BIGINT` | `INTEGER` |
| `DATE` / `TIMESTAMPTZ` | ISO-8601 `TEXT` |
| `BOOLEAN` | `INTEGER CHECK (... IN (0, 1))` |
| `now()` | `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')` |
| Schema-qualified organization | Flat tables in one SQLite database file |

The SQLite version also adds inventory lifecycle tracking columns: `first_seen_ts_iso`, `last_seen_ts_iso`, `last_seen_run_id`, `missing_since_ts_iso`, and `is_current`.

