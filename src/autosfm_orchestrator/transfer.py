from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from autosfm_orchestrator.models import AutoSfmRun, TransferItem, TransferPlan

log = logging.getLogger(__name__)

_TASK_ID_RE = re.compile(r"Task ID:\s*([0-9a-fA-F-]{36})")


class TransferError(RuntimeError):
    pass


def build_input_transfer_plan(config: dict, run: AutoSfmRun) -> list[TransferPlan]:
    """Build one or more transfer plans to stage inputs onto NCSU/SUNNY.

    Returns a list because the GCP reference file may be indexed under a
    different site than the batch images (e.g. images on CERES, reference
    only on NCSU or JUNO so far). Each plan targets a single source endpoint.
    """
    globus_cfg = config["globus"]
    ceres_endpoint = globus_cfg["ceres_endpoint"]
    ncsu_endpoint = globus_cfg["ncsu_endpoint"]
    label_prefix = globus_cfg.get("label_prefix", "autosfm")

    image_items = [
        TransferItem(
            source=record.source_path,
            destination=run.paths.images_dir / record.filename,
        )
        for record in run.image_records
    ]
    plans = [
        TransferPlan(
            label=f"{label_prefix}-stage-{run.run_id}-images",
            source_endpoint=ceres_endpoint,
            destination_endpoint=ncsu_endpoint,
            items=image_items,
        )
    ]

    if run.source_reference_path is not None:
        reference_item = TransferItem(
            source=run.source_reference_path,
            destination=run.paths.reference_dir / run.source_reference_path.name,
        )
        # reference_record is None when the DB lookup found nothing and we fell
        # back to the guessed config path (paths.ceres_reference_root) -- in
        # that case assume CERES, matching prior behavior.
        reference_endpoint = run.reference_record.endpoint if run.reference_record else ceres_endpoint

        if reference_endpoint == ceres_endpoint:
            # Same source endpoint as the images -- ride along on that plan.
            plans[0].items.append(reference_item)
        elif reference_endpoint == ncsu_endpoint:
            # Reference already lives on the NCSU/SUNNY side -- no Globus
            # transfer needed, just a same-endpoint filesystem copy.
            plans.append(
                TransferPlan(
                    label=f"{label_prefix}-stage-{run.run_id}-reference-local",
                    source_endpoint=ncsu_endpoint,
                    destination_endpoint=ncsu_endpoint,
                    items=[reference_item],
                )
            )
        else:
            # Reference lives on some other indexed site (e.g. JUNO). Give it
            # its own real Globus transfer plan from that endpoint.
            plans.append(
                TransferPlan(
                    label=f"{label_prefix}-stage-{run.run_id}-reference",
                    source_endpoint=reference_endpoint,
                    destination_endpoint=ncsu_endpoint,
                    items=[reference_item],
                )
            )

    return plans


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
        if str(item.destination).startswith("/90daydata/"):
            return False

    return True


def _local_copy_when_possible(plan: TransferPlan) -> None:
    """Direct filesystem copy -- used for dry-run-local-visible tests and for
    same-endpoint plans where a Globus transfer would be pointless."""
    copied = 0
    for item in plan.items:
        if not item.source.exists():
            log.warning("Skipping copy, source not found: %s", item.source)
            continue
        item.destination.parent.mkdir(parents=True, exist_ok=True)
        if item.source.is_dir():
            shutil.copytree(item.source, item.destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item.source, item.destination)
        copied += 1
    if copied:
        log.info("Local copy completed for %s/%s items (%s)", copied, len(plan.items), plan.label)


# ---------------------------------------------------------------------------
# Local filesystem path -> Globus collection path translation
#
# A path that's locally mounted (e.g. via autofs/NFS) is not necessarily the
# same path the Globus Connect Server collection exposes for that same file.
# Confirmed on SUNNY:
#   local:  /mnt/research-projects/s/screberg/longterm_images2/autosfm_staging
#   globus: /rsstu/users/s/screberg/longterm_images2/autosfm_staging
#
# paths.sunny_staging_root / paths.sunny_staging_globus_root in config define
# this single fixed mapping. Paths that don't start with sunny_staging_root
# (e.g. CERES /90daydata paths, or reference sources pulled straight from
# globus_file_index.full_path) pass through unchanged.
# ---------------------------------------------------------------------------

