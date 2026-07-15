# AutoSfM Orchestrator

SUNNY-oriented orchestration for running AutoSfM from database/manual-log selected image subsets.

The first version is intentionally small:

1. Read a manual log or CLI arguments for `batch_id`, `start_time`, and `end_time`.
2. Query the nightly AgIR SQLite Globus inventory database.
3. Select current developed JPGs from `globus_file_index` using `batch_id` and filename timestamps.
4. Build a run manifest.
5. Create a locked staging workspace.
6. Stage inputs from CERES to SUNNY-accessible NFS with Globus.
7. Run AutoSfM on staged inputs.
8. Collect pixel grids and reference outputs.
9. Promote outputs back to CERES `/90daydata`.
10. Update SQLite run status in an `autosfm_runs` operational table.

The scaffold defaults to dry-run mode for both Globus and AutoSfM so the planning/staging flow can be tested before running Metashape.

## Install

```bash
uv sync
```

Or, for editable development:

```bash
uv pip install -e .
```

## Commands

Plan one run without executing real transfers or AutoSfM:

```bash
autosfm-orchestrator plan-one   --config conf/default.yaml   --batch-id NC_2026-06-04   --start-time "10:12:00"   --end-time "10:18:30"   --season peanuts_2026
```

Run one sub-batch:

```bash
autosfm-orchestrator run-one   --config conf/default.yaml   --batch-id NC_2026-06-04   --start-time "10:12:00"   --end-time "10:18:30"   --season peanuts_2026
```

Plan from manual log:

```bash
autosfm-orchestrator plan-log   --config conf/default.yaml   --manual-log /path/to/manual_log.csv
```

Summarize indexed inventory for a batch:

```bash
autosfm-orchestrator summarize-batch   --config conf/default.yaml   --batch-id NC_2026-06-04
```

Recompute the `batches_ready_for_asfm` snapshot table in the run-status DB (`run_status_db.db_path`) from `globus_file_index.sqlite3` - batches with developed images (indexed at CERES and/or JUNO) but no AutoSfM output yet at the CERES destination. This walks the full (large, nightly-refreshed) inventory DB, so run it once after each inventory refresh rather than per lookup:

```bash
autosfm-orchestrator refresh-ready   --config conf/default.yaml
```

List batches from that snapshot, optionally filtered by field site and batch date - a fast local read that never touches `globus_file_index.sqlite3`:

```bash
autosfm-orchestrator list-ready   --config conf/default.yaml   --site NC   --start-date 2026-06-01   --end-date 2026-07-15
```

The snapshot has one row per (batch, storage location) rather than one row per batch, since a batch's developed images can be indexed at more than one storage site with different paths - e.g. `storage_site=CERES storage_domain=dash_agir namespace=90daydata storage_root=/90daydata/dash_agir` vs. `storage_site=JUNO storage_domain=dash_agir namespace=LTS storage_root=/LTS/project/dash_agir`. A batch found only at JUNO isn't stageable as-is with this scaffold's current CERES-only staging step (`transfer.stage_inputs`); it would need re-indexing or transfer to CERES first.

Each row also carries `has_legacy_reference`: some batches already went through AutoSfM under an older version, before output was promoted to the current `semifield-asfm` destination - for those the only trace left is a `reference/` folder under `semifield-developed-images/<batch_id>/`. Such a batch has no `semifield-asfm` output, so it isn't excluded from `list-ready`; the flag just distinguishes "genuinely never processed" from "processed before, possibly just needs a rerun under the current version" so you can decide whether to reprocess it.

## Expected manual log columns

Required:

```text
batch_id,start_time,end_time
```

Optional:

```text
sub_batch_id,notes,cam_angle,z_axis,season,process
```

Rows with `process=false`, `process=no`, `process=0`, or `process=skip` are ignored.

## SQLite expectations

This scaffold is now wired to the nightly AgIR inventory schema.

Image selection reads from `globus_file_index` and expects:

```sql
batch_id TEXT,
data_state TEXT,
entry_type TEXT,
file_ext TEXT,
parent_dir TEXT,
rel_path TEXT,
full_path TEXT,
file_name TEXT,
fname_ts_epoch INTEGER,
is_current INTEGER
```

The default query selects:

- `entry_type = 'file'`
- `is_current = 1`
- `data_state = 'semifield-developed-images'`
- `file_ext IN ('jpg', 'jpeg')`
- `parent_dir = 'images'` or `rel_path LIKE '%/images/%'`
- `fname_ts_epoch` within the requested manual-log time window

Optional scope filters live in `conf/default.yaml` under:

```yaml
database:
  inventory_filters:
    endpoint: ...
    site: CERES
    storage_domain: dash_agir
    namespace: LTS
    storage_root: /LTS/project/dash_agir
```

Set any of those values to blank/null to relax that filter.

## Important design boundary

`autosfm.py` should only know how to run AutoSfM from staged paths. It should not query SQLite, parse manual logs, or know CERES/NCSU endpoint details.
