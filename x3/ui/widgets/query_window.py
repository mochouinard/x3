"""Winbox tool window: Ping, Traceroute, IP Scan, Bandwidth Test.

Shaped like Winbox's Ping window -- parameters on top, Start/Stop at the right,
results in the middle, the device's own status line at the bottom.

The run model comes from a capture of real Winbox (see the query-tools section
of README): startcmd returns a per-run session id, the window then polls GETALL
carrying that id, and cancelcmd stops it. Because runs are told apart by session
id rather than by handler, several windows onto the same tool work -- which is
what Winbox's "New Window" does -- provided each has its own SYS_FROM.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMdiSubWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QSpinBox,
    QStyledItemDelegate,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from x3.proto.device import QUERY_DONE

from .. import a11y, style
from ..icons import window_icon
from ..models.table_model import SimpleTableModel
from .wheel_guard import guard_wheel


class _QueryWorker(QThread):
    """One poll, off the UI thread."""

    finished_ok = Signal(object)
    finished_err = Signal(str)

    def __init__(self, fn: Callable[[], Any], parent=None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:  # noqa: N802
        try:
            self.finished_ok.emit(self._fn())
        except Exception as e:  # noqa: BLE001
            self.finished_err.emit(f"{type(e).__name__}: {e}")


class _HistoryDelegate(QStyledItemDelegate):
    """Traceroute's History: a bar per probe, newest at the right.

    The cell holds the levels as text ("97 95 101 ..."). Each is a height on
    the .jg's 0..max scale and takes the colour of the first band whose level
    it does not exceed; past max (101) is a lost probe, drawn full height in
    the last band's red.
    """

    def __init__(self, colors, top, parent=None) -> None:
        super().__init__(parent)
        self._bands = sorted(
            (int(level), str(color)) for level, color in colors if level is not None
        ) or [(100, style.SEL_BG)]
        self._max = int(top or 100)

    def _colour(self, level: int) -> str:
        for top, colour in self._bands:
            if level <= top:
                return colour
        return self._bands[-1][1]

    def paint(self, painter, option, index) -> None:  # noqa: N802
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QColor

        self.initStyleOption(option, index)
        option.text = ""
        option.widget.style().drawControl(
            option.widget.style().ControlElement.CE_ItemViewItem, option, painter,
            option.widget,
        )
        try:
            levels = [int(v) for v in str(index.data() or "").split()]
        except ValueError:
            return
        rect = option.rect.adjusted(2, 2, -2, -2)
        # Wide bars while there are few probes, narrowing as the history
        # fills so all of it fits the cell; at the narrowest the oldest scroll
        # off the left.
        widest, narrowest = style.scale_px(8), max(2, style.scale_px(2))
        each = (rect.width() + 1) // max(1, len(levels)) - 1
        bar = max(narrowest, min(widest, each))
        fit = max(1, (rect.width() + 1) // (bar + 1))
        x = rect.right() - bar + 1
        painter.save()
        for level in reversed(levels[-fit:]):
            h = rect.height() if level > self._max else max(
                1, round(rect.height() * level / self._max))
            painter.fillRect(QRect(x, rect.bottom() - h + 1, bar, h),
                             QColor(self._colour(level)))
            x -= bar + 1
        painter.restore()


class QueryWindow(QMdiSubWindow):
    """Run one query tool and show its rows as they arrive."""

    def __init__(
        self,
        title: str,
        menu,
        *,
        client,
        seq: int = 1,
        icon_index: int | None = None,
        prefill: dict[str, Any] | None = None,
        autostart: bool = False,
        on_new_window: Callable[[], None] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{title} <{seq}>" if seq > 1 else title)
        self.setWindowIcon(window_icon("list", sprite_index=icon_index))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.menu = menu
        self.client = client
        self._session_id: int | None = None
        # Where the device says this run has got to (0xfe0003), echoed on the
        # next poll, and the rows accumulated so far (a GETALL poll delivers
        # only what is new).
        self._cursor: int | None = None
        self._table: list[list[str]] = []
        self._worker: _QueryWorker | None = None
        self._editors: dict[int, QWidget] = {}
        # This window's own SYS_FROM: what keeps two runs of the same tool from
        # reading each other's rows.
        try:
            self._from_path = client.alloc_from()
        except Exception:  # noqa: BLE001
            self._from_path = None

        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(style.MARGIN, style.MARGIN, style.MARGIN, style.MARGIN)
        root.setSpacing(style.SPACING)

        body = QHBoxLayout()
        body.setSpacing(max(6, style.SPACING))
        # The request form is laid out in tabs the way Winbox draws it (Ping:
        # General / Advanced); a tool whose request has one tab gets no tab bar.
        by_tab: dict[str, list] = {}
        for tab, fld in self._request_fields():
            by_tab.setdefault(tab, []).append(fld)
        pages: list[tuple[str, QWidget]] = []
        for tab, flds in by_tab.items():
            page = QWidget()
            form = QFormLayout(page)
            form.setContentsMargins(10, 6, 0, 0)
            form.setHorizontalSpacing(max(6, style.MARGIN * 2))
            form.setVerticalSpacing(max(1, style.SPACING))
            form.setLabelAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
            for fld in flds:
                editor = self._make_editor(fld)
                self._editors[fld.code] = editor
                name = fld.name or f"<{fld.code:#x}>"
                form.addRow(name, editor)
                a11y.label_editor(editor, name)
            # Winbox's form is as tall as its tallest tab; a short tab must not
            # centre its rows in the leftover height.
            form.addItem(QSpacerItem(0, 0, QSizePolicy.Policy.Minimum,
                                     QSizePolicy.Policy.Expanding))
            pages.append((tab, page))
        if len(pages) > 1 and self.menu.request_gridcells:
            # Named gridcells are columns, not tabs: Traceroute's Basic and
            # Advanced sit side by side under their titles, as in Winbox v3.
            form_host = QWidget()
            row = QHBoxLayout(form_host)
            row.setContentsMargins(0, 0, 0, 0)
            for tab, page in pages:
                box = QGroupBox(tab)
                inner = QVBoxLayout(box)
                inner.setContentsMargins(2, 2, 2, 2)
                inner.addWidget(page)
                row.addWidget(box, 1)
        elif len(pages) > 1:
            form_host = QTabWidget()
            for tab, page in pages:
                form_host.addTab(page, tab or "General")
        elif pages:
            form_host = pages[0][1]
        else:
            form_host = QWidget()
        # A .jg row action (Neighbors ▸ Tools ▸ Ping) opens this window with
        # its parameters already answered -- by field *name*, which is the only
        # handle the action's `link:` gives.
        if prefill:
            self._apply_prefill(prefill)
        # Enter in a request box starts the run, as it does in Winbox.
        for editor in self._editors.values():
            line = editor.lineEdit() if isinstance(editor, QSpinBox) else editor
            if isinstance(line, QLineEdit):
                line.returnPressed.connect(self.start)
        # A wheel roll aimed at the results must not retarget a request
        # dropdown it passes over (see wheel_guard).
        guard_wheel(form_host)
        body.addWidget(form_host, 1)

        buttons = QVBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(max(2, style.SPACING))
        self._start_btn = QPushButton("Start")
        self._start_btn.clicked.connect(self.start)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.clicked.connect(self.stop)
        self._stop_btn.setEnabled(False)
        self._clear_btn = QPushButton("Clear")
        self._clear_btn.clicked.connect(self._clear)
        btns = [self._start_btn, self._stop_btn, self._clear_btn]
        from x3.proto.schema import offers_new_window

        # Winbox's New Window: a second run of the same tool beside this one
        # (ping two hosts at once). The device keeps them apart by session id.
        self._new_btn: QPushButton | None = None
        if on_new_window is not None and offers_new_window(menu):
            self._new_btn = QPushButton("New Window")
            self._new_btn.clicked.connect(lambda: on_new_window())
            btns.append(self._new_btn)
        for btn in btns:
            btn.setMinimumHeight(max(22, style.ROW_HEIGHT + 4))
            buttons.addWidget(btn)
        buttons.addStretch(1)
        holder = QWidget()
        holder.setLayout(buttons)
        body.addWidget(holder, 0)
        root.addLayout(body, 0)

        self._columns = self._result_columns()
        self._model = SimpleTableModel([self._header(c) for c in self._columns], [])
        self.table = QTableView()
        self.table.setModel(self._model)
        style.style_data_table(self.table)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(style.ROW_HEIGHT)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.setWordWrap(False)
        # The widths the .jg gives its columns (Ping: Seq # 45, Host 150 ...).
        from x3.proto.schema import default_column_widths

        declared = default_column_widths(self.menu)
        for i, col in enumerate(self._columns):
            if declared.get(col.name):
                self.table.setColumnWidth(i, style.scale_px(declared[col.name]))
            if col.kind == "graph":
                self.table.setItemDelegateForColumn(
                    i, _HistoryDelegate(col.graph_colors, col.graph_max, self.table)
                )
        root.addWidget(self.table, 1)

        self._status = QLabel("")
        self._status.setStyleSheet(
            f"color: {style.TEXT}; background: {style.BG_HEADER}; "
            f"border-top: 1px solid {style.BORDER}; padding: 3px 6px; font-size: {style.small_css(9)};"
        )
        root.addWidget(self._status, 0)

        self.setWidget(container)
        self.setMinimumSize(style.scale_px(360), style.scale_px(220))
        self.resize(style.scale_px(720), style.scale_px(420))

        self._timer = QTimer(self)
        self._timer.setInterval(menu.autorefresh_ms or 1000)
        self._timer.timeout.connect(self._tick)
        self._set_status("ready")
        if autostart:
            # After the window is on screen: start() can raise a dialog, and a
            # dialog over a window that has not been shown yet is a dialog
            # over nothing.
            QTimer.singleShot(0, self.start)

    # --- schema helpers -------------------------------------------------

    def _request_fields(self) -> list[tuple[str, Any]]:
        """(tab, field) in the .jg's order; fields no tab claims go on General."""
        out = []
        seen: set[int] = set()
        for tab, code in self.menu.request_order:
            fld = self.menu.request_fields.get(code)
            if fld is None or code in seen or fld.nonpublic or not fld.name:
                continue
            seen.add(code)
            out.append((tab or "General", fld))
        for code, fld in self.menu.request_fields.items():
            if code in seen or fld.nonpublic or not fld.name:
                continue
            out.append(("General", fld))
        return out

    def _result_columns(self) -> list:
        """The columns the .jg declares, as a list draws them.

        A field with a ``width`` is a column, in declaration order -- Ping's
        Seq #, Host, Time, Reply Size, TTL, Status, Winbox v3's set. Taking
        every named field instead showed Min/Avg/Max, which are the status
        bar's, and lost Host, which is a tuple with no field of its own.
        """
        from x3.proto.schema import Column, default_columns, query_columns

        cols = default_columns(self.menu)
        # A declared table is drawn the way Winbox draws one, unit in the
        # header; a readout with no table of its own (Supout's progress) keeps
        # its unit beside the number, "43%".
        self._units_in_header = bool(cols)
        if cols:
            return cols
        return [Column(name=f.name or f"<{f.code:#x}>", field=f)
                for f in query_columns(self.menu)]

    def _unit_in_header(self, col) -> str | None:
        fld = col.field
        if (
            fld is None or not fld.postfix or not self._units_in_header
            or fld.inline_postfix or col.kind != "field"
        ):
            return None
        return fld.postfix

    def _header(self, col) -> str:
        """Winbox puts a table column's unit in its header: "Time (ms)".

        Unless the .jg says ``inlinepostfix``: Traceroute's Loss reads "0.0%".
        """
        unit = self._unit_in_header(col)
        return f"{col.name} ({unit})" if unit else col.name

    def _cell(self, row, col) -> str:
        if col.kind == "graph":
            # (probe, level) pairs; the delegate draws the levels in probe order.
            raw = row.values_by_code.get(col.field.code) if col.field else None
            if not isinstance(raw, list):
                return ""
            pairs = sorted(zip(raw[0::2], raw[1::2]))
            return " ".join(str(v) for _i, v in pairs)
        if col.kind == "tuple":
            parts = (self._address_text(row, code) for code in col.codes)
            return col.sep.join(p for p in parts if p)
        if col.field is None:
            return ""
        text = row.get_rendered(col.field.code, prefer_name=col.field.name)
        unit = self._unit_in_header(col)
        if unit and text.endswith(unit):
            text = text[: -len(unit)]   # the header carries the unit
        return text

    def _address_text(self, row, code: int) -> str:
        """One member of an address tuple (Ping's Host), read off the wire.

        The members have no fields of their own, and a code can be shared:
        Ping's IPv6 host and its Status string are both 0xe, told apart by
        tag. So the value's own shape decides, and a string is never an
        address. A tool started with the global 0xFE000F (Ping) answers in
        network order, as it was asked: 10.0.2.2 arrives as 0x0A000202.
        """
        from x3.proto import schema as sch

        values = getattr(row, "values_by_code_multi", {}).get(code)
        if not values:
            values = [(None, getattr(row, "values_by_code", {}).get(code))]
        for _tag, value in values:
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, int):
                if self.menu.start_command == 0xFE000F:
                    return sch.format_ipv4_net(value)
                return sch.format_ipv4(value)
            if isinstance(value, (bytes, bytearray)) and len(value) == 16:
                return sch.Field(code=code, prefix="a", name="",
                                 jg_type="ip6addr").render(bytes(value))
            if isinstance(value, (bytes, bytearray)) and len(value) == 6:
                return ":".join(f"{b:02X}" for b in value)
        return ""

    def take_focus(self) -> None:
        """Open with the keyboard in the first request box.

        Ping To is the first thing anyone types: open, 8.8.8.8, Enter.
        """
        first = next(iter(self._editors.values()), None)
        if first is not None:
            first.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def _make_editor(self, fld) -> QWidget:
        """One editor, seeded from the .jg default and bounded by min/max.

        The defaults matter on the wire, not just cosmetically: real Winbox
        sends Interval 1000 and Packet Size 56 for Ping, and shipping 0 for an
        interval would ask the router for a flood ping.
        """
        default = getattr(fld, "default", None)
        choices = fld.enum or {}
        if fld.jg_type in ("bool", "flag", "opt"):
            box = QCheckBox()
            box.setChecked(bool(default))
            return box
        if choices:
            combo = QComboBox()
            for value, label in choices.items():
                combo.addItem(str(label), value)
            if default is not None:
                idx = combo.findData(default)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            return combo
        if fld.jg_type in ("number", "bignumber", "numberrange") and not fld.scale:
            spin = QSpinBox()
            low = fld.minimum if isinstance(fld.minimum, int) else 0
            # QSpinBox is a C int: Traceroute's "Max Hops" style fields declare
            # max 0xfffffffe, and setRange() raises OverflowError on that --
            # which is what kept the window from opening at all.
            int_max = 2_147_483_647
            high = min(fld.maximum if isinstance(fld.maximum, int) else int_max, int_max)
            low = max(low, -int_max)
            spin.setRange(min(low, 0), max(high, low))
            # An `opt:1` number starts unset and is not sent until given one --
            # Winbox leaves the box empty. Its default is often the "no limit"
            # sentinel (Traceroute's Count and Max Hops: 0xffffffff), which a
            # C-int box cannot hold anyway: clamped, Count read 2147483647 and
            # went to the router as that, and Ping's TTL went out as 0. The
            # step below the range is the unset value, drawn blank.
            unset_default = not isinstance(default, int) or not (low <= default <= high)
            if fld.optional and spin.minimum() > -int_max:
                spin.setMinimum(spin.minimum() - 1)
                spin.setSpecialValueText(" ")
                spin.setProperty("optional_unset", spin.minimum())
                if unset_default:
                    spin.setValue(spin.minimum())
                    default = None
            if default is not None or not fld.optional:
                # A default can sit outside the field's own range; the box
                # takes the nearest value it can hold.
                value = int(default) if isinstance(default, int) else min(low, 0)
                spin.setValue(max(spin.minimum(), min(value, spin.maximum())))
            spin.setMaximumWidth(140)
            if isinstance(fld.minimum, int) or isinstance(fld.maximum, int):
                spin.setToolTip(f"{low}–{high}")
            return spin
        edit = QLineEdit()
        if default is not None:
            edit.setText(str(default))
        if fld.jg_type == "addr":
            edit.setPlaceholderText("address or host")
        return edit

    def _apply_prefill(self, prefill: dict[str, Any]) -> None:
        """Fill request boxes named by a context action's ``link:``.

        Each entry carries both forms of the value: a dropdown takes the wire
        id it holds (an interface), a text box the rendered text (an address).
        """
        by_name: dict[str, tuple[Any, QWidget]] = {}
        for code, editor in self._editors.items():
            fld = self.menu.request_fields.get(code)
            if fld is not None and fld.name:
                by_name.setdefault(fld.name.strip(), (fld, editor))
        for name, value in prefill.items():
            hit = by_name.get(str(name).strip())
            if hit is None:
                continue
            _fld, editor = hit
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

    def _values(self) -> dict[int, Any]:
        out: dict[int, Any] = {}
        for code, editor in self._editors.items():
            if isinstance(editor, QCheckBox):
                out[code] = editor.isChecked()
            elif isinstance(editor, QComboBox):
                out[code] = editor.currentData()
            elif isinstance(editor, QSpinBox):
                if editor.value() != editor.property("optional_unset"):
                    out[code] = editor.value()
            else:
                text = editor.text().strip()
                if text:
                    out[code] = text
        return out

    # --- run ------------------------------------------------------------

    def start(self) -> None:
        if self._session_id is not None:
            return
        values = self._values()
        self._cursor = None
        self._table = []
        self._model.set_rows([])
        try:
            self._session_id = self.client.start_query(
                self.menu, values, from_path=self._from_path
            )
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, self.windowTitle(), f"Could not start:\n{e}")
            return
        if self._session_id is None:
            QMessageBox.warning(
                self, self.windowTitle(),
                "The device accepted the command but returned no session id, "
                "so there is nothing to poll.",
            )
            return
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._set_status(f"running (session {self._session_id})")
        self._timer.start()
        self._tick()

    def stop(self) -> None:
        self._timer.stop()
        sid, self._session_id = self._session_id, None
        if sid is not None:
            try:
                self.client.cancel_query(self.menu, sid, from_path=self._from_path)
            except Exception:  # noqa: BLE001
                pass
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._set_status(f"stopped — {self._model.rowCount()} row(s)")

    def _clear(self) -> None:
        self._table = []
        self._model.set_rows([])
        self._set_status("cleared")

    def _tick(self) -> None:
        if self._session_id is None or self.client is None:
            return
        if self._worker is not None and self._worker.isRunning():
            return
        sid, cursor = self._session_id, self._cursor
        menu, from_path = self.menu, self._from_path

        def fetch():
            # Only a GETALL-polled run has a cursor to echo.
            extra = {} if cursor is None else {"cursor": cursor}
            return self.client.poll_query(menu, sid, from_path=from_path, **extra)

        worker = _QueryWorker(fetch, self)
        self._worker = worker
        worker.finished_ok.connect(self._on_rows)
        worker.finished_err.connect(self._on_error)
        worker.start()

    def _on_rows(self, rows) -> None:
        if self._session_id is None:
            # Stop was pressed while this poll was in flight.
            return
        new = [[self._cell(row, c) for c in self._columns] for row in rows]
        # The reply that ends the run carries rows but no cursor, so once a run
        # has shown one it stays incremental.
        if getattr(rows, "incremental", False) or self._cursor is not None:
            self._cursor = getattr(rows, "cursor", None) or self._cursor
            self._table.extend(new)
        else:
            self._table = new
        table = self._table
        limit = self.menu.auto_limit
        if limit and len(table) > limit:
            table = table[-limit:]
        self._model.set_rows(table)
        if self.menu.auto_scroll and table:
            self.table.scrollToBottom()
        done = getattr(rows, "done", False) or self._run_finished(rows)
        status = self._device_status(rows)
        self._set_status(
            ("done  ·  " + status if status else "done") if done
            else (status or f"{len(table)} row(s)")
        )
        if done:
            self._finish()

    @staticmethod
    def _run_finished(rows) -> bool:
        """Whether the device says this run is over.

        A run that ends by itself -- Make Supout.rif at 100%, an upgrade, a
        file transfer -- keeps answering polls with the same shape and flips
        0xfe000b when there is nothing left to do. Without reading it the
        window polls a finished run for ever and never says it worked.
        """
        for row in rows:
            if (getattr(row, "values_by_code", None) or {}).get(QUERY_DONE):
                return True
        return False

    def _finish(self) -> None:
        """Stop polling a run the device has already finished.

        Deliberately not :meth:`stop`: cancelcmd is the Cancel button, and
        sending it after the fact asks the device to abort something it has
        already written.
        """
        self._timer.stop()
        self._session_id = None
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)

    def _device_status(self, rows) -> str:
        """The tool's own status text, when it sends one.

        Ping reports "1 of 1 packets received" in a field of the last row; the
        .jg marks those codes as statusbar entries.
        """
        codes = []
        for item in self.menu.statusbar or []:
            # A named entry keeps its name ("Min 3.952ms"): three bare
            # timings in a row do not say which is the minimum.
            label = None if item.get("withouttitle") else item.get("name")
            if item.get("kind") == "field" and isinstance(item.get("code"), int):
                codes.append((item["code"], label))
            for code in item.get("codes") or []:
                if isinstance(code, int):
                    codes.append((code, label))
        if not codes or not rows:
            return ""
        last = rows[-1]
        parts = []
        for code, label in codes:
            text = last.get_rendered(code)
            if isinstance(text, (bytes, bytearray)):
                text = text.decode("utf-8", "replace")
            if text and text not in ("—", "0"):
                parts.append(f"{label} {text}" if label else text)
        return "  ·  ".join(parts)

    def _on_error(self, msg: str) -> None:
        if self._session_id is None:
            return
        self._timer.stop()
        self._set_status(f"error: {msg}")
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._session_id = None

    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    def closeEvent(self, event) -> None:  # noqa: N802
        # Stop the run on the device rather than leaving it going.
        if self._session_id is not None:
            self.stop()
        self._timer.stop()
        super().closeEvent(event)
