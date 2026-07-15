from __future__ import annotations

import argparse
import logging
from pathlib import Path

from autosfm_orchestrator.config import load_config
from autosfm_orchestrator.db import AsfmRunStatusDb, InventoryDb
from autosfm_orchestrator.utils import configure_logging
from autosfm_orchestrator.workflow import plan_log, plan_one, run_log, run_one

log = logging.getLogger(__name__)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.log_level)
    config = load_config(args.config)

    if args.command == "plan-one":
        run = plan_one(config, args.batch_id, args.start_time, args.end_time, args.sub_batch_id, season=args.season)
        print(run.paths.manifest_path)
    elif args.command == "run-one":
        run = run_one(config, args.batch_id, args.start_time, args.end_time, args.sub_batch_id, season=args.season)
        print(run.paths.manifest_path)
    elif args.command == "plan-log":
        runs = plan_log(config, args.manual_log)
        for run in runs:
            print(run.paths.manifest_path)
    elif args.command == "run-log":
        runs = run_log(config, args.manual_log)
        for run in runs:
            print(run.paths.manifest_path)
    elif args.command == "summarize-batch":
        db = InventoryDb(config["database"]["path"], config=config)
        rows = db.summarize_batch(args.batch_id)
        for row in rows:
            print(row)
    elif args.command == "refresh-ready":
        inventory_db = InventoryDb(config["database"]["path"], config=config)
        rows = inventory_db.compute_batches_ready_for_asfm()
        run_status_db = AsfmRunStatusDb(_run_status_db_path(config), config=config)
        run_status_db.refresh_batches_ready_for_asfm(rows)
        print(f"Refreshed batches_ready_for_asfm with {len(rows)} rows ({len({r['batch_id'] for r in rows})} batches).")
    elif args.command == "list-ready":
        run_status_db = AsfmRunStatusDb(_run_status_db_path(config), config=config)
        rows = run_status_db.list_ready_batches(
            site=args.site, start_date=args.start_date, end_date=args.end_date
        )
        if not rows:
            print("No batches ready for AutoSfM.")
        for row in rows:
            legacy_flag = "LEGACY_REFERENCE " if row["has_legacy_reference"] else ""
            print(
                f"{row['batch_id']:20s} site={row['site']:4s} date={row['batch_date']} "
                f"jpg_count={row['jpg_count']:<6d} storage_site={row['storage_site']:6s} "
                f"storage_domain={row['storage_domain']:12s} namespace={row['namespace']:10s} "
                f"storage_root={row['storage_root']} {legacy_flag}"
            )
    else:
        parser.error(f"Unknown command: {args.command}")


def _run_status_db_path(config: dict) -> str:
    return config.get("run_status_db", {}).get(
        "db_path", "/mnt/research-projects/s/screberg/longterm_images2/semifield-asfm/db/autosfm_run_status.sqlite3"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autosfm-orchestrator")
    parser.add_argument("--config", type=Path, default=Path("conf/default.yaml"))
    parser.add_argument("--log-level", default="INFO")

    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ["plan-one", "run-one"]:
        sub = subparsers.add_parser(command)
        sub.add_argument("--batch-id", required=True)
        sub.add_argument("--start-time", required=True)
        sub.add_argument("--end-time", required=True)
        sub.add_argument("--sub-batch-id")
        sub.add_argument("--season", help="Optional season folder for GroundControlPoints lookup")

    for command in ["plan-log", "run-log"]:
        sub = subparsers.add_parser(command)
        sub.add_argument("--manual-log", type=Path, required=True)

    summary = subparsers.add_parser("summarize-batch")
    summary.add_argument("--batch-id", required=True)

    subparsers.add_parser(
        "refresh-ready",
        help="Recompute the batches_ready_for_asfm snapshot table in the run-status DB from globus_file_index.sqlite3",
    )

    ready = subparsers.add_parser(
        "list-ready",
        help="List batches with developed images that don't yet have AutoSfM output at the CERES destination "
        "(reads the batches_ready_for_asfm snapshot - run refresh-ready first)",
    )
    ready.add_argument("--site", help="Filter by field site code (e.g. NC, MD, TX)")
    ready.add_argument("--start-date", help="Only include batches on/after this date (YYYY-MM-DD)")
    ready.add_argument("--end-date", help="Only include batches on/before this date (YYYY-MM-DD)")

    return parser


if __name__ == "__main__":
    main()
