"""Form for a schema ``kind:'doit'`` command — System ▸ Password and friends.

A doit is a one-shot action that takes arguments but returns no result view,
so neither the property form (which edits a row) nor the query window (which
streams results) fits. Winbox shows a small modal, you fill it in, it runs.

The fields come from the schema, so this is not a password dialog that happens
to be generic: ``(13,1)`` cmd 3 declares Old Password / New Password / Confirm
Password, and that is exactly what gets rendered.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
)

from x3.proto import schema as sch

from .. import a11y
from .wheel_guard import guard_wheel


def _is_password(fld) -> bool:
    return (fld.jg_type or "") == "password" or sch.is_secret(fld)


class CommandDialog(QDialog):
    """Collect a doit command's arguments and run it."""

    def __init__(
        self, title: str, menu, *, client, obj_id: int | None = None,
        on_accept=None, prefill: dict | None = None, note: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self._menu = menu
        self._client = client
        #: Object the command acts on, when one was selected. See _run.
        self._obj_id = obj_id
        self._editors: dict[int, Any] = {}
        #: Set for a dialog that is not a device command: OK hands the values
        #: to this instead (a `termopen` dialog opens a session with them).
        self._on_accept = on_accept

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        root.addLayout(form)

        for code, fld in self._ordered_fields():
            editor = self._make_editor(fld)
            if editor is None:
                continue
            self._editors[code] = editor
            form.addRow(f"{fld.name}:", editor)
            a11y.label_editor(editor, fld.name or "")

        if not self._editors:
            form.addRow(QLabel("This command takes no arguments."))
        if prefill:
            self._apply_prefill(prefill)
        if note:
            hint = QLabel(note)
            hint.setWordWrap(True)
            root.addWidget(hint)
        # A wheel roll aimed at the dialog must not quietly retarget a
        # dropdown it passes over (see wheel_guard).
        guard_wheel(self)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        buttons.accepted.connect(self._run)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # --- form ---------------------------------------------------------

    def _ordered_fields(self) -> list[tuple[int, Any]]:
        """Declaration order, so Old comes before New before Confirm."""
        seen: set[int] = set()
        out: list[tuple[int, Any]] = []
        for _tab, code in getattr(self._menu, "form_order", ()) or ():
            fld = self._menu.fields.get(code)
            if fld is not None and code not in seen:
                out.append((code, fld))
                seen.add(code)
        for code, fld in self._menu.fields.items():
            if code not in seen and code != sch.STD_OBJ_ID_CODE:
                out.append((code, fld))
                seen.add(code)
        return out

    def _make_editor(self, fld):
        jg = fld.jg_type or ""
        if jg == "bool":
            return QCheckBox()
        if fld.enum:
            combo = QComboBox()
            for value, label in sorted(fld.enum.items()):
                combo.addItem(str(label), value)
            return combo
        line = QLineEdit()
        if _is_password(fld):
            line.setEchoMode(QLineEdit.EchoMode.Password)
        return line

    def _value_of(self, editor):
        if isinstance(editor, QCheckBox):
            return editor.isChecked()
        if isinstance(editor, QComboBox):
            return editor.currentData()
        return editor.text()

    # --- run ----------------------------------------------------------

    def _confirm_mismatch(self) -> str | None:
        """Catch a mistyped confirmation before it reaches the router.

        The device does check, but it answers with a bare FAILED string; saying
        it here means the typed-once field is still on screen to fix.
        """
        pairs: list[tuple[str, Any]] = [
            ((self._menu.fields[c].name or "").strip().lower(), e)
            for c, e in self._editors.items()
            if c in self._menu.fields
        ]
        by_name = dict(pairs)
        for name, editor in pairs:
            if not name.startswith("confirm"):
                continue
            subject = name.replace("confirm", "").strip() or "password"
            other = by_name.get(f"new {subject}") or by_name.get(subject)
            if other is None:
                continue
            if self._value_of(editor) != self._value_of(other):
                return (
                    f"The two {subject} entries do not match. "
                    "Retype them and try again."
                )
        return None

    def _apply_prefill(self, prefill: dict) -> None:
        """Fill editors named by a context action's ``link:`` (by field name)."""
        by_name: dict[str, Any] = {}
        for code, editor in self._editors.items():
            fld = self._menu.fields.get(code)
            if fld is not None and fld.name:
                by_name.setdefault(fld.name.strip(), editor)
        for name, value in prefill.items():
            editor = by_name.get(str(name).strip())
            if editor is None:
                continue
            raw = value.get("raw") if isinstance(value, dict) else value
            text = value.get("text") if isinstance(value, dict) else value
            if isinstance(editor, QCheckBox):
                editor.setChecked(bool(raw))
            elif isinstance(editor, QComboBox):
                idx = editor.findData(raw)
                if idx < 0 and text is not None:
                    idx = editor.findText(str(text))
                if idx >= 0:
                    editor.setCurrentIndex(idx)
            elif isinstance(editor, QSpinBox):
                try:
                    editor.setValue(int(raw))
                except (TypeError, ValueError):
                    pass
            elif isinstance(editor, QLineEdit):
                editor.setText("" if text is None else str(text))

    def values_by_name(self) -> dict[str, Any]:
        """What the form holds, keyed by field name."""
        out: dict[str, Any] = {}
        for code, editor in self._editors.items():
            fld = self._menu.fields.get(code)
            if fld is None or not fld.name:
                continue
            out[fld.name.strip()] = self._value_of(editor)
        return out

    def _run(self) -> None:
        if self._on_accept is not None:
            # Not a device command: the caller does something with the values
            # (a termopen dialog opens a session).
            try:
                self._on_accept(self.values_by_name())
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, self.windowTitle(), str(e))
                return
            self.accept()
            return
        mismatch = self._confirm_mismatch()
        if mismatch:
            QMessageBox.warning(self, self.windowTitle(), mismatch)
            return

        values = {
            code: self._value_of(editor)
            for code, editor in self._editors.items()
        }
        # A blank line edit means "not supplied", not "set it to empty".
        values = {c: v for c, v in values.items() if v != ""}

        command = getattr(self._menu, "command", None)
        if command is None:
            QMessageBox.warning(
                self, self.windowTitle(), "This command declares no verb."
            )
            return
        path = tuple(self._menu.path)
        # A doit on a collection needs the object it acts on; the device
        # answers UNKNOWNID for a made-up id like 0. Nothing was selected here
        # (this was opened from the menu tree), so ask the client what this
        # command means by default -- for System ▸ Password, the logged-in user.
        obj_id = self._obj_id
        if obj_id is None:
            resolve = getattr(self._client, "default_command_target", None)
            if resolve is not None:
                try:
                    obj_id = resolve(path)
                except Exception:  # noqa: BLE001
                    obj_id = None
        try:
            self._client.run_command(path, int(command), values, obj_id=obj_id)
        except Exception as e:  # noqa: BLE001
            # The router puts the reason only in the string ("Bad old password!").
            QMessageBox.warning(
                self, "Not applied", f"The router refused the command:\n\n{e}"
            )
            return
        QMessageBox.information(
            self, self.windowTitle(), f"{self.windowTitle()} — done."
        )
        self.accept()
