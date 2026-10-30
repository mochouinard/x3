"""Tabbed property / settings dialog (read-only capable)."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import a11y, style


class PropertyDialog(QDialog):
    def __init__(
        self,
        title: str,
        fields: tuple[tuple[str, str, Any], ...],
        parent=None,
        *,
        overrides: dict[str, Any] | None = None,
        readonly: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title + (" [read-only]" if readonly else ""))
        self.setModal(True)
        self.setMinimumWidth(style.scale_px(380))
        self.resize(style.scale_px(460), style.scale_px(360))
        self.readonly = readonly
        self._editors: dict[str, QWidget] = {}
        overrides = overrides or {}

        tabs_data: OrderedDict[str, list[tuple[str, Any]]] = OrderedDict()
        for tab_name, label, value in fields:
            if label in overrides:
                value = overrides[label]
            tabs_data.setdefault(tab_name, []).append((label, value))

        root = QVBoxLayout(self)
        root.setContentsMargins(style.MARGIN + 2, style.MARGIN + 2, style.MARGIN + 2, style.MARGIN + 2)
        root.setSpacing(style.SPACING + 2)

        if not tabs_data:
            root.addWidget(QLabel("(no fields)"))
        elif len(tabs_data) == 1:
            root.addWidget(self._form_page(next(iter(tabs_data.values()))))
        else:
            tabs = QTabWidget()
            for tab_name, items in tabs_data.items():
                tabs.addTab(self._form_page(items), tab_name)
            root.addWidget(tabs)

        if readonly:
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.reject)
            buttons.accepted.connect(self.accept)
            buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        else:
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel
                | QDialogButtonBox.StandardButton.Apply
            )
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._on_apply)
        root.addWidget(buttons)

    def _form_page(self, items: list[tuple[str, Any]]) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(6, 6, 6, 6)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(3)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        for label, value in items:
            editor = self._make_editor(label, value)
            self._editors[label] = editor
            form.addRow(label, editor)
            a11y.label_editor(editor, label)
        return page

    def _make_editor(self, label: str, value: Any) -> QWidget:
        if isinstance(value, bool):
            cb = QCheckBox()
            cb.setChecked(value)
            cb.setEnabled(not self.readonly)
            return cb
        if isinstance(value, int) and not isinstance(value, bool):
            if self.readonly:
                edit = QLineEdit(str(value))
                edit.setReadOnly(True)
                return edit
            spin = QSpinBox()
            spin.setRange(-2147483648, 2147483647)
            spin.setValue(value)
            spin.setMaximumWidth(120)
            return spin
        edit = QLineEdit("" if value is None else str(value))
        edit.setReadOnly(self.readonly)
        return edit

    def _on_apply(self) -> None:
        QMessageBox.information(self, "Apply", "Write support is not enabled.")

    def values(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for label, w in self._editors.items():
            if isinstance(w, QCheckBox):
                out[label] = w.isChecked()
            elif isinstance(w, QSpinBox):
                out[label] = w.value()
            elif isinstance(w, QLineEdit):
                out[label] = w.text()
        return out
