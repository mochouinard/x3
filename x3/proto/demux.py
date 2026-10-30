"""Frame demultiplexer for a Winbox session.

``ModernSession.request()`` is send-then-recv: it returns whatever frame arrives
next. That works for one caller at a time and breaks the moment two channels
share a connection -- a terminal reader parked on ``recv()`` swallows GET replies,
and a GET swallows terminal output. Real Winbox multiplexes freely (a terminal
open sits in the middle of 1 Hz interface polls in our own captures) because it
routes frames instead of assuming order.

So: one reader thread owns all reads and hands each frame to whoever is waiting
for it.

Routing key is ``(SYS_TO, SYS_REQUEST_ID)``. The device echoes the client's
SYS_FROM back as SYS_TO on every frame it sends, including the terminal's
no-rid data frames, so the path is always present; and a request id is not
globally unique on a real device (the captures show one id live on two paths at
once), so the id alone is not enough.

Nothing is ever dropped silently: a frame that matches no waiter is counted,
handed to the ``on_unrouted`` hook and pushed to a bounded sink so the legacy
``recv()`` shim can still see it.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any, Callable

from . import message as m


class DemuxClosed(Exception):
    """The reader stopped (connection died or the session was closed)."""


def _path_of(msg: Any, code: int) -> tuple[int, ...]:
    try:
        hit = msg.find(code)
    except Exception:  # noqa: BLE001
        return ()
    if hit is None:
        return ()
    value = hit[1]
    if isinstance(value, (list, tuple)):
        return tuple(int(x) for x in value)
    if isinstance(value, int):
        return (int(value),)
    return ()


def _rid_of(msg: Any) -> int | None:
    """Request id, or None when the frame carries none.

    Uses ``find`` rather than ``get_u32``: rid 0 is legal and must not read as
    absent.
    """
    try:
        hit = msg.find(m.SYS_REQUEST_ID)
    except Exception:  # noqa: BLE001
        return None
    if hit is None:
        return None
    return int(hit[1]) if isinstance(hit[1], int) else None


class Pending:
    """One-shot slot for the reply to one request."""

    __slots__ = ("key", "_event", "_msg", "_exc")

    def __init__(self, key: tuple) -> None:
        self.key = key
        self._event = threading.Event()
        self._msg: Any = None
        self._exc: BaseException | None = None

    def set(self, msg: Any) -> None:
        self._msg = msg
        self._event.set()

    def fail(self, exc: BaseException) -> None:
        self._exc = exc
        self._event.set()

    def result(self, timeout: float | None) -> Any:
        if not self._event.wait(timeout):
            raise TimeoutError(f"no reply for {self.key!r} within {timeout}s")
        if self._exc is not None:
            raise self._exc
        return self._msg


class Subscription:
    """Durable queue of the frames addressed to one SYS_FROM path."""

    def __init__(self, key: tuple[int, ...], maxsize: int = 0) -> None:
        self.key = key
        self._items: deque = deque(maxlen=maxsize or None)
        self._cond = threading.Condition()
        self._closed = False
        self._exc: BaseException | None = None
        self.dropped = 0

    @property
    def closed(self) -> bool:
        return self._closed

    def put(self, msg: Any) -> None:
        with self._cond:
            if self._closed:
                return
            if self._items.maxlen is not None and len(self._items) == self._items.maxlen:
                self.dropped += 1
            self._items.append(msg)
            self._cond.notify()

    def get(self, timeout: float | None = None) -> Any | None:
        """Next frame, or None on timeout. Raises once closed and drained."""
        with self._cond:
            if not self._items and not self._closed:
                self._cond.wait(timeout)
            if self._items:
                return self._items.popleft()
            if self._closed:
                raise self._exc or DemuxClosed(f"subscription {self.key!r} closed")
            return None

    def close(self, exc: BaseException | None = None) -> None:
        with self._cond:
            self._closed = True
            self._exc = exc
            self._cond.notify_all()


class FrameDemux:
    """Reader thread plus the routing table it delivers into."""

    def __init__(
        self,
        session: Any,
        *,
        on_unrouted: Callable[[Any], None] | None = None,
        on_reader_error: Callable[[BaseException], None] | None = None,
        sink_max: int = 256,
    ) -> None:
        self._sess = session
        self._on_unrouted = on_unrouted
        self._on_reader_error = on_reader_error
        # Why the reader stopped, if it stopped on its own. Kept so a request
        # made afterwards can say what actually happened instead of waiting
        # out a timeout against a socket nobody is reading.
        self.reader_error: BaseException | None = None
        self._lock = threading.Lock()
        self._pending: dict[tuple, Pending] = {}
        self._subs: dict[tuple[int, ...], Subscription] = {}
        # Keys whose waiter already timed out, so a late reply is reported as
        # late rather than as unroutable.
        self._expired: deque = deque(maxlen=64)
        self._sink: deque = deque(maxlen=sink_max)
        self._sink_cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._debug = bool(os.environ.get("X3_DEMUX_DEBUG"))
        self.frames_in = 0
        self.unrouted = 0
        self.late_replies = 0

    # --- lifecycle ----------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="winbox-demux", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        sock = getattr(self._sess, "_sock", None)
        # Unblock the reader parked in recv(). shutdown() is the clean way; the
        # MAC transport has no shutdown, so fall back to closing it.
        if sock is not None:
            try:
                import socket as _socket

                if hasattr(sock, "shutdown"):
                    sock.shutdown(_socket.SHUT_RDWR)
                elif hasattr(sock, "close"):
                    sock.close()
            except OSError:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._fail_all(DemuxClosed("demux stopped"))

    @property
    def alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                msg = self._sess._recv_message()
                self.frames_in += 1
                self._route(msg)
        except BaseException as e:  # noqa: BLE001
            # Connection death must wake every waiter, not hang them.
            if not self._stop.is_set():
                self.reader_error = e
                # Record it before failing the waiters. With no waiter parked
                # at this instant the exception used to be discarded here, and
                # every later request just timed out against a socket nobody
                # was reading -- a dead session that looked like a slow one.
                if self._on_reader_error is not None:
                    try:
                        self._on_reader_error(e)
                    except Exception:  # noqa: BLE001
                        pass
                self._fail_all(e)
        finally:
            self._stop.set()

    def _fail_all(self, exc: BaseException) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
            subs = list(self._subs.values())
            self._subs.clear()
        for p in pending:
            p.fail(exc)
        for s in subs:
            s.close(exc)
        with self._sink_cond:
            self._sink_cond.notify_all()

    # --- routing ------------------------------------------------------

    def _route(self, msg: Any) -> None:
        to = _path_of(msg, m.SYS_TO)
        rid = _rid_of(msg)

        if rid is not None:
            with self._lock:
                waiter = self._pending.pop((to, rid), None)
                if waiter is None:
                    # Some firmware answers on a path we did not send from.
                    same_rid = [k for k in self._pending if k[1] == rid]
                    if len(same_rid) == 1:
                        waiter = self._pending.pop(same_rid[0])
                        self._trace(msg, to, rid, f"rid-only {same_rid[0]!r}")
                expired = waiter is None and (to, rid) in self._expired
            if waiter is not None:
                self._trace(msg, to, rid, "reply")
                waiter.set(msg)
                return
            if expired:
                self.late_replies += 1
                self._trace(msg, to, rid, "late reply (dropped)")
                return

        with self._lock:
            sub = self._subs.get(to)
            if sub is None and not to:
                # Belt and braces for a frame with no SYS_TO: try SYS_FROM.
                sub = self._subs.get(_path_of(msg, m.SYS_FROM))
        if sub is not None:
            self._trace(msg, to, rid, "subscription")
            sub.put(msg)
            return

        self.unrouted += 1
        self._trace(msg, to, rid, "UNROUTED")
        if self._on_unrouted is not None:
            try:
                self._on_unrouted(msg)
            except Exception:  # noqa: BLE001
                pass
        with self._sink_cond:
            self._sink.append(msg)
            self._sink_cond.notify()

    def _trace(self, msg: Any, to: tuple, rid: int | None, verdict: str) -> None:
        if not self._debug:
            return
        try:
            cmd = msg.get_u32(m.SYS_COMMAND)
        except Exception:  # noqa: BLE001
            cmd = None
        frm = _path_of(msg, m.SYS_FROM)
        print(
            f"[demux] to={list(to)} from={list(frm)} "
            f"cmd={cmd:#x} rid={rid} -> {verdict}"
            if cmd is not None
            else f"[demux] to={list(to)} from={list(frm)} rid={rid} -> {verdict}",
            flush=True,
        )

    # --- write side ---------------------------------------------------

    def send(self, msg: Any) -> None:
        self._sess.send(msg)

    def request(self, msg: Any, *, timeout: float | None = None) -> Any:
        rid = _rid_of(msg)
        if rid is None:
            raise ValueError("request() needs SYS_REQUEST_ID to route the reply")
        # Sending into a session whose reader has stopped can only end in a
        # timeout, and a timeout reads as "the device is slow" when in fact
        # the connection is finished. Say so immediately instead.
        if self._stop.is_set() or not self.alive:
            why = self.reader_error
            raise DemuxClosed(
                f"connection reader stopped: {type(why).__name__}: {why}"
                if why is not None
                else "connection reader stopped"
            )
        key = (_path_of(msg, m.SYS_FROM), rid)
        waiter = Pending(key)
        with self._lock:
            self._pending[key] = waiter
        try:
            self.send(msg)
            if timeout is None:
                timeout = getattr(self._sess, "timeout", None) or 10.0
            return waiter.result(timeout)
        finally:
            with self._lock:
                if self._pending.pop(key, None) is not None:
                    # Timed out or raised: remember the key so a reply that
                    # turns up later is classified, not counted as unroutable.
                    self._expired.append(key)

    def pipeline(
        self, msgs: list[Any], *, timeout: float | None = None
    ) -> list[Any | BaseException]:
        """Send several requests without waiting between them, then collect.

        This is how Winbox itself reads: a property form's dropdown sources go
        out as one burst of ~20 frames and the device answers them in parallel,
        so the whole form costs about one round trip instead of twenty. Sending
        them one at a time -- send, wait, send, wait -- pays every latency in
        sequence, which is what made opening a row take about a second next to
        Winbox (a session capture: 21 requests sent in 6.5 ms, all
        answered by 15.9 ms; the same replies summed serially are 98 ms).

        Replies come back in ``msgs`` order whatever order the wire delivered
        them, since each is routed by its own (SYS_FROM, request id). A request
        that failed or timed out yields its exception in place rather than
        sinking the whole batch.
        """
        if not msgs:
            return []
        if self._stop.is_set() or not self.alive:
            why = self.reader_error
            raise DemuxClosed(
                f"connection reader stopped: {type(why).__name__}: {why}"
                if why is not None
                else "connection reader stopped"
            )
        if timeout is None:
            timeout = getattr(self._sess, "timeout", None) or 10.0

        waiters: list[tuple[tuple, Pending] | None] = []
        out: list[Any | BaseException] = [None] * len(msgs)
        try:
            for i, msg in enumerate(msgs):
                rid = _rid_of(msg)
                if rid is None:
                    out[i] = ValueError(
                        "pipeline() needs SYS_REQUEST_ID to route the reply"
                    )
                    waiters.append(None)
                    continue
                key = (_path_of(msg, m.SYS_FROM), rid)
                waiter = Pending(key)
                with self._lock:
                    self._pending[key] = waiter
                waiters.append((key, waiter))
                try:
                    self.send(msg)
                except BaseException as e:  # noqa: BLE001
                    # The socket is shared: a failed send means the rest of the
                    # batch cannot go out either. Stop, and let the waiters
                    # already registered report the same failure.
                    out[i] = e
                    break
            # Every frame is on the wire; now wait. The deadline is shared, so a
            # slow first reply does not multiply the batch's timeout.
            deadline = time.monotonic() + timeout
            for i, slot in enumerate(waiters):
                if slot is None or isinstance(out[i], BaseException):
                    continue
                _key, waiter = slot
                try:
                    out[i] = waiter.result(max(0.0, deadline - time.monotonic()))
                except BaseException as e:  # noqa: BLE001
                    out[i] = e
        finally:
            with self._lock:
                for slot in waiters:
                    if slot is None:
                        continue
                    key, _w = slot
                    if self._pending.pop(key, None) is not None:
                        self._expired.append(key)
        return out

    # --- read side ----------------------------------------------------

    def subscribe(
        self, from_path: tuple[int, ...], *, maxsize: int = 0
    ) -> Subscription:
        sub = Subscription(tuple(from_path), maxsize=maxsize)
        with self._lock:
            self._subs[tuple(from_path)] = sub
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if self._subs.get(sub.key) is sub:
                del self._subs[sub.key]
        sub.close()

    def recv_any(self, timeout: float | None = None) -> Any:
        """Next unroutable frame. The shim behind the legacy ``recv()``."""
        with self._sink_cond:
            if not self._sink:
                self._sink_cond.wait(timeout)
            if self._sink:
                return self._sink.popleft()
        if self._stop.is_set():
            raise DemuxClosed("demux stopped")
        raise TimeoutError(f"no frame within {timeout}s")

    def stats(self) -> dict[str, int]:
        return {
            "frames_in": self.frames_in,
            "unrouted": self.unrouted,
            "late_replies": self.late_replies,
        }
