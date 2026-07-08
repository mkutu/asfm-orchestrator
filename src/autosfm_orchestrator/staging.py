from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone

from autosfm_orchestrator.models import AutoSfmRun
from autosfm_orchestrator.paths import create_workspace
from autosfm_orchestrator.utils import write_yaml


class RunLockError(RuntimeError):
    pass


@contextmanager
def locked_workspace(run: AutoSfmRun):
    create_workspace(run.paths)
    _acquire_lock(run)
    try:
        yield
    finally:
        _release_lock(run)


def _acquire_lock(run: AutoSfmRun) -> None:
    lock_path = run.paths.lock_path
    if lock_path.exists():
        raise RunLockError(f"Run appears to be locked already: {lock_path}")

    lock_data = {
        "run_id": run.run_id,
        "batch_id": run.batch_id,
        "sub_batch_id": run.sub_batch_id,
        "hostname": os.uname().nodename,
        "pid": os.getpid(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_yaml(lock_path, lock_data)


def _release_lock(run: AutoSfmRun) -> None:
    try:
        run.paths.lock_path.unlink(missing_ok=True)
    except Exception:
        # Do not mask the original exception if cleanup fails.
        pass
