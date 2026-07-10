from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from autosfm_orchestrator.autosfm import AutoSfmRunner
from autosfm_orchestrator.db import AsfmRunStatusDb, InventoryDb, ensure_records_found
from autosfm_orchestrator.manual_log import read_manual_log
from autosfm_orchestrator.models import AutoSfmRun, ManualLogEntry
from autosfm_orchestrator.paths import build_run, create_workspace, remove_staged_images
from autosfm_orchestrator.staging import locked_workspace
from autosfm_orchestrator.transfer import (
    build_db_promotion_transfer_plan,
    build_input_transfer_plan,
    build_output_transfer_plan,
    execute_transfer,
)
from autosfm_orchestrator.utils import normalize_time_str, write_yaml

log = logging.getLogger(__name__)


def plan_one(
    config: dict,
    batch_id: str,
    start_time: str,
    end_time: str,
    sub_batch_id: str | None = None,
    season: str | None = None,
) -> AutoSfmRun:
    entry = ManualLogEntry(
        batch_id=batch_id,
        start_time=normalize_time_str(start_time),
        end_time=normalize_time_str(end_time),
        sub_batch_id=sub_batch_id,
        season=season,
    )
    return _plan_entry(config, entry)


def plan_log(config: dict, manual_log_path: str | Path) -> list[AutoSfmRun]:
    entries = read_manual_log(manual_log_path)
    runs: list[AutoSfmRun] = []
    for entry in entries:
        runs.append(_plan_entry(config, entry))
    return runs


def run_one(
    config: dict,
    batch_id: str,
    start_time: str,
    end_time: str,
    sub_batch_id: str | None = None,
    season: str | None = None,
) -> AutoSfmRun:
    run = plan_one(config, batch_id, start_time, end_time, sub_batch_id, season=season)
    return execute_run(config, run)


def run_log(config: dict, manual_log_path: str | Path) -> list[AutoSfmRun]:
    runs = plan_log(config, manual_log_path)
    completed: list[AutoSfmRun] = []
    for run in runs:
        completed.append(execute_run(config, run))
    return completed


def _plan_entry(config: dict, entry: ManualLogEntry) -> AutoSfmRun:
    db = InventoryDb(config["database"]["path"], config=config)
    records = ensure_records_found(
        db.get_images_for_time_window(entry.batch_id, entry.start_time, entry.end_time),
        entry.batch_id,
    )
    reference = db.find_gcp_reference(entry.batch_id, season=entry.season)
    run = build_run(config, entry, records, reference_record=reference)
    create_workspace(run.paths)
    write_manifest(run, status="planned")
    return run


def execute_run(config: dict, run: AutoSfmRun) -> AutoSfmRun:
    run_status_db = AsfmRunStatusDb(
        config.get("run_status_db", {}).get(
            "db_path", "/mnt/research-projects/s/screberg/longterm_images2/semifield-asfm/db/autosfm_run_status.sqlite3"
        ),
        config=config,
    )

    transfer_cfg = config.get("transfer", {})
    stage_inputs = transfer_cfg.get("stage_inputs", True)
    promote_outputs = transfer_cfg.get("promote_outputs", True)
    cleanup_staged_images = config.get("workspace", {}).get("cleanup_staged_images", False)

    with locked_workspace(run):
        try:
            _safe_upsert_run_status(run_status_db, run, "running")
            write_manifest(run, status="running")

            if stage_inputs:
                input_plans = build_input_transfer_plan(config, run)
                for plan in input_plans:
                    execute_transfer(config, plan)
            else:
                log.info(
                    "transfer.stage_inputs=false -- skipping input staging, "
                    "assuming %s is already populated from a prior run",
                    run.paths.inputs_dir,
                )

            outputs = AutoSfmRunner(config).run(run)
            log.info("AutoSfM outputs: %s", asdict(outputs))

            write_manifest(run, status="autosfm_complete")

            if promote_outputs:
                output_plan = build_output_transfer_plan(config, run)
                execute_transfer(config, output_plan)
            else:
                log.info("transfer.promote_outputs=false -- skipping output promotion to CERES")

            if cleanup_staged_images:
                remove_staged_images(run.paths)                
            else:
                log.info("workspace.cleanup_staged_images=false -- leaving staged NFS images in place")

            run.status = "success"
            write_manifest(run, status="success")
            _safe_upsert_run_status(run_status_db, run, "success")
            _promote_run_status_db(config)
            return run
        except Exception as exc:
            run.status = "failed"
            write_manifest(run, status="failed", message=str(exc))
            _safe_upsert_run_status(run_status_db, run, "failed", message=str(exc))
            _promote_run_status_db(config)
            raise


def _promote_run_status_db(config: dict) -> None:
    db_cfg = config.get("run_status_db", {})
    if not db_cfg.get("enabled", False) or not db_cfg.get("promote_to_juno", False):
        return
    try:
        plan = build_db_promotion_transfer_plan(config)
        execute_transfer(config, plan)
    except Exception as exc:
        log.warning("Could not promote run-status DB to JUNO: %s", exc)


def _safe_upsert_run_status(
    db: AsfmRunStatusDb,
    run: AutoSfmRun,
    status: str,
    message: str | None = None,
) -> None:
    try:
        db.upsert_run_status(run, status, message=message)
    except Exception as exc:
        log.warning("Could not update autosfm run status in SQLite: %s", exc)


def write_manifest(run: AutoSfmRun, status: str, message: str | None = None) -> None:
    data = run.to_manifest_dict()
    data["status"] = status
    data["image_count"] = len(run.image_records)
    data["source_image_paths"] = [str(record.source_path) for record in run.image_records]
    if message:
        data["message"] = message
    write_yaml(run.paths.manifest_path, data)