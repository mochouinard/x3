"""MDI child: /system package update — Winbox's "Check For Updates".

Everything on screen comes from the (24,24) schema menu: the rows from
``form_layout``, the status line from ``statusbar``, and the buttons from
``form_actions`` with their command numbers and ``on:`` conditions. Nothing is
keyed on a hardcoded field code beyond ``state``, because the menu genuinely
differs between releases — the Channel field is 0xa on most builds but 0xb on
the 6.48.6-6.49.19 long-term branch and absent before 6.31, and 7.23.1 added
Mode / Check Certificate / IP Version. Walking the schema absorbs all of that.

Unlike a property form this window issues commands directly rather than
queueing them: checking for updates and downloading are operations, not
config edits, so there is nothing meaningful to stage. The install command is
the exception the extra confirmation exists for — it reboots the router.

Dropdowns write immediately too, which is what the schema's ``type:autoset``
wrapper around Channel means and what real Winbox does — command 0xFE000E
carrying just the changed field. The device answers nothing directly; it re-runs
its version check and reports the new value on the next poll or pushed frame.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMdiSubWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from x3.proto import schema as sch

from .. import a11y, style
from ..icons import window_icon
from .wheel_guard import guard_wheel

if TYPE_CHECKING:
    from x3.proto.device import DeviceClient, RowObject
    from x3.proto.schema import Menu


#: Field 0x5. Winbox's own conditions read it: 0 -> "latest", 1 -> "newver",
#: 2 -> "downloading". We only need it to decide when to stop polling hard.
_STATE = 0x5

#: Substrings that mark a button as the one that reboots the device. The label
#: is release-dependent ("Download&Install" from 6.31, "Download&Upgrade"
#: before it), so match on the word rather than the whole string.
_INSTALL_WORDS = ("install", "upgrade")


def _as_text(value: object) -> str:
    """Wire value to display text, without ``b'...'`` leaking to the screen."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


class _PollWorker(QThread):
    """Background GET so a slow router never freezes the UI thread."""

    finished_ok = Signal(object)
    finished_err = Signal(str)

    def __init__(self, fetcher: Callable[[], Any], parent=None) -> None:
        super().__init__(parent)
        self._fetcher = fetcher

    def run(self) -> None:  # noqa: N802
        try:
            self.finished_ok.emit(self._fetcher())
        except Exception as e:  # noqa: BLE001
            self.finished_err.emit(f"{type(e).__name__}: {e}")


class _ProgressReader(QThread):
    """Drains the progress subscription and re-emits each pushed row.

    Blocking ``sub.get`` in short slices keeps close() responsive without
    putting a timeout anywhere near the shared socket.
    """

    row = Signal(object)
    ended = Signal(str)

    def __init__(self, sub, decode, parent=None) -> None:
        super().__init__(parent)
        self._sub = sub
        self._decode = decode
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: N802
        while not self._stop:
            try:
                msg = self._sub.get(timeout=0.25)
            except Exception as e:  # noqa: BLE001 — subscription closed
                if not self._stop:
                    self.ended.emit(f"{type(e).__name__}: {e}")
                return
            if msg is None:
                continue
            try:
                decoded = self._decode(msg)
            except Exception:  # noqa: BLE001 — a decode gap must not stop us
                continue
            if decoded is not None:
                self.row.emit(decoded)


