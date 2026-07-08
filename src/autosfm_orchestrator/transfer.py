from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from autosfm_orchestrator.models import AutoSfmRun, TransferItem, TransferPlan

log = logging.getLogger(__name__)


class TransferError(RuntimeError):
    pass


def build_input_transfer_plan(config: dict, run: AutoSfmRun) -> TransferPlan:
    items = [
        TransferItem(
            source=record.source_path,
            destination=run.paths.images_dir / record.filename,
        )
        for record in run.image_records
    ]

    if run.source_reference_path is not None:
        items.append(
            TransferItem(
                source=run.source_reference_path,
                destination=run.paths.reference_dir / run.source_reference_path.name,
            )
        )

    return TransferPlan(
        label=f"{config['globus'].get('label_prefix', 'autosfm')}-stage-{run.run_id}",
        source_endpoint=config["globus"]["ceres_endpoint"],
        destination_endpoint=config["globus"]["ncsu_endpoint"],
        items=items,
    )


def build_output_transfer_plan(config: dict, run: AutoSfmRun) -> TransferPlan:
    items = []
    for source in [run.paths.autosfm_dir, run.paths.logs_dir, run.paths.manifest_path]:
        if source.exists():
            items.append(
                TransferItem(
                    source=source,
                    destination=run.ceres_output_path / source.name,
                )
            )

    return TransferPlan(
        label=f"{config['globus'].get('label_prefix', 'autosfm')}-promote-{run.run_id}",
        source_endpoint=config["globus"]["ncsu_endpoint"],
        destination_endpoint=config["globus"]["ceres_endpoint"],
        items=items,
    )

def _can_local_copy(plan: TransferPlan) -> bool:
    """Return True only when all sources and destinations are locally reachable."""
    if not plan.items:
        return False

    for item in plan.items:
        if not item.source.exists():
            return False

        # Important: do not treat remote CERES paths as local.
        # On SUNNY, /90daydata is usually not mounted.
        destination_anchor = item.destination.anchor
        if str(item.destination).startswith("/90daydata/"):
            return False

    return True

def execute_transfer(config: dict, plan: TransferPlan) -> None:
    transfer_cfg = config.get("globus", {})
    dry_run = bool(transfer_cfg.get("dry_run", True))

    if dry_run:
        log.info("Transfer dry run: %s (%s items)", plan.label, len(plan.items))
        for item in plan.items[:10]:
            log.info("  %s -> %s", item.source, item.destination)
        if len(plan.items) > 10:
            log.info("  ... %s more items", len(plan.items) - 10)
        return

    if _can_local_copy(plan):
        _local_copy_when_possible(plan)
        return

    raise NotImplementedError(
        "Globus transfer is not implemented yet. "
        "Set globus.dry_run=true or use locally mounted source/destination paths."
    )


def _local_copy_when_possible(plan: TransferPlan) -> None:
    """Helpful for SUNNY/NFS tests when source paths are already locally visible."""
    copied = 0
    for item in plan.items:
        if not item.source.exists():
            continue
        item.destination.parent.mkdir(parents=True, exist_ok=True)
        if item.source.is_dir():
            shutil.copytree(item.source, item.destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item.source, item.destination)
        copied += 1
    if copied:
        log.info("Dry-run local copy completed for %s visible items", copied)


def _execute_globus_cli(config: dict, plan: TransferPlan) -> None:
    cli = config["globus"].get("cli", "globus")
    batch_lines = "\n".join(f"{item.source} {item.destination}" for item in plan.items)
    command = [
        cli,
        "transfer",
        plan.source_endpoint,
        plan.destination_endpoint,
        "--batch",
        "--label",
        plan.label,
    ]
    log.info("Submitting Globus transfer: %s", plan.label)
    result = subprocess.run(command, input=batch_lines, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise TransferError(f"Globus transfer failed: {result.stderr}")
    log.info("Globus transfer submitted: %s", result.stdout.strip())
