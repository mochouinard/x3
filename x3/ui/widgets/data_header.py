"""Header for the data tables: labels that line up with the column beneath.

Right-aligning a numeric column is only half the job — its heading has to move
with it, and that runs straight into how Qt places the sort arrow:

* The arrow is drawn over the right end of the section, and with the header
  under a stylesheet nothing reserves that lane for the label. A left-aligned
  heading never notices; a right-aligned one ends up with a triangle sitting on
  its last letter.
* ``QHeaderView`` pays for the lane anyway: while a sort indicator is shown it
  adds a whole section height to *every* section's size hint, sorted or not.
  On a content-fitted table that is the single widest piece of dead space in
  the list -- ~19px per column, in front of columns whose data is 30px wide.

So this header reserves the lane the arrow actually occupies instead of Qt's
much larger one, and keeps a right-aligned label out of it when the arrow is
really there. Nothing changes for a left-aligned heading, which is every column
that holds text.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView, QStyle, QStyleOptionHeader

from .. import style

#: Painting pass, for the two-pass path in :meth:`DataHeaderView.paintSection`.
_CHROME = 1  # section box + sort arrow, no label
_LABEL = 2  # section box + label, no sort arrow


class DataHeaderView(QHeaderView):
    """Horizontal header that keeps a right-aligned label off the sort arrow."""

    def __init__(self, parent=None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._pass = 0

    # -- geometry ---------------------------------------------------------

    def _margin(self) -> int:
        return self.style().pixelMetric(QStyle.PixelMetric.PM_HeaderMargin, None, self)

    def arrow_lane(self, height: int) -> int:
        """Width the sort arrow claims at the right end of a section.

        Mirrors ``QCommonStyle``'s own SE_HeaderArrow — half the section height
        plus its margins — with a cell's worth of air in front of it so a label
        stopped here does not touch the triangle.
        """
        return max(0, height) // 2 + 2 * self._margin() + int(style.CELL_PAD)

    def _right_aligned(self, logical_index: int) -> bool:
        model = self.model()
        if model is None:
            return False
        align = model.headerData(
            logical_index, self.orientation(), Qt.ItemDataRole.TextAlignmentRole
        )
        return bool(align) and bool(int(align) & int(Qt.AlignmentFlag.AlignRight))

    def sectionSizeFromContents(self, logicalIndex):  # noqa: N802
        size = super().sectionSizeFromContents(logicalIndex)
        if self.isSortIndicatorShown():
            # Undo the section-height lane Qt just added (qheaderview.cpp adds
            # `size.height() + margin` to the width) and put back the narrower
            # one the arrow is drawn in.
            height = self.height() or (size.height() + 2 * self._margin())
            reserved = size.height() + self._margin()
            size.setWidth(max(1, size.width() - reserved + self.arrow_lane(height)))
        return size

    # -- painting ---------------------------------------------------------

    def initStyleOptionForIndex(self, option, logicalIndex) -> None:  # noqa: N802
        super().initStyleOptionForIndex(option, logicalIndex)
        # Which half of the two-pass paint we are in. Both passes draw the same
        # section box, so the label pass simply covers the chrome pass again.
        if self._pass == _CHROME:
            option.text = ""
        elif self._pass == _LABEL:
            option.sortIndicator = QStyleOptionHeader.SortIndicator.None_

    def paintSection(self, painter, rect, logicalIndex) -> None:  # noqa: N802
        if (
            not rect.isValid()
            or not self.isSortIndicatorShown()
            or logicalIndex != self.sortIndicatorSection()
            or not self._right_aligned(logicalIndex)
        ):
            super().paintSection(painter, rect, logicalIndex)
            return

        lane = self.arrow_lane(rect.height())
        if lane >= rect.width():
            super().paintSection(painter, rect, logicalIndex)
            return

        # The section and its arrow, at full width, with the label withheld...
        self._pass = _CHROME
        try:
            super().paintSection(painter, rect, logicalIndex)
        finally:
            self._pass = 0

        # ...then the label alone, right-aligned to where the lane begins.
        # CE_HeaderLabel is only the text: no box, no border, nothing that
        # would have to be clipped back off the section just drawn.
        opt = QStyleOptionHeader()
        self.initStyleOption(opt)
        self.initStyleOptionForIndex(opt, logicalIndex)
        opt.rect = rect.adjusted(0, 0, -lane, 0)
        self.style().drawControl(
            QStyle.ControlElement.CE_HeaderLabel, opt, painter, self
        )
