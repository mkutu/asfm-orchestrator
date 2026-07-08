from __future__ import annotations

import argparse
import logging
from pathlib import Path

from autosfm_orchestrator.config import load_config
from autosfm_orchestrator.db import InventoryDb
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
    else:
        parser.error(f"Unknown command: {args.command}")


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

    return parser


if __name__ == "__main__":
    main()
