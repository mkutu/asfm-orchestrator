from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from autosfm_orchestrator.autosfm import AutoSfmRunner
from autosfm_orchestrator.db import InventoryDb, ensure_records_found
from autosfm_orchestrator.manual_log import read_manual_log
from autosfm_orchestrator.models import AutoSfmRun, ManualLogEntry
from autosfm_orchestrator.paths import build_run, create_workspace
from autosfm_orchestrator.staging import locked_workspace
from autosfm_orchestrator.transfer import (
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
    db = InventoryDb(config["database"]["path"], config=config)
    with locked_workspace(run):
        try:
            _safe_upsert_run_status(db, run, "running")
            write_manifest(run, status="running")

            input_plans = build_input_transfer_plan(config, run)
            for plan in input_plans:
                execute_transfer(config, plan)

            outputs = AutoSfmRunner(config).run(run)
            log.info("AutoSfM outputs: %s", asdict(outputs))

            write_manifest(run, status="autosfm_complete")
            output_plan = build_output_transfer_plan(config, run)
            execute_transfer(config, output_plan)

            run.status = "success"
            write_manifest(run, status="success")
            _safe_upsert_run_status(db, run, "success")
            return run
        except Exception as exc:
            run.status = "failed"
            write_manifest(run, status="failed", message=str(exc))
            _safe_upsert_run_status(db, run, "failed", message=str(exc))
            raise


def _safe_upsert_run_status(
    db: InventoryDb,
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