class _ConfirmInstallDialog(QDialog):
    """Type-the-identity gate in front of the reboot.

    A plain yes/no is too easy to click through when the consequence is a
    production router restarting, and this tool is aimed at a fleet where the
    wrong window is a real possibility.
    """

    def __init__(self, identity: str, version: str, action: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(action)
        self._identity = identity
        lay = QVBoxLayout(self)
        lay.setSpacing(max(6, style.SPACING * 2))
        msg = QLabel(
            f"<b>{action}</b> will install <b>{version or 'the downloaded version'}</b> "
            f"and <b>reboot the router</b>.<br><br>"
            f"The device will be unreachable while it restarts, and an "
            f"interrupted upgrade can leave it needing manual recovery.<br><br>"
            f"Type the device identity <b>{identity}</b> to continue:"
        )
        msg.setWordWrap(True)
        lay.addWidget(msg)
        self._edit = QLineEdit()
        self._edit.setPlaceholderText(identity)
        lay.addWidget(self._edit)
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText(action)
        ok.setEnabled(False)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        lay.addWidget(self._buttons)
        self._edit.textChanged.connect(self._check)
        self.resize(style.scale_px(430), style.scale_px(210))

    def _check(self, text: str) -> None:
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(text.strip() == self._identity)


class UpdateWindow(QMdiSubWindow):
    """Check For Updates, driven entirely by the (24,24) schema menu."""

    _POLL_MS = 1000

    def __init__(
        self,
        client: "DeviceClient",
        *,
        menu: "Menu",
        edit_session=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.client = client
        self.menu = menu
        self.edit_session = edit_session
        self.path = tuple(menu.path)
        self.setWindowTitle(menu.title or menu.name or "Check For Updates")
        self.setWindowIcon(window_icon("item"))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self._from_path = client.alloc_from()
        self._value_widgets: dict[int, QWidget] = {}
        #: Codes rendered as editable controls, so a poll refresh does not
        #: yank the value out from under someone mid-choice.
        self._writable_codes: set[int] = set()
        self._action_buttons: list[tuple[dict[str, Any], QPushButton]] = []
        self._values: dict[int, Any] = {}
        self._poll_timer: QTimer | None = None
        self._poll_worker: _PollWorker | None = None
        self._poll_busy = False
        self._busy_note = ""
        self._install_sent = False
        self._sub = None
        self._reader: _ProgressReader | None = None
        # Set once close starts. A poll reply queued before the close is still
        # delivered after it, and acting on that late row would re-subscribe on
        # a window Qt is about to delete -- spawning a thread that outlives its
        # parent, which aborts the process.
        self._closing = False

        self.setWidget(self._build())
        self.resize(style.scale_px(560), style.scale_px(460))
        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        esc.activated.connect(self.close)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self.menu.autorefresh_ms or self._POLL_MS)
        self._poll_timer.timeout.connect(self._tick)
        self._poll_timer.start()
        self._tick()

    # --- construction --------------------------------------------------

    def _build(self) -> QWidget:
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(*([max(4, style.SPACING * 2)] * 4))
        outer.setSpacing(max(4, style.SPACING))

        body = QHBoxLayout()
        body.setSpacing(max(6, style.SPACING * 2))
        body.addWidget(self._build_form(), 1)
        body.addWidget(self._build_buttons(), 0)
        outer.addLayout(body, 1)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {style.TEXT_DIM};")
        outer.addWidget(self._status)
        return root

    def _build_form(self) -> QWidget:
        """Rows in the schema's own order, skipping chrome and `state`.

        `state` drives the buttons rather than being shown -- the .jg marks it
        nonpublic, and Winbox does not display it either.
        """
        page = QWidget()
        form = QFormLayout(page)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(max(3, style.SPACING))

        chrome = set(self.menu.form_chrome_codes() or set())
        layout = list(self.menu.form_layout or [])
        if not layout:
            layout = [
                {"kind": "field", "code": c}
                for c in sorted(self.menu.fields or {})
            ]

        for entry in layout:
            kind = entry.get("kind")
            if kind == "separator":
                line = QFrame()
                line.setFrameShape(QFrame.Shape.HLine)
                line.setFrameShadow(QFrame.Shadow.Sunken)
                line.setStyleSheet(f"color: {style.BORDER};")
                form.addRow(line)
                continue
            if kind != "field":
                continue
            code = entry.get("code")
            if not isinstance(code, int) or code in chrome or code == _STATE:
                continue
            fld = (self.menu.fields or {}).get(code)
            if fld is None:
                continue
            widget = self._widget_for(fld, code)
            self._value_widgets[code] = widget
            label = (fld.name or "").strip()
            if label:
                form.addRow(f"{label}:", widget)
                a11y.label_editor(widget, label)
            else:
                # The changelog row is deliberately unlabelled in the .jg.
                form.addRow(widget)
        # Channel is a dropdown next to a scrolling changelog: a wheel roll
        # over it must not switch release channels (see wheel_guard).
        guard_wheel(page)
        return page

    def _widget_for(self, fld: "sch.Field", code: int) -> QWidget:
        """One schema field. Enums are writable; everything else is display."""
        if fld.jg_type == "multilinestring":
            box = QPlainTextEdit()
            box.setReadOnly(True)
            box.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            box.setMinimumHeight(style.scale_px(150))
            return box
        if fld.enum and not fld.ro:
            # The .jg wraps Channel in type:autoset -- picking a value writes it
            # immediately, no Apply. Command 0xFE000E, one field, no reply.
            combo = QComboBox()
            for value, text in sorted(fld.enum.items()):
                combo.addItem(str(text), value)
            combo.setToolTip(
                f"{fld.name or hex(code)} — applied to the device as soon as "
                f"you pick it; the router then re-checks for updates."
            )
            combo.activated.connect(
                lambda _idx, c=code, w=combo: self._on_enum_chosen(c, w)
            )
            self._writable_codes.add(code)
            return combo
        if fld.enum:
            combo = QComboBox()
            for value, text in sorted(fld.enum.items()):
                combo.addItem(str(text), value)
            combo.setEnabled(False)
            return combo
        edit = QLineEdit()
        edit.setReadOnly(True)
        edit.setFrame(True)
        return edit

    def _on_enum_chosen(self, code: int, combo: QComboBox) -> None:
        """Write a dropdown straight to the device, Winbox-style."""
        value = combo.currentData()
        previous = self._values.get(code)
        if value is None or value == previous:
            return
        fld = (self.menu.fields or {}).get(code)
        label = (fld.name if fld and fld.name else hex(code))
        if getattr(self.edit_session, "readonly", False):
            self._restore_enum(code, combo)
            QMessageBox.information(
                self, label,
                "Read-only mode is on.\n\n"
                f"Uncheck Read-only in the toolbar to change {label}.",
            )
            return
        try:
            self.client.package_update_set(
                code, value, from_path=self._from_path
            )
        except Exception as e:  # noqa: BLE001
            self._restore_enum(code, combo)
            QMessageBox.warning(self, label, f"Could not set {label}:\n\n{e}")
            return
        self._busy_note = f"{label} set to {combo.currentText()!r}."
        # The device re-runs its check after a channel change, so let the next
        # poll or pushed frame report the real value rather than assuming.
        self._tick()

    def _restore_enum(self, code: int, combo: QComboBox) -> None:
        """Put a combo back to the device's value after a refused write."""
        idx = combo.findData(self._values.get(code))
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _build_buttons(self) -> QWidget:
        col = QWidget()
        lay = QVBoxLayout(col)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(max(2, style.SPACING))

        widest = 96
        for act in list(self.menu.form_actions or []):
            if act.get("kind") != "doit":
                continue
            cmd = act.get("cmd")
            if not isinstance(cmd, int) or not cmd:
                continue
            name = str(act.get("name") or "Action")
            btn = QPushButton(name)
            btn.setMinimumHeight(max(22, style.ROW_HEIGHT + 4))
            btn.setToolTip(f"{name} (cmd {cmd})")
            btn.clicked.connect(lambda checked=False, a=act: self._run_action(a))
            lay.addWidget(btn)
            self._action_buttons.append((act, btn))
            widest = max(widest, col.fontMetrics().horizontalAdvance(name) + 26)

        refresh = QPushButton("Refresh")
        refresh.setMinimumHeight(max(22, style.ROW_HEIGHT + 4))
        refresh.clicked.connect(self._tick)
        lay.addWidget(refresh)

        close = QPushButton("Close")
        close.setMinimumHeight(max(22, style.ROW_HEIGHT + 4))
        close.clicked.connect(self.close)
        lay.addWidget(close)

        lay.addStretch(1)
        col.setFixedWidth(min(widest, 220))
        return col

    # --- polling -------------------------------------------------------

    def _tick(self) -> None:
        if self._closing or self._poll_busy:
            return
        self._poll_busy = True
        worker = _PollWorker(
            lambda: self.client.package_update_status(from_path=self._from_path),
            self,
        )
        worker.finished_ok.connect(self._on_row)
        worker.finished_err.connect(self._on_err)
        worker.finished.connect(self._on_poll_done)
        self._poll_worker = worker
        worker.start()

    def _on_poll_done(self) -> None:
        self._poll_busy = False

    # --- pushed progress ------------------------------------------------

    def _ensure_subscribed(self) -> None:
        """Start the progress subscription, once, when a download begins.

        Winbox subscribes only for the download rather than for the whole life
        of the window, so we do the same: on sending Download/Download&Install,
        or on finding the device already at state 2 because the window was
        opened mid-download.
        """
        if self._closing or self._sub is not None:
            return
        try:
            self._sub = self.client.package_update_subscribe(
                from_path=self._from_path
            )
        except Exception as e:  # noqa: BLE001 — polling still works without it
            self._busy_note = f"(no live progress: {e})"
            return
        reader = _ProgressReader(
            self._sub, self.client.package_update_notify_row, self
        )
        reader.row.connect(self._on_row)
        reader.ended.connect(self._on_push_ended)
        self._reader = reader
        reader.start()

    def _on_push_ended(self, message: str) -> None:
        # Expected when the router reboots out from under the subscription.
        if self._install_sent:
            self._on_err(message)

    def _drop_subscription(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.stop()
        sub, self._sub = self._sub, None
        if sub is not None:
            try:
                self.client.package_update_unsubscribe(
                    sub, from_path=self._from_path
                )
            except Exception:  # noqa: BLE001
                pass
        if reader is not None:
            # Unconditionally, never guarded by isRunning(): start() is
            # asynchronous, so a thread closed immediately after opening the
            # window can still be between start() and running. Skipping the
            # wait there lets it come alive after Qt has deleted its parent,
            # which aborts the process.
            reader.wait(2000)

    def _on_row(self, row: "RowObject") -> None:
        if self._closing:
            return
        values = dict(getattr(row, "values_by_code", None) or {})
        self._values = values
        for code, widget in self._value_widgets.items():
            try:
                text = row.get_rendered(code)
            except Exception:  # noqa: BLE001 — a decode gap must not kill polling
                text = ""
            if not text:
                # get_rendered falls back to str() on an undecoded value, which
                # turns a wire string into b'7.23.3'. Decode it ourselves.
                text = _as_text(values.get(code))
            if isinstance(widget, QPlainTextEdit):
                if widget.toPlainText() != text:
                    # Preserve the reader's scroll position across ticks.
                    bar = widget.verticalScrollBar()
                    at = bar.value()
                    widget.setPlainText(text)
                    bar.setValue(min(at, bar.maximum()))
            elif isinstance(widget, QComboBox):
                # Never move a combo while its list is open -- a 1 Hz refresh
                # landing mid-choice would change what the click selects.
                if widget.view().isVisible():
                    continue
                idx = widget.findData(values.get(code))
                if idx >= 0 and idx != widget.currentIndex():
                    widget.setCurrentIndex(idx)
            elif isinstance(widget, QLineEdit):
                widget.setText(text or "—")
        # State 2 means a download is under way, whether we started it or the
        # window opened onto one already running.
        if values.get(_STATE) == 2:
            self._ensure_subscribed()
        self._refresh_buttons()
        self._refresh_status(row)

    def _on_err(self, message: str) -> None:
        # The device drops the session as it reboots, which is the expected
        # end of an install rather than a fault worth a dialog.
        if self._install_sent:
            self._stop_polling()
            self._status.setText(
                "Install started — the router is rebooting and this session "
                "has ended. Reconnect once it is back."
            )
            for _act, btn in self._action_buttons:
                btn.setEnabled(False)
            return
        self._status.setText(message)

    def _refresh_status(self, row: "RowObject") -> None:
        """The .jg puts field 0x3 in a statusbar; show that, plus our note."""
        parts: list[str] = []
        for item in list(self.menu.statusbar or []):
            code = item.get("code")
            if not isinstance(code, int):
                continue
            try:
                text = row.get_rendered(code)
            except Exception:  # noqa: BLE001
                text = ""
            if not text:
                text = _as_text(self._values.get(code))
            if text:
                name = item.get("name")
                parts.append(f"{name}: {text}" if name else text)
        if self._busy_note:
            parts.append(self._busy_note)
        state = self._values.get(_STATE)
        if not parts and state is not None:
            parts.append({0: "Up to date", 1: "Update available",
                          2: "Working…"}.get(state, f"state {state}"))
        self._status.setText("   ".join(parts))

    def _stop_polling(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()

    # --- actions -------------------------------------------------------

    def _refresh_buttons(self) -> None:
        """Show/hide each button per its on: condition, every tick.

        These are `hidedynamicly` conditions on `state`, so they flip while the
        window is open -- Check For Updates gives way to Download and
        Download&Install, which give way to Cancel. Evaluating them only at
        construction (as the generic property form does) would freeze the
        window in whatever state the device happened to be in when it opened.
        """
        readonly = bool(getattr(self.edit_session, "readonly", False))
        for act, btn in self._action_buttons:
            visible = True
            on = act.get("on")
            if on:
                try:
                    visible = sch.condition_is_true(self.menu, on, self._values)
                except Exception:  # noqa: BLE001
                    visible = True
            btn.setVisible(visible)
            btn.setEnabled(visible and not readonly and not self._install_sent)
            if readonly:
                btn.setToolTip(
                    "Read-only mode is on — uncheck Read-only in the toolbar."
                )

    def _run_action(self, act: dict[str, Any]) -> None:
        name = str(act.get("name") or "Action")
        cmd = act.get("cmd")
        if not isinstance(cmd, int) or not cmd:
            return
        if getattr(self.edit_session, "readonly", False):
            QMessageBox.information(
                self, name,
                "Read-only mode is on.\n\n"
                "Uncheck Read-only in the toolbar to run update commands.",
            )
            return

        installing = any(w in name.lower() for w in _INSTALL_WORDS)
        if installing:
            identity = (
                getattr(getattr(self.client, "info", None), "identity", None)
                or getattr(getattr(self.client, "info", None), "name", None)
                or self.client.host
            )
            latest = self._latest_version()
            dlg = _ConfirmInstallDialog(str(identity), latest, name, self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
        elif act.get("on") == "downloading":
            pass   # Cancel is harmless; no prompt.

        try:
            self.client.package_update_run(cmd, from_path=self._from_path)
        except Exception as e:  # noqa: BLE001 — surface, never crash the UI
            if installing:
                # The reboot can race the reply; treat it as started.
                self._install_sent = True
                self._on_err(f"{type(e).__name__}: {e}")
                return
            QMessageBox.warning(self, name, f"{name} failed:\n\n{e}")
            return

        if installing:
            self._install_sent = True
            self._busy_note = "Install requested; the router will reboot."
            self._ensure_subscribed()
            self._refresh_buttons()
        else:
            self._busy_note = f"{name} sent."
            # Download (cmd 1) streams progress the same way an install does.
            if act.get("on") == "newver":
                self._ensure_subscribed()
        self._tick()

    def _latest_version(self) -> str:
        """Whatever the schema calls "Latest Version", by name not by code."""
        for code, fld in (self.menu.fields or {}).items():
            if (fld.name or "").strip().lower() == "latest version":
                raw = self._values.get(code)
                return "" if raw is None else str(raw)
        return ""

    # --- teardown ------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closing = True
        self._stop_polling()
        self._drop_subscription()
        self._poll_worker = None
        # Join every worker still parented to this window, not just the most
        # recent one. Each tick creates a _PollWorker owned by the window, so a
        # slow reply can leave an earlier one alive; Qt aborts the process if
        # any QThread outlives its parent. wait() is unconditional because
        # start() is asynchronous -- isRunning() can still be False for a
        # thread that is about to run.
        for thread in self.findChildren(QThread):
            stop = getattr(thread, "stop", None)
            if callable(stop):
                stop()
        for thread in self.findChildren(QThread):
            thread.wait(2000)
        super().closeEvent(event)
