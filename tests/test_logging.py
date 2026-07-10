import logging

from autosfm_orchestrator.utils import log_to_file

log = logging.getLogger("autosfm_orchestrator.test_logging")


def test_log_to_file_writes_records(tmp_path):
    log_path = tmp_path / "logs" / "run.log"

    with log_to_file(log_path):
        log.info("hello from the run")

    content = log_path.read_text()
    assert "hello from the run" in content


def test_log_to_file_detaches_handler_on_exit(tmp_path):
    log_path = tmp_path / "logs" / "run.log"

    with log_to_file(log_path):
        log.info("inside the block")

    log.info("after the block")

    content = log_path.read_text()
    assert "inside the block" in content
    assert "after the block" not in content


def test_log_to_file_creates_parent_dirs(tmp_path):
    log_path = tmp_path / "nested" / "logs" / "run.log"

    with log_to_file(log_path):
        log.info("nested dirs")

    assert log_path.exists()


def test_log_to_file_writes_to_multiple_paths(tmp_path):
    nfs_path = tmp_path / "nfs" / "run.log"
    local_path = tmp_path / "local" / "run.log"

    with log_to_file([nfs_path, local_path]):
        log.info("written to both")

    for path in (nfs_path, local_path):
        assert "written to both" in path.read_text()
