"""UI edit modes: read-only, live write-through, or a local queue."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

#: Standard wire codes a change can carry without the schema naming them.
STD_DISABLED_CODE = 0xFE000A
STD_COMMENT_CODE = 0xFE0009


class EditMode(str, Enum):
    READONLY = "readonly"
    #: Write-through: Apply sends the change to the device now.
    LIVE = "live"
    QUEUE = "queue"


@dataclass
class QueuedChange:
    path: tuple[int, ...]
    title: str
    # field name → new display/wire value
    values: dict[str, Any] = field(default_factory=dict)
    # optional object identity for list rows
    row_key: str = ""
    #: The router's own object `.id`. Every verb but ADD needs it, and no
    #: display string can stand in for it — two rows can render identically.
    obj_id: int | None = None
    # Per-field detail: code, name, old, new, label. Carries the wire field
    # code, which `values` (keyed by display name) cannot, so applying a
    # change can build a SET message from it.
    edits: list[dict[str, Any]] = field(default_factory=list)
    # set | set_settings | add | remove | enable | disable | comment
    action: str = "set"
    #: Wire fields to send that no editor produced -- an ADD of a typed
    #: interface (VLAN, EoIP, …) must carry the interface type (0x10001), which
    #: is schema-derived, not something the user types. ``{code: value}``.
    extra_wire: dict[int, Any] = field(default_factory=dict)
    #: Which form produced this, so a re-save from the same form replaces its
    #: own entry. A form's edits are everything changed since it opened, so
    #: its newer change contains its older one; another form's does not.
    origin: str = ""

    def wire_values(self) -> dict[int, Any]:
        """The edits that carry a wire code, as ``{code: new_value}``.

        An edit without a code cannot be sent — it only ever had a display
        name — so it is dropped here rather than guessed at.
        """
        out: dict[int, Any] = {}
        for e in self.edits:
            code = e.get("code")
            if isinstance(code, int):
                out[code] = e.get("new")
            # An optional match writes three sibling codes (present / negated /
            # value) from one editor; they ride along under "extra".
            extra = e.get("extra")
            if isinstance(extra, dict):
                for c, v in extra.items():
                    if isinstance(c, int):
                        out[c] = v
        # Schema-derived fields (e.g. an ADD's interface type) that ride along.
        for c, v in self.extra_wire.items():
            if isinstance(c, int):
                out.setdefault(c, v)
        return out

    def uncodeable(self) -> list[str]:
        """Names of edits that carry no wire code, and so cannot be sent."""
        return [
            str(e.get("name") or "?") for e in self.edits
            if not isinstance(e.get("code"), int)
        ]


class EditSession:
    """Session-wide mode, the live write path, and the local change queue."""

    def __init__(
        self,
        mode: EditMode = EditMode.QUEUE,
        *,
        client: Any = None,
        write_guard: Callable[[], str | None] | None = None,
    ) -> None:
        self.mode = mode
        self.queue: list[QueuedChange] = []
        self.client = client
        #: Returns why a write must not happen right now, or None to allow it.
        #: MainWindow points this at the Safe Mode check.
        self.write_guard = write_guard
        #: Called with ``(change, summary)`` after a write reaches the device.
        #: This is the one choke point every local change goes through, which
        #: is what makes it the right place to arm the automatic snapshot --
        #: hooking the call sites instead would miss the queue flush, and
        #: hooking the device layer would miss which menu it was.
        self.on_applied: Callable[[Any, str], None] | None = None
        #: Called with no arguments whenever the queue's contents change, so
        #: every open list can redraw what it holds over the router's rows.
        self.listeners: list[Callable[[], None]] = []

    def _changed(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - a redraw must not lose the queue
                pass

    @property
    def readonly(self) -> bool:
        return self.mode == EditMode.READONLY

    @property
    def live(self) -> bool:
        return self.mode == EditMode.LIVE

    @property
    def write_supported(self) -> bool:
        """Whether a change can actually reach the device."""
        return self.client is not None

    def blocked_reason(self) -> str | None:
        """Why a live write cannot happen now, or None when it can."""
        if not self.live:
            return "Live mode is not selected."
        return self.write_reason()

    def write_reason(self) -> str | None:
        """Why a write cannot reach the device, whatever the mode.

        Split out of :meth:`blocked_reason` for Apply all, which sends the
        queue from Queue mode: the mode check is the one part that must not
        apply there, while the connection and the Safe Mode guard still must.
        """
        if self.client is None:
            return "This session has no device connection."
        if self.write_guard is not None:
            return self.write_guard()
        return None

    def apply(self, change: QueuedChange, *, require_live: bool = True) -> str:
        """Send one change to the device, and report it. Returns a summary.

        The reporting is deliberately after the write and never in front of it:
        a refused change is not a change, and arming a snapshot for one would
        record a config nobody altered.
        """
        summary = self._apply_one(change, require_live=require_live)
        if self.on_applied is not None:
            try:
                self.on_applied(change, summary)
            except Exception:  # noqa: BLE001
                # Bookkeeping must never turn a successful write into a failure
                # the user has to interpret.
                pass
        return summary

    def _apply_one(self, change: QueuedChange, *, require_live: bool = True) -> str:
        """Send one change to the device. Returns a one-line summary.

        Raises the device layer's own error on rejection, so the router's
        wording (``cannot move builtin``, ``Bad old password!``) reaches the
        user unedited — the router returns FAILED for every business-rule
        refusal, so the string is the only thing that carries the reason.
        """
        reason = self.blocked_reason() if require_live else self.write_reason()
        if reason:
            raise RuntimeError(reason)
        client = self.client
        path = tuple(change.path)
        action = change.action

        if action == "remove":
            if change.obj_id is None:
                raise RuntimeError("no object id: cannot remove this row")
            client.remove_obj(path, change.obj_id)
            return f"Removed {change.row_key or 'object'}"

        if action == "doit":
            # A command, not a field write: the form's own action buttons
            # (Force Update, Reset Counters). Captured: IP ▸ Cloud's Force
            # Update is [123,1] cmd=0x1 carrying 0xFE0001 = 0.
            cmd = change.values.get("_cmd")
            if cmd is None:
                raise RuntimeError("this action declares no command")
            obj_id = change.obj_id
            if obj_id is None:
                resolve = getattr(client, "default_command_target", None)
                if resolve is not None:
                    obj_id = resolve(path)
            client.run_command(path, int(cmd), obj_id=obj_id)
            return f"Ran {change.values.get('_doit') or 'command'}"

        if action == "set_settings":
            # One object, no `.id` — System ▸ Identity, IP ▸ Cloud. There is
            # nothing to key a SETOBJ on and nothing to ADD either; treating it
            # as an Add is what sent ADDOBJ to (24,1) and got NOTIMP back.
            values = change.wire_values()
            if not values:
                raise RuntimeError("nothing to send")
            client.set_settings(path, values)
            n = len(values)
            return f"Wrote {n} field{'s' if n != 1 else ''} to {change.title}"

        if action == "add":
            values = change.wire_values()
            if not values:
                raise RuntimeError("nothing to add: no fields carry a wire code")
            new_id, _row = client.add_obj(path, values)
            # Record the id on the change. The form that sent this has to
            # become an edit of the new object, or its next save adds a second
            # one — which is how a list grew a duplicate on every Apply.
            if new_id is not None:
                change.obj_id = new_id
            return f"Added object .id {new_id}" if new_id is not None else "Added object"

        if action == "move":
            if change.obj_id is None:
                raise RuntimeError("no object id: cannot move this row")
            client.move_obj(path, change.obj_id, before=change.values.get("_before"))
            return f"Moved {change.row_key or 'object'}"

        if action in ("enable", "disable"):
            values = {STD_DISABLED_CODE: action == "disable"}
        elif action == "comment":
            values = {STD_COMMENT_CODE: change.values.get("Comment", "")}
        else:
            values = change.wire_values()
            missing = change.uncodeable()
            if missing:
                raise RuntimeError(
                    "no wire code for: " + ", ".join(missing)
                    + " — this field cannot be sent"
                )

        if not values:
            raise RuntimeError("nothing to send")
        if change.obj_id is None:
            # Deliberately still refused. A collection row whose id we could
            # not work out must not be written: without an id the fields would
            # land on the menu as a whole. A genuine settings menu says so with
            # its own action instead (see set_settings above).
            raise RuntimeError("no object id: cannot edit this row")
        client.set_obj(path, change.obj_id, values)
        n = len(values)
        return f"Wrote {n} field{'s' if n != 1 else ''} to {change.row_key or 'object'}"

    def set_mode(self, mode: EditMode) -> None:
        self.mode = mode

    def enqueue(self, change: QueuedChange) -> None:
        """Hold ``change``, folding it into what it supersedes and nothing else.

        This used to replace any earlier entry with the same path and row key,
        which lost work: every new object is keyed "(new)", so a second Add
        dropped the first, and a Disable dropped the Comment queued on the
        same row before it. Now:

        - a re-save from the same form replaces that form's entry;
        - two field edits of one object merge, the newer value winning;
        - a comment replaces a comment, enable/disable replace each other;
        - anything else -- every Add, a different kind of change -- is kept.

        A replaced or merged entry moves to the end. The queue is sent oldest
        first, and its newest state may lean on something queued after the
        first edit -- an address list the rule now names.
        """
        for i, prior in enumerate(self.queue):
            if prior.path != change.path:
                continue
            if change.origin and prior.origin == change.origin:
                del self.queue[i]
                self.queue.append(change)
                self._changed()
                return
            if "add" in (prior.action, change.action):
                continue
            # Both, not either: an id alone has been seen shared by stand-in
            # rows, and a key alone is a display name two rows can share.
            same_object = (
                prior.obj_id == change.obj_id and prior.row_key == change.row_key
            )
            if not same_object:
                continue
            kinds = {prior.action, change.action}
            if kinds <= {"set"} or kinds <= {"set_settings"}:
                del self.queue[i]
                self.queue.append(_merged(prior, change))
                self._changed()
                return
            if kinds <= {"comment"} or kinds <= {"enable", "disable"}:
                del self.queue[i]
                self.queue.append(change)
                self._changed()
                return
        self.queue.append(change)
        self._changed()

    def clear(self) -> None:
        self.queue.clear()
        self._changed()

    def drop(self, change: QueuedChange) -> None:
        """Take one change out of the queue without sending it."""
        self.queue = [c for c in self.queue if c is not change]
        self._changed()

    def flush_reason(self) -> str | None:
        """Why the queue cannot be sent right now, or None when it can."""
        if not self.queue:
            return "The queue is empty."
        if self.readonly:
            return "Read-only mode is selected: nothing can be written."
        return self.write_reason()

    def flush(
        self,
    ) -> tuple[
        list[tuple[QueuedChange, str]], tuple[QueuedChange, str] | None
    ]:
        """Send every queued change, oldest first. The queue's one exit.

        Stops at the first refusal. A queue is usually a chain -- add an
        address list, then a rule that names it -- so pushing on past a
        broken link writes changes whose premise never landed. Everything
        that went through is dropped from the queue; the change that failed
        stays at its head, so fixing the cause and pressing Apply all again
        resumes from exactly there.

        Returns ``(landed, failed)``: the ``(change, summary)`` pairs that
        reached the device, and the ``(change, reason)`` that did not -- at
        most one, ``None`` when the whole queue went through.
        """
        reason = self.flush_reason()
        if reason:
            raise RuntimeError(reason)
        landed: list[tuple[QueuedChange, str]] = []
        failed: tuple[QueuedChange, str] | None = None
        client = self.client
        # The client refuses every write while `readonly` is set, and Queue
        # mode is exactly the mode that sets it. Apply all *is* the decision
        # to write, so the flag comes off for the length of the flush and
        # goes back however the flush ends.
        has_flag = hasattr(client, "readonly")
        was_readonly = getattr(client, "readonly", False)
        if has_flag:
            client.readonly = False
        try:
            while self.queue:
                change = self.queue[0]
                try:
                    summary = self.apply(change, require_live=False)
                except Exception as e:  # noqa: BLE001
                    failed = (change, str(e))
                    break
                self.queue.pop(0)
                landed.append((change, summary))
        finally:
            if has_flag:
                client.readonly = was_readonly
            if landed:
                self._changed()
        return landed, failed

    def label(self) -> str:
        if self.mode == EditMode.READONLY:
            return "READ-ONLY"
        if self.mode == EditMode.LIVE:
            return "LIVE" if self.write_supported else "LIVE (no write path)"
        n = len(self.queue)
        return f"QUEUE ({n})" if n else "QUEUE"


def _merged(older: QueuedChange, newer: QueuedChange) -> QueuedChange:
    """Two field edits of one object as one: newer values win, older stay.

    Keyed by wire code (the name for an edit that has none); a field edited
    twice keeps the first ``old`` so the queue still says what it was.
    """
    def key(e: dict[str, Any]) -> Any:
        return e.get("code") if isinstance(e.get("code"), int) else e.get("name")

    edits = {key(e): dict(e) for e in older.edits}
    for e in newer.edits:
        k = key(e)
        if k in edits:
            e = {**e, "old": edits[k].get("old", e.get("old"))}
        edits[k] = dict(e)
    return QueuedChange(
        path=newer.path,
        title=newer.title,
        values={**older.values, **newer.values},
        row_key=newer.row_key,
        obj_id=newer.obj_id,
        edits=list(edits.values()),
        action=newer.action,
        extra_wire={**older.extra_wire, **newer.extra_wire},
        origin=newer.origin,
    )
