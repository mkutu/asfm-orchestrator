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
