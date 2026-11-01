"""Non-blocking mount movements: start, observe, wait for or stop a move.

``park()``, ``go_home()`` and the finite axis moves return a
:class:`MotionHandle` at once. The move runs in a worker thread, so the thread
that started it is always free to call :meth:`MotionHandle.stop` (or the
mount's ``stop()``) while the mount is still slewing.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Callable

MOTION_RUNNING = "running"
MOTION_SUCCEEDED = "succeeded"
MOTION_FAILED = "failed"
MOTION_STOPPED = "stopped"

# Longest time a worker waits before it looks at its stop request again.
STOP_CHECK_INTERVAL_S = 0.1


class MotionStopped(Exception):
    """Raised by :meth:`MotionHandle.wait` when the move was stopped early.

    A move is stopped by :meth:`MotionHandle.stop`, by the mount's ``stop()``
    or by the meridian supervisor. ``stop_result`` is the verified
    :class:`~onstep_adapter.indi_stop.IndiStopResult`, when one is known.
    """

    def __init__(self, kind: str, stop_result: Any = None) -> None:
        super().__init__(f"{kind} move was stopped before it finished")
        self.kind = kind
        self.stop_result = stop_result


class MotionToken:
    """What a worker sees of its handle: stop requests and progress reports."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.stage = "starting"
        self.fraction: float | None = None
        self.last_raw_status: str | None = None
        self.kind = "move"

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def check(self) -> None:
        """Raise :class:`MotionStopped` once a stop was requested."""
        if self._stop.is_set():
            raise MotionStopped(self.kind)

    def sleep(self, seconds: float) -> None:
        """Sleep, but wake (and raise) as soon as a stop is requested."""
        if self._stop.wait(max(0.0, seconds)):
            raise MotionStopped(self.kind)

    def report(
        self, stage: str | None = None, *, fraction: float | None = None,
        raw_status: str | None = None,
    ) -> None:
        with self._lock:
            if stage is not None:
                self.stage = stage
            if fraction is not None:
                self.fraction = max(0.0, min(1.0, fraction))
            if raw_status is not None:
                self.last_raw_status = raw_status


class _NoStopToken(MotionToken):
    """Token for direct (blocking) router and mover calls: never stops."""

    def report(self, *args: Any, **kwargs: Any) -> None:
        return None


NO_STOP = _NoStopToken()


@dataclass(frozen=True)
class MotionProgress:
    """A point-in-time view of one move.

    ``fraction`` is 0..1 for axis moves (measured angle over requested angle)
    and ``None`` for PARK and HOME, where OnStep reports no distance to go.
    """

    kind: str
    state: str
    stage: str
    elapsed_s: float
    fraction: float | None
    last_raw_status: str | None
    error: str | None


class MotionHandle:
    """A running mount move. Returned at once by park, go_home and axis moves.

    * :meth:`stop` sends ABORT and tracking off immediately and ends the move.
    * :meth:`progress` returns a :class:`MotionProgress` without blocking.
    * :meth:`wait` blocks the caller (not the mount) until the move ends and
      returns the move's result; it raises :class:`MotionStopped` if the move
      was stopped and re-raises any error the move ended with.
    """

    def __init__(
        self,
        kind: str,
        run: Callable[[MotionToken], Any],
        *,
        stop: Callable[..., Any],
        on_finish: Callable[[MotionHandle], None] | None = None,
    ) -> None:
        self.kind = kind
        self._run = run
        self._stop = stop
        self._on_finish = on_finish
        self._token = MotionToken()
        self._token.kind = kind
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._state = MOTION_RUNNING
        self._result: Any = None
        self._error: BaseException | None = None
        self._stop_result: Any = None
        self._started = time.monotonic()
        self._finished: float | None = None
        self._thread = threading.Thread(
            target=self._work, name=f"onstep-motion-{kind}", daemon=True
        )

    def _start(self) -> MotionHandle:
        self._started = time.monotonic()
        self._thread.start()
        return self

    def _work(self) -> None:
        state, result, error = MOTION_FAILED, None, None
        try:
            result = self._run(self._token)
            state = MOTION_SUCCEEDED
        except MotionStopped as exc:
            state, error = MOTION_STOPPED, exc
        except BaseException as exc:  # reported through wait()
            state, error = MOTION_FAILED, exc
        with self._lock:
            if isinstance(error, MotionStopped) and error.stop_result is None:
                error.stop_result = self._stop_result
            self._state, self._result, self._error = state, result, error
            self._finished = time.monotonic()
        self._done.set()
        if self._on_finish is not None:
            try:
                self._on_finish(self)
            except Exception:
                pass

    # -- called by the client when any stop path runs -----------------------
    def _request_stop(self) -> None:
        self._token.request_stop()

    def _record_stop(self, stop_result: Any) -> None:
        with self._lock:
            self._stop_result = stop_result
            if isinstance(self._error, MotionStopped) and self._error.stop_result is None:
                self._error.stop_result = stop_result

    # -- public API ---------------------------------------------------------
    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def stop_result(self) -> Any:
        with self._lock:
            return self._stop_result

    def progress(self) -> MotionProgress:
        with self._lock:
            end = self._finished if self._finished is not None else time.monotonic()
            error = self._error
            state = self._state
        token = self._token
        with token._lock:
            stage, fraction, raw = token.stage, token.fraction, token.last_raw_status
        if state == MOTION_SUCCEEDED:
            stage = "done"
            if fraction is not None:
                fraction = 1.0
        elif state == MOTION_STOPPED:
            stage = "stopped"
        return MotionProgress(
            kind=self.kind, state=state, stage=stage,
            elapsed_s=round(end - self._started, 3), fraction=fraction,
            last_raw_status=raw, error=str(error) if error is not None else None,
        )

    def wait(self, timeout: float | None = None) -> Any:
        """Return the move's result once it ends.

        Raises ``TimeoutError`` if the move is still running after
        ``timeout`` seconds; the move itself carries on.
        """
        if not self._done.wait(timeout):
            raise TimeoutError(f"{self.kind} move is still running")
        with self._lock:
            if self._error is not None:
                raise self._error
            return self._result

    def stop(self, *, timeout: float = 5.0) -> Any:
        """Stop this move now and return the verified stop result.

        ABORT and tracking off are sent before anything is awaited, so the
        mount stops even if confirming the stop takes a while. Stopping a move
        that already ended still sends a stop to the mount.
        """
        self._token.request_stop()
        result = self._stop(timeout=timeout)
        self._record_stop(result)
        return result

    def __repr__(self) -> str:
        return f"MotionHandle(kind={self.kind!r}, state={self.state!r})"
