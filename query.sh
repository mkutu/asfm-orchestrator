#!/usr/bin/env bash

DB_PATH=/home/mkutuga/asfm-orchestrator/db/globus_file_index.sqlite3



sqlite3 -header -column "$DB_PATH" <<'SQL'
SELECT *
FROM v_batches_needing_jpg_to_det
WHERE "batch_id" LIKE '%NC_2026-06-30%'
LIMIT 100;
SQL