"""Pending changes: what Queue mode holds, one change per line, each droppable.

It was a message box with the queue as a text dump and three buttons, all of
them about the whole queue: one wrong change meant sending all of it or
throwing all of it away. Here every change is its own entry with its fields
beneath, and Remove takes out just the ones selected -- the lists redraw
without them, since they draw the queue over the router's rows.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from .. import style

#: Item data role holding the QueuedChange a top-level entry stands for.
_CHANGE_ROLE = int(Qt.ItemDataRole.UserRole)

#: What each kind of change is called in the list.
_VERBS = {
    "add": "Add", "set": "Edit", "set_settings": "Edit", "remove": "Remove",
    "enable": "Enable", "disable": "Disable", "comment": "Comment",
    "move": "Move", "doit": "Run",
}


def change_lines(change) -> list[str]:
    """The fields a change writes, as "Name: old → new".

    A new object has no "old" worth showing -- it was the prefilled default,
    for an Interface the raw id -- so an Add lists its values alone.
    """
    lines = []
    for e in change.edits or []:
        name = e.get("name") or "?"
        new = e.get("label", e.get("new", ""))
        old = e.get("old")
        if change.action == "add" or old in (None, ""):
            lines.append(f"{name}: {new}")
        else:
            lines.append(f"{name}: {old} → {new}")
    if not lines and change.action == "comment":
        lines.append(f"Comment: {change.values.get('Comment', '')}")
    return lines


def _object_label(change) -> str:
    """The object a change is about, as its list names it.

    The title already reads "Address — comment 172.16.100.254/24"; the part
    after the dash, less the verb, is the row's own label. The row key is an
    internal handle ("(20, 1):172.16.100.254/24") and only a fallback.
    """
    if change.action == "add":
        return "new object"
    title = change.title or ""
    if " — " in title:
        part = title.split(" — ", 1)[1].strip()
        if part.lower().startswith(change.action):
            part = part[len(change.action):].strip()
        if part and part != "(new)":
            return part
    return str(change.row_key or "")


class PendingChangesDialog(QDialog):
    """The queue, reviewable and editable. ``exec()`` returns Accepted for
    Apply all, so the caller sends it with its own reporting."""

    def __init__(self, session, parent=None, *, can_send: str | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Pending changes")
        self.setModal(True)
        self.resize(style.scale_px(620), style.scale_px(380))

        lay = QVBoxLayout(self)
        self._summary = QLabel()
        lay.addWidget(self._summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Change", "Where"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setColumnWidth(0, style.scale_px(400))
        lay.addWidget(self.tree, 1)

        row = QHBoxLayout()
        self._remove_btn = QPushButton("Remove")
        self._remove_btn.setToolTip("Take the selected changes out of the queue, unsent")
        self._remove_btn.clicked.connect(self._remove_selected)
        self._discard_btn = QPushButton("Discard all")
        self._discard_btn.clicked.connect(self._discard_all)
        self._apply_btn = QPushButton("Apply all")
        self._apply_btn.setToolTip("Send every queued change to the router, oldest first")
        self._apply_btn.clicked.connect(self.accept)
        if can_send:
            # Offered-but-refused, with the reason on it: an absent button
            # cannot answer "why can I not send this".
            self._apply_btn.setEnabled(False)
            self._apply_btn.setToolTip(can_send)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        # Close is what Enter and Esc do: Apply all writes to a router.
        close.setDefault(True)
        for b in (self._remove_btn, self._discard_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self._apply_btn)
        row.addWidget(close)
        lay.addLayout(row)

        self.tree.itemSelectionChanged.connect(self._sync_buttons)
        self._fill()

    # --- contents -------------------------------------------------------

    def _fill(self) -> None:
        self.tree.clear()
        for n, change in enumerate(self.session.queue, start=1):
            verb = _VERBS.get(change.action, change.action)
            where = change.title.split(" — ")[0] if change.title else ""
            top = QTreeWidgetItem([f"{n}. {verb} {_object_label(change)}".strip(), where])
            top.setData(0, _CHANGE_ROLE, change)
            for line in change_lines(change):
                top.addChild(QTreeWidgetItem([line, ""]))
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)
        count = len(self.session.queue)
        self._summary.setText(
            f"{count} change(s) queued. Nothing has been sent to the router."
            if count else "The queue is empty."
        )
        self._sync_buttons()

    def _selected_changes(self) -> list:
        out = []
        for item in self.tree.selectedItems():
            top = item if item.parent() is None else item.parent()
            change = top.data(0, _CHANGE_ROLE)
            if change is not None and change not in out:
                out.append(change)
        return out

    def _sync_buttons(self) -> None:
        has = bool(self.session.queue)
        self._remove_btn.setEnabled(bool(self._selected_changes()))
        self._discard_btn.setEnabled(has)
        if not has:
            self._apply_btn.setEnabled(False)

    # --- actions --------------------------------------------------------

    def _remove_selected(self) -> None:
        for change in self._selected_changes():
            self.session.drop(change)
        self._fill()

    def _discard_all(self) -> None:
        n = len(self.session.queue)
        ans = QMessageBox.question(
            self, "Discard all",
            f"Throw away all {n} queued change(s)? None of them has been sent.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.session.clear()
            self._fill()


def open_pending(session, parent, *, can_send: str | None, apply: Callable[[], None]) -> None:
    """Show the dialog; send the queue if it was closed with Apply all."""
    dlg = PendingChangesDialog(session, parent, can_send=can_send)
    if dlg.exec() == QDialog.DialogCode.Accepted and session.queue:
        apply()