def to_globus_path(path: Path, config: dict) -> str:
    """Translate a local filesystem path to the path Globus should use, if needed."""
    paths_cfg = config.get("paths", {})
    local_root = paths_cfg.get("sunny_staging_root")
    globus_root = paths_cfg.get("sunny_staging_globus_root")
    path_str = str(path)

    if local_root and globus_root:
        local_root = str(local_root).rstrip("/")
        globus_root = str(globus_root).rstrip("/")
        if path_str == local_root or path_str.startswith(local_root + "/"):
            return globus_root + path_str[len(local_root):]

    return path_str


def _parse_task_id(cli_output: str) -> str | None:
    match = _TASK_ID_RE.search(cli_output)
    return match.group(1) if match else None


def _execute_globus_cli(config: dict, plan: TransferPlan) -> str:
    cli = config["globus"].get("cli", "globus")
    batch_lines = "\n".join(
        f"{to_globus_path(item.source, config)} {to_globus_path(item.destination, config)}"
        for item in plan.items
    )
    command = [
        cli,
        "transfer",
        plan.source_endpoint,
        plan.destination_endpoint,
        "--label",
        plan.label,
        "--batch",
        "-",  # read batch lines from stdin
    ]
    log.info("Submitting Globus transfer: %s (%s items)", plan.label, len(plan.items))
    for item in plan.items[:5]:
        log.debug(
            "  %s -> %s",
            to_globus_path(item.source, config),
            to_globus_path(item.destination, config),
        )
    result = subprocess.run(command, input=batch_lines, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise TransferError(f"Globus transfer submission failed: {result.stderr.strip()}")

    task_id = _parse_task_id(result.stdout)
    if task_id is None:
        raise TransferError(
            f"Globus transfer submitted but no Task ID could be parsed from output: {result.stdout.strip()}"
        )
    log.info("Globus transfer submitted: task_id=%s", task_id)
    return task_id


def _wait_for_task(config: dict, task_id: str) -> None:
    """Block until the Globus task finishes, using the CLI's own blocking wait."""
    cli = config["globus"].get("cli", "globus")
    poll_seconds = config["globus"].get("poll_seconds", 30)
    timeout_seconds = config["globus"].get("timeout_seconds")

    command = [cli, "task", "wait", task_id, "--polling-interval", str(poll_seconds)]
    if timeout_seconds:
        command += ["--timeout", str(timeout_seconds)]

    log.info("Waiting for Globus task %s (polling every %ss)", task_id, poll_seconds)
    result = subprocess.run(command, text=True, capture_output=True, check=False)

    if result.returncode != 0:
        status = _task_status(config, task_id)
        raise TransferError(
            f"Globus task {task_id} did not complete successfully (status={status}). "
            f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
        )
    log.info("Globus task %s completed successfully", task_id)


def _task_status(config: dict, task_id: str) -> str:
    cli = config["globus"].get("cli", "globus")
    try:
        result = subprocess.run(
            [cli, "task", "show", task_id, "--jmespath", "status", "--format", "unix"],
            text=True,
            capture_output=True,
            check=False,
        )
        return result.stdout.strip() or "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def execute_transfer(config: dict, plan: TransferPlan) -> None:
    transfer_cfg = config.get("globus", {})
    dry_run = bool(transfer_cfg.get("dry_run", True))

    if not plan.items:
        log.info("Skipping empty transfer plan: %s", plan.label)
        return

    if plan.source_endpoint == plan.destination_endpoint:
        # Same Globus endpoint on both sides (e.g. reference file already on
        # NCSU/SUNNY) -- no Globus transfer needed, just a filesystem copy.
        if dry_run:
            log.info("Same-endpoint dry run: %s (%s items)", plan.label, len(plan.items))
            for item in plan.items:
                log.info("  %s -> %s", item.source, item.destination)
            return
        _local_copy_when_possible(plan)
        return

    if dry_run:
        log.info("Transfer dry run: %s (%s items)", plan.label, len(plan.items))
        for item in plan.items[:10]:
            log.info("  %s -> %s", item.source, item.destination)
        if len(plan.items) > 10:
            log.info("  ... %s more items", len(plan.items) - 10)
        return

    if transfer_cfg.get("enabled", False):
        task_id = _execute_globus_cli(config, plan)
        if transfer_cfg.get("wait_for_completion", True):
            _wait_for_task(config, task_id)
        return

    if _can_local_copy(plan):
        _local_copy_when_possible(plan)
        return

    raise NotImplementedError(
        "Globus transfer is not implemented for this plan. "
        "Set globus.dry_run=true, globus.enabled=true, or use locally mounted source/destination paths."
    )