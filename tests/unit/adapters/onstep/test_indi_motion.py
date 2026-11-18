import threading
import time

import pytest

from onstep_adapter.indi_motion import MotionHandle, MotionStopped


def start(run, stops=None):
    stops = stops if stops is not None else []

    def stop(*, timeout):
        stops.append(timeout)
        return "stop-result"

    return MotionHandle("test", run, stop=stop)._start(), stops


def test_handle_returns_result_from_wait():
    handle, _ = start(lambda token: "arrived")
    assert handle.wait(timeout=1) == "arrived"
    assert handle.state == "succeeded" and handle.progress().stage == "done"


def test_wait_with_timeout_leaves_the_move_running():
    release = threading.Event()
    handle, _ = start(lambda token: release.wait(2) and "late")
    with pytest.raises(TimeoutError, match="still running"):
        handle.wait(timeout=0.05)
    assert not handle.done and handle.progress().state == "running"
    release.set()
    assert handle.wait(timeout=1) == "late"


def test_stop_wakes_the_worker_and_wait_raises_motion_stopped():
    def run(token):
        token.report("moving", fraction=0.25)
        while True:
            token.sleep(0.05)

    handle, stops = start(run)
    time.sleep(0.05)
    assert handle.progress().fraction == 0.25
    started = time.monotonic()
    assert handle.stop(timeout=2.0) == "stop-result"
    with pytest.raises(MotionStopped) as raised:
        handle.wait(timeout=1)
    assert time.monotonic() - started < 0.5
    assert raised.value.stop_result == "stop-result"
    assert stops == [2.0] and handle.state == "stopped"


def test_move_error_is_reraised_by_wait():
    def run(token):
        raise RuntimeError("overran target")

    handle, _ = start(run)
    with pytest.raises(RuntimeError, match="overran"):
        handle.wait(timeout=1)
    assert handle.state == "failed"
    assert handle.progress().error == "overran target"
