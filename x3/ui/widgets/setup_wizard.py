"""``type:setup`` -- the DHCP and Hotspot setup wizards.

Two menus are declared this way, and neither is a menu you browse to: Winbox
offers **DHCP Setup** on the DHCP Server window and **Hotspot Setup** on the
Hotspot one, as a button beside Add. The node carries the handler it writes to
((23,0) and (63)) and a flat list of children where every ``tab`` starts a page
and names its prompt::

    {title:'DHCP Setup',type:'setup',path:[23,0],c:[
      {name:'Select interface to run DHCP server on',type:'tab'},
      {name:'DHCP Server Interface',type:'enm',id:'ub',values:{…}},
      {name:'Select network for DHCP addresses',type:'tab'},
      {name:'DHCP Address Space',type:'network',id:'u15',maskid:'u16'},
      …]}

So the pages, their prompts, their fields and their vocabularies are all the
schema's; nothing here knows what a DHCP server is.

The editors come from the property form's own factory, which matters for more
than looks: ``Addresses to Give Out`` is a ``multinetwork`` and ``DNS Servers``
a ``multiipaddr``, and those editors already know how to put an array of
networks on the wire. A text box would have sent a comma-separated string.

Finishing hands the whole page set to the session as one ADD on the wizard's
handler -- the router does the rest of the work (pool, network, DNS) itself,
which is why one request can set up a DHCP server. In queue mode it lands in
the pending list to be reviewed like any other write, and in read-only mode the
dialog says so instead of pretending.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import a11y, style
from ..edit_mode import QueuedChange
from .wheel_guard import guard_wheel


class SetupWizard(QDialog):
    """One page per prompt, Back / Next / Finish, then a single ADD."""

    def __init__(self, menu, *, client, edit_session, parent=None) -> None:
        super().__init__(parent)
        self._menu = menu
        self._client = client
        self._edit = edit_session
        self.setWindowTitle(menu.title or "Setup")
        self.setModal(True)
        #: page index → [(field, editor)] in the order the .jg declares them.
        self._editors: list[list[tuple[Any, QWidget]]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(style.MARGIN, style.MARGIN, style.MARGIN,
                               style.MARGIN)
        root.setSpacing(style.SPACING)

        self._prompt = QLabel("")
        self._prompt.setWordWrap(True)
        root.addWidget(self._prompt)

        self._stack = QStackedWidget()
        root.addWidget(self._stack, 1)
        for prompt, fields in self._pages():
            self._stack.addWidget(self._build_page(prompt, fields))

        self._step = QLabel("")
        self._step.setStyleSheet(f"color: {style.TEXT_DIM};")
        root.addWidget(self._step)

        buttons = QDialogButtonBox()
        self._back = QPushButton("Back")
        self._next = QPushButton("Next")
        self._cancel = QPushButton("Cancel")
        buttons.addButton(self._back, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self._next, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self._cancel, QDialogButtonBox.ButtonRole.RejectRole)
        self._back.clicked.connect(self._go_back)
        self._next.clicked.connect(self._go_next)
        self._cancel.clicked.connect(self.reject)
        root.addWidget(buttons)

        guard_wheel(self)
        self.resize(style.scale_px(460), style.scale_px(220))
        self._show_step(0)

    # --- the pages the .jg declares -----------------------------------

    def _pages(self) -> list[tuple[str, list[Any]]]:
        """(prompt, fields) per page, in declaration order.

        A ``tab`` inside a setup node is a page, and the fields collected under
        it carry its name as their tab -- so the form layout already holds the
        pages, and no second pass over the .jg is needed.
        """
        pages: list[tuple[str, list[Any]]] = []
        for prompt in self._menu.tab_names():
            fields = []
            for item in self._menu.form_layout or []:
                if item.get("tab") != prompt or item.get("kind") != "field":
                    continue
                fld = self._menu.field_for(item.get("code"), item.get("prefix"))
                if fld is not None and fld.name and not fld.nonpublic:
                    fields.append(fld)
            if fields:
                pages.append((prompt, fields))
        return pages

    def _build_page(self, prompt: str, fields: list[Any]) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        row: list[tuple[Any, QWidget]] = []
        for fld in fields:
            editor = _make_wizard_editor(fld, self._client)
            form.addRow(f"{fld.name}:", editor)
            a11y.label_editor(editor, fld.name or "")
            row.append((fld, editor))
        self._editors.append(row)
        return page

    # --- moving between them ------------------------------------------

    def _show_step(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        prompt, _fields = self._pages()[index]
        self._prompt.setText(prompt)
        last = index == self._stack.count() - 1
        self._back.setEnabled(index > 0)
        self._next.setText("Finish" if last else "Next")
        self._step.setText(f"Step {index + 1} of {self._stack.count()}")

    def _go_back(self) -> None:
        self._show_step(max(0, self._stack.currentIndex() - 1))

    def _go_next(self) -> None:
        problem = self._page_problem(self._stack.currentIndex())
        if problem:
            QMessageBox.information(self, self.windowTitle(), problem)
            return
        index = self._stack.currentIndex()
        if index < self._stack.count() - 1:
            self._show_step(index + 1)
            return
        self._finish()

    def _page_problem(self, index: int) -> str | None:
        """What the .jg says is wrong with this page, in words.

        Only what it actually says: ``req``, ``zeroinvalid`` and a ``min`` above
        zero (which on an array means "at least one entry"). Most pages ask for
        nothing, and a page Winbox would let you walk past is not ours to
        block.
        """
        for fld, editor in self._editors[index]:
            if fld.optional or isinstance(editor, QCheckBox):
                continue
            required = bool(fld.required or fld.zeroinvalid or (fld.minimum or 0) > 0)
            if not required:
                continue
            texts = getattr(editor, "texts", None)
            if callable(texts):
                # An array editor: one entry is enough.
                if texts():
                    continue
            else:
                value = _value_of(editor)
                text = "" if value is None else str(value).strip()
                if text and text not in ("0.0.0.0", "0", "::"):
                    continue
            return f"{fld.name} needs a value before you can go on."
        return None

    # --- what Finish does ---------------------------------------------

    def collected(self) -> list[dict[str, Any]]:
        """Every page's answer as queue edits -- ``code``, ``name``, ``new``.

        An array editor reports a ``(tag, value)`` pair the write path passes
        through untouched; everything else reports the value its widget holds.
        """
        edits: list[dict[str, Any]] = []
        for page in self._editors:
            for fld, editor in page:
                wire = getattr(editor, "wire_value", None)
                new = wire() if callable(wire) else _value_of(editor)
                if new is None or (isinstance(new, str) and not new.strip()):
                    continue
                label = getattr(editor, "label", None)
                edits.append({
                    "code": fld.code,
                    "name": fld.name,
                    "old": None,
                    "new": new,
                    "label": label() if callable(label) else str(new),
                })
        return edits

    def change(self) -> QueuedChange:
        """The wizard's whole answer as one ADD on the handler it names."""
        edits = self.collected()
        return QueuedChange(
            path=tuple(self._menu.path or ()),
            title=self._menu.title or "Setup",
            values={e["name"]: e["label"] for e in edits},
            edits=edits,
            action="add",
        )

    def _finish(self) -> None:
        change = self.change()
        if not change.edits:
            QMessageBox.information(
                self, self.windowTitle(), "Nothing was filled in."
            )
            return
        lines = "\n".join(f"• {e['name']}: {e['label']}" for e in change.edits)
        box = QMessageBox(self)
        box.setWindowTitle(self.windowTitle())
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"{self._menu.title} sends one request to "
            f"{tuple(self._menu.path or ())}, and the router creates what it "
            f"needs from it."
        )
        box.setInformativeText(lines)
        box.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        if box.exec() != QMessageBox.StandardButton.Ok:
            return
        summary = self.commit(change)
        if summary is None:
            return
        QMessageBox.information(self, self.windowTitle(), summary)
        self.accept()

    def commit(self, change: QueuedChange) -> str | None:
        """Send it in Live mode, hold it for review otherwise.

        The same rule every other write in this app follows -- a wizard is a
        write like any other, and Live is the only mode that reaches the
        device. None means it went nowhere and the dialog stays open.
        """
        session = self._edit
        if session is None:
            return None
        if not getattr(session, "live", False):
            reason = session.write_reason() if hasattr(session, "write_reason") else None
            if reason and session.client is None:
                QMessageBox.warning(self, self.windowTitle(), reason)
                return None
            session.enqueue(change)
            return (
                f"{change.title} is queued: {len(change.edits)} field(s) to "
                f"add on {tuple(change.path)}. Review it in Pending changes."
            )
        try:
            return session.apply(change)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(
                self, self.windowTitle(),
                f"The router refused it:\n\n{e}",
            )
            return None


