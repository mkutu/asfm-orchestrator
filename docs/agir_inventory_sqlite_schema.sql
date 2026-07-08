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
