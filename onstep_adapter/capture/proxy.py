"""Transparent TCP pass-through that records what flows through it.

Put it between an INDI client and indiserver (protocol ``indi``) or between
``indi_lx200_OnStep`` and an OnStepX controller reached over TCP (protocol
``lx200``). Every byte is forwarded unchanged and at once; recording happens
afterwards on a separate thread from a copy. The proxy never originates a
byte on either connection: it has no code path that writes anything other
than what the opposite peer just sent.

For capture without anything in the data path, record with ``tcpdump`` and
convert with :mod:`onstep_adapter.capture.pcap`.
"""

from __future__ import annotations

from datetime import datetime, timezone
import itertools
import queue
import socket
import threading
import time

from .stream import CaptureWriter


class CaptureProxy:
    def __init__(
        self,
        writer: CaptureWriter,
        *,
        upstream: tuple[str, int],
        listen: tuple[str, int] = ("127.0.0.1", 7625),
        protocol: str = "indi",
    ) -> None:
        if protocol not in {"indi", "lx200"}:
            raise ValueError("protocol must be indi or lx200")
        self.writer = writer
        self.upstream = upstream
        self.listen = listen
        self.protocol = protocol
        self.port = 0
        self._ids = itertools.count(1)
        self._queue: queue.Queue = queue.Queue()
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._sockets: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._running = False
        self.connections = 0

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> CaptureProxy:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(self.listen)
        listener.listen(8)
        listener.settimeout(0.2)
        self._listener = listener
        self.port = listener.getsockname()[1]
        self._running = True
        self.writer.meta(
            mode="proxy", protocol=self.protocol,
            listen=f"{self.listen[0]}:{self.port}",
            upstream=f"{self.upstream[0]}:{self.upstream[1]}",
        )
        for target, name in ((self._accept_loop, "capture-accept"), (self._record_loop, "capture-record")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self) -> None:
        self._running = False
        if self._listener is not None:
            self._listener.close()
        with self._lock:
            sockets = list(self._sockets)
        for sock in sockets:
            _shutdown(sock)
        self._queue.put(None)
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads.clear()
        self.writer.recorder.close()

    def __enter__(self) -> CaptureProxy:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def serve_forever(self) -> None:
        """Block until interrupted (Ctrl+C); used by the command-line tool."""
        try:
            while self._running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

    # ---------------------------------------------------------------- plumbing
    def _event(self, *item) -> None:
        self._queue.put((time.monotonic(), datetime.now(timezone.utc), *item))

    def _accept_loop(self) -> None:
        while self._running:
            try:
                client, peer = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                server = socket.create_connection(self.upstream, timeout=5.0)
            except OSError:
                client.close()
                continue
            for sock in (client, server):
                sock.settimeout(None)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn = next(self._ids)
            self.connections += 1
            with self._lock:
                self._sockets.update((client, server))
            self._event("connect", conn, f"{peer[0]}:{peer[1]}")
            for source, sink, direction in ((client, server, "c2s"), (server, client, "s2c")):
                threading.Thread(
                    target=self._pump, args=(conn, source, sink, direction),
                    name=f"capture-{conn}-{direction}", daemon=True,
                ).start()

    def _pump(self, conn: int, source: socket.socket, sink: socket.socket, direction: str) -> None:
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                sink.sendall(data)  # forward first: recording adds no latency
                self._event("data", conn, direction, data)
        except OSError:
            pass
        finally:
            # One side closed: close both so neither peer waits on a half link.
            for sock in (source, sink):
                _shutdown(sock)
                with self._lock:
                    self._sockets.discard(sock)
            if direction == "c2s":
                self._event("close", conn)

    def _record_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            at, at_utc, kind, conn, *rest = item
            try:
                if kind == "connect":
                    self.writer.connect(conn, self.protocol, rest[0], at=at, at_utc=at_utc)
                elif kind == "data":
                    self.writer.data(conn, rest[0], rest[1], at=at, at_utc=at_utc)
                else:
                    self.writer.close(conn, at=at, at_utc=at_utc)
            except Exception:  # recording must never affect forwarding
                self.writer.recorder.write_failures += 1
        # Drain what arrived before stop.
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is not None and item[2] == "data":
                at, at_utc, _, conn, direction, data = item
                try:
                    self.writer.data(conn, direction, data, at=at, at_utc=at_utc)
                except Exception:
                    self.writer.recorder.write_failures += 1


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