def _make_wizard_editor(fld, client) -> QWidget:
    """One editor, from the property form's factory where it applies.

    The array fields are the reason: ``multinetwork`` and ``multiipaddr`` have
    editors that know their wire shape, and a text box does not.
    """
    from .network_array import NetworkArrayEditor
    from .property_window import DurationEdit, _is_network_multi
    from .scalar_array import ScalarArrayEditor, is_scalar_multi

    jg = fld.jg_type or ""
    if jg in ("bool", "flag"):
        box = QCheckBox()
        box.setChecked(bool(fld.default))
        return box
    if jg == "multi" and _is_network_multi(fld):
        return NetworkArrayEditor(fld, None)
    if is_scalar_multi(fld) or jg == "multinetwork":
        return ScalarArrayEditor(fld, None)
    choices: list[tuple[Any, str]] = []
    if client is not None:
        try:
            choices = client.enum_choices(fld)
        except Exception:  # noqa: BLE001
            choices = []
    if choices:
        combo = QComboBox()
        for value, label in choices:
            combo.addItem(str(label), value)
        if fld.default is not None:
            index = combo.findData(fld.default)
            if index >= 0:
                combo.setCurrentIndex(index)
        return combo
    if jg == "interval":
        dur = DurationEdit(
            int(fld.default) if isinstance(fld.default, int) else None,
            scale=fld.scale,
        )
        return dur
    edit = QLineEdit()
    if fld.default is not None:
        edit.setText(fld.render(fld.default))
    if jg in ("network", "ipaddr", "addr"):
        edit.setPlaceholderText("address" + ("/mask" if jg == "network" else ""))
    return edit


def _value_of(editor: QWidget) -> Any:
    from .property_window import _editor_value

    return _editor_value(editor)


def wizard_button_text(menu) -> str:
    """The label Winbox puts on the button that opens this wizard."""
    return str(menu.title or "Setup")


#: Tooltip for that button, so it is clear what it will write to.
def wizard_button_tip(menu) -> str:
    path = tuple(menu.path or ())
    return f"{menu.title}: a guided add on handler {path}"


