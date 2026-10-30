"""Native Safe Mode: the binary handler ``[17]``.

The Winbox Safe Mode button is a handler, not the terminal. Captured on
firmware 7.23.1 (a wireless router, decrypted on the wire) driving
take → add an address → undo → redo → undo → release; every step landed on a
bare one-element address ``to=[17]``.

Commands carry the ``0x80000`` "handler command" bit; the low bits are the verb::

    0x80003  take     enter safe mode; the device assigns a session id
    0x80001  undo     step back one change (this is what *discards*)
    0x80002  redo     step forward one change
    0x80005  release  exit, keeping whatever the buffer currently reflects

The change itself goes over the ordinary write path -- safe mode only makes it
reversible. There is no keep-vs-discard exit verb: to discard everything, undo
down to empty and then release. The router also rolls the whole buffer back on
its own if the session drops, which is server-side and needs no client verb.

This replaces an earlier terminal-driven implementation that typed
``/safe-mode/*`` into handler 76. That approach screen-scraped ANSI, collided
with the interactive console ("Safe Mode is taken by current user in another
session"), offered no structured undo/redo, and -- on a device sitting at
the router's "Change your password" prompt -- submitted its CLI commands as the
new password.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import message as m

#: Handler address. One element, unlike most menus.
SAFE_MODE_PATH = (17,)

#: Marks a handler-local command rather than one of the global 0xFE00xx verbs.
CMD_BIT = 0x80000

CMD_UNDO = CMD_BIT | 1
CMD_REDO = CMD_BIT | 2
CMD_TAKE = CMD_BIT | 3
CMD_RELEASE = CMD_BIT | 5

#: The safe-mode session id: assigned by the device on take, echoed on release.
SESSION_ID = 0xFE0001

#: How many undo steps :meth:`SafeModeChannel.unroll` will take before giving
#: up. A safe-mode buffer is a hand-made edit session, not a script run, so a
#: run this long means undo has stopped making progress.
MAX_UNROLL_STEPS = 500


class SafeModeError(Exception):
    pass


@dataclass
class SafeModeStatus:
    """What this session knows about Safe Mode.

    The handler exposes no "get" verb -- take/undo/redo/release is the whole
    vocabulary -- so this is what *we* hold, not a device reading. ``owner``
    and ``user`` are kept for the UI's sake and stay empty on this path.
    """

    enabled: bool = False
    current: bool = False
    owner: str = ""
    user: str = ""
    raw: str = ""
    session_id: int | None = None

    @property
    def active_here(self) -> bool:
        """True when this session holds Safe Mode."""
        return bool(self.enabled and self.current)


class SafeModeChannel:
    """Drives Safe Mode over handler ``[17]``.

    ``request(path, command, extra=None)`` sends one M2 request and returns the
    reply -- normally ``DeviceClient._request``, so safe-mode traffic lands in
    the session transcript like everything else.
    """

    def __init__(
        self,
        request: Callable[..., Any],
        *,
        from_path: tuple[int, ...] | None = None,
    ) -> None:
        self._request = request
        self.from_path = from_path
        #: Assigned by the device on take; echoed back on release.
        self.session_id: int | None = None
        #: Net undo depth, so unroll() knows when it has wound back to empty.
        #: The handler reports no buffer length, so this is counted client side.
        self._depth = 0

    # --- plumbing -------------------------------------------------------

    def _send(self, command: int, session_id: int | None = None) -> Any:
        def add_fields(msg: m.Message) -> None:
            if session_id is not None:
                msg.add_u32(SESSION_ID, int(session_id) & 0xFFFFFFFF)

        return self._request(
            SAFE_MODE_PATH,
            command,
            from_path=self.from_path,
            extra=add_fields if session_id is not None else None,
        )

    def status(self) -> SafeModeStatus:
        held = self.session_id is not None
        return SafeModeStatus(
            enabled=held, current=held, session_id=self.session_id
        )

    # --- verbs ----------------------------------------------------------

    def take(self) -> SafeModeStatus:
        """Enter Safe Mode. The device assigns the session id; we send none."""
        if self.session_id is not None:
            return self.status()
        reply = self._send(CMD_TAKE)
        sid = reply.get_u32(SESSION_ID)
        if sid is None:
            # Take is the one verb whose reply carries something we need. No
            # id means we cannot release cleanly later, so do not report a
            # safe mode we are not actually holding.
            raise SafeModeError(
                "safe mode take returned no session id — not entering"
            )
        self.session_id = int(sid)
        self._depth = 0
        return self.status()

    def undo(self) -> None:
        """Step back one change. This is what discards."""
        if self.session_id is None:
            raise SafeModeError("not in safe mode")
        self._send(CMD_UNDO)
        self._depth -= 1

    def redo(self) -> None:
        """Step forward one change, reapplying what undo took back."""
        if self.session_id is None:
            raise SafeModeError("not in safe mode")
        self._send(CMD_REDO)
        self._depth += 1

    @property
    def pending(self) -> int:
        """Changes this session wrote under Safe Mode and has not undone.

        Counted here, since the handler never says: every config write path
        reports itself (see DeviceClient._note_safe_mode_change). Zero means
        there is nothing for keep-or-unroll to decide.
        """
        return max(0, self._depth)

    def note_change(self, n: int = 1) -> None:
        """Record that ``n`` reversible changes were written.

        The handler never says how deep the buffer is, and undo does not report
        hitting the bottom, so :meth:`unroll` can only wind back what it was
        told about. A caller that writes without telling us leaves changes the
        client cannot count -- release still keeps them, which is the safe
        direction, but unroll will not reach them.
        """
        self._depth += int(n)

    def release(self) -> SafeModeStatus:
        """Exit, keeping whatever the buffer currently reflects."""
        if self.session_id is None:
            return self.status()
        try:
            self._send(CMD_RELEASE, session_id=self.session_id)
        finally:
            # Whether or not the reply arrived, this session no longer owns
            # safe mode in any way we can act on; holding a stale id would make
            # a later take() a silent no-op.
            self.session_id = None
            self._depth = 0
        return self.status()

    def unroll(self) -> SafeModeStatus:
        """Undo every change we know about, then exit.

        There is no discard-on-exit verb: winding the buffer back to empty and
        then releasing is what "leave without keeping" means here.
        """
        if self.session_id is None:
            return self.status()
        steps = 0
        while self._depth > 0 and steps < MAX_UNROLL_STEPS:
            self.undo()
            steps += 1
        return self.release()

    def toggle(self) -> SafeModeStatus:
        return self.release() if self.session_id is not None else self.take()

    def close(self) -> None:
        """Drop the session without unrolling. Safe on an already-closed one."""
        if self.session_id is None:
            return
        try:
            self.release()
        except Exception:  # noqa: BLE001
            self.session_id = None
            self._depth = 0
