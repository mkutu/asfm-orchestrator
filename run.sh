#!/usr/bin/env bash
set -uo pipefail

# Runs one row per line through `autosfm-orchestrator run-one`, each in its
# own process group, pinned to a fixed CPU range so a single batch can't
# monopolize SUNNY (shared, single node, no scheduler). Ported from the old
# SemiF-Preprocessing batch-loop script; text-parsing simplified to just
# batch_id | start_time | end_time (matches manual_log.csv columns) since
# per-batch resize/downscale overrides now live in conf/default.yaml instead
# of being passed as Hydra overrides per row. Re-add columns here later if
# you decide you need per-batch overrides of the autosfm config.

CONFIG="${AUTOSFM_CONFIG:-conf/default.yaml}"
INPUT_FILE="${1:-conf/batch_run_list.txt}"
CPU_RANGE="${AUTOSFM_CPU_RANGE:-2-31}"   # leaves cores 0-1 free for the rest of SUNNY
GFI_DB_NFS_PATH="${AUTOSFM_GFI_DB_NFS_PATH:-/mnt/research-projects/s/screberg/longterm_images2/globus_index/globus_file_index.sqlite3}"
GFI_DB_LOCAL_PATH="${AUTOSFM_GFI_DB_LOCAL_PATH:-./db/globus_file_index.sqlite3}"


# Make sure the local db is the most recent copy from the NFS path

echo "Updating local GFI DB from NFS path..."
mkdir -p "$(dirname "$GFI_DB_LOCAL_PATH")"
rsync -avhP "$GFI_DB_NFS_PATH" "$GFI_DB_LOCAL_PATH"

if [[ ! -f "$INPUT_FILE" ]]; then
    echo "ERROR: Input file not found: $INPUT_FILE"
    exit 1
fi

input_dir="$(dirname "$INPUT_FILE")"
input_base="$(basename "$INPUT_FILE")"
input_stem="${input_base%.*}"
input_ext="${input_base##*.}"

if [[ "$input_base" == "$input_ext" ]]; then
    RESULTS_FILE="${input_dir}/${input_base}_results"
    echo "WARNING: Input file has no extension. Results file will be: $RESULTS_FILE"
else
    RESULTS_FILE="${input_dir}/${input_stem}_results.${input_ext}"
    echo "Results will be saved to: $RESULTS_FILE"
fi

{
    echo "Batch run started: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "Input file: $INPUT_FILE"
    echo "Config: $CONFIG"
    echo "CPU range: $CPU_RANGE"
    echo "=================================================="
} >> "$RESULTS_FILE"


cleanup_process_group() {
    local pgid="$1"
    local batch_id="$2"

    if ! pgrep -g "$pgid" >/dev/null 2>&1; then
        return 0
    fi

    echo "WARNING: leftover processes detected after batch $batch_id"
    echo "Process group: $pgid"
    ps -o pid,ppid,pgid,stat,etime,cmd -g "$pgid" || true

    echo "Sending SIGTERM to leftover processes for batch $batch_id..."
    pkill -TERM -g "$pgid" || true

    for _ in {1..10}; do
        if ! pgrep -g "$pgid" >/dev/null 2>&1; then
            echo "Cleanup complete for batch $batch_id"
            return 0
        fi
        sleep 1
    done

    echo "WARNING: some processes did not exit; sending SIGKILL for batch $batch_id..."
    pkill -KILL -g "$pgid" || true
}


batch_pid=""

cleanup_on_interrupt() {
    echo
    echo "Interrupted. Cleaning up current batch process group..."
    if [[ -n "${batch_pid:-}" ]]; then
        cleanup_process_group "$batch_pid" "${batch_id:-unknown}"
    fi
    exit 130
}

trap cleanup_on_interrupt INT TERM


# Expected input format, one row per line:
#   batch_id | start_time | end_time
# e.g.
#   NC_2026-06-04 | 10:12:00 | 10:18:30
# Comment lines starting with # and blank lines are skipped.

while IFS='|' read -r batch_id start_time end_time; do
    echo "--------------------------------------------------"

    [[ -z "${batch_id// }" ]] && continue

    batch_id="$(echo "$batch_id" | xargs)"
    start_time="$(echo "${start_time:-}" | xargs)"
    end_time="$(echo "${end_time:-}" | xargs)"

    [[ "${batch_id:0:1}" == "#" ]] && continue

    echo "Processing batch_id: $batch_id"
    [[ -n "$start_time" ]] && echo "  Start: $start_time"
    [[ -n "$end_time" ]] && echo "  End:   $end_time"

    cmd=(
        taskset -c "$CPU_RANGE" uv run autosfm-orchestrator
        --config "$CONFIG"
        run-one
        --batch-id "$batch_id"
    )
    [[ -n "$start_time" ]] && cmd+=(--start-time "$start_time")
    [[ -n "$end_time" ]] && cmd+=(--end-time "$end_time")

    echo "Command:"
    printf '  %q' "${cmd[@]}"
    echo

    # Run each batch in its own session/process group so it can be taskset'd
    # and cleanly torn down independently of the loop and of other batches.
    setsid "${cmd[@]}" &
    batch_pid=$!

    wait "$batch_pid"
    exit_code=$?

    cleanup_process_group "$batch_pid" "$batch_id"

    if [[ "$exit_code" -eq 0 ]]; then
        status="SUCCESS"
    else
        status="FAILED"
    fi
    echo "$status: $batch_id | start=$start_time | end=$end_time | exit_code=$exit_code"

    {
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] $status | batch_id=$batch_id | start=$start_time | end=$end_time | exit_code=$exit_code"
    } >> "$RESULTS_FILE"

    batch_pid=""

done < "$INPUT_FILE"

{
    echo "=================================================="
    echo "Batch run finished: $(date '+%Y-%m-%d %H:%M:%S')"
} >> "$RESULTS_FILE"

echo "Done. Results saved to $RESULTS_FILE"