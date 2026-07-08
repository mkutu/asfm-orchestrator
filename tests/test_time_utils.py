from autosfm_orchestrator.utils import normalize_time_str, time_window_to_epoch


def test_normalize_time_str():
    assert normalize_time_str("1:02 PM") == "13:02:00"
    assert normalize_time_str("13:02:03") == "13:02:03"


def test_time_window_to_epoch_rollover():
    start, end = time_window_to_epoch("NC_2026-06-04", "23:59:00", "00:01:00")
    assert end > start
