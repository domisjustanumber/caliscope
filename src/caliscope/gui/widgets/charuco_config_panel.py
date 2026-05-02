"""Reusable charuco board configuration widget.

Extracted from CharucoWidget for embedding in ProjectSetupView and
potential future use in other tabs (Intrinsics, Landmarks).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from caliscope.core.charuco import Charuco, derive_charuco_square_cm_and_margin_mm
from caliscope.gui.utils.paper_sizes import (
    PRESET_BY_KEY,
    charuco_fields_for_preset,
    find_matching_preset,
    paper_presets_for_region,
)
from caliscope.gui.utils.spinbox_utils import setup_spinbox_sizing

logger = logging.getLogger(__name__)


class CharucoConfigPanel(QWidget):
    """Reusable charuco board configuration widget.

    Emits `config_changed` whenever any configuration value is modified.
    Use `get_charuco()` to build a Charuco instance from current values.

    Layout: Vertical stack of rows
    - Row 1: Board Shape: [rows] x [cols]
    - Row 2: Print page: preset dropdown (+ Custom)
    - Row 3: Board Size: [width] x [height] [units] (enabled only for Custom)
    - Row 4: Invert checkbox
    - Row 5: Square size (read-only, one decimal cm)

    This widget does NOT contain:
    - Board preview image (responsibility of the parent view)
    - PDF export buttons (responsibility of the parent view)
    """

    config_changed = Signal()

    def __init__(
        self,
        initial_charuco: Charuco,
        parent: QWidget | None = None,
    ) -> None:
        """Initialize the panel with values from an existing Charuco.

        Args:
            initial_charuco: Charuco instance to populate initial widget values
            parent: Optional parent widget
        """
        super().__init__(parent)
        self._charuco_params = self._extract_params(initial_charuco)
        self._paper_presets = paper_presets_for_region()
        self._preset_keys = {p.key for p in self._paper_presets}
        self._setup_ui()
        self._connect_signals()

    def _extract_params(self, charuco: Charuco) -> dict:
        """Extract configuration parameters from a Charuco instance."""
        return {
            "columns": charuco.columns,
            "rows": charuco.rows,
            "board_width": charuco.board_width,
            "board_height": charuco.board_height,
            "units": charuco.units,
            "inverted": charuco.inverted,
            "dictionary": charuco.dictionary,
            "aruco_scale": charuco.aruco_scale,
            "legacy_pattern": charuco.legacy_pattern,
        }

    def _setup_ui(self) -> None:
        """Build the widget layout with vertically stacked rows."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)  # We'll use explicit spacing between rows

        # Row 1: Board Shape: [rows] x [cols]
        shape_row = QHBoxLayout()
        shape_row.setAlignment(Qt.AlignmentFlag.AlignLeft)

        shape_row.addWidget(QLabel("Board Shape:"))

        self._row_spin = QSpinBox()
        self._row_spin.setValue(self._charuco_params["rows"])
        setup_spinbox_sizing(self._row_spin, min_value=4, max_value=999, padding=10)
        shape_row.addWidget(self._row_spin)

        shape_row.addWidget(QLabel("x"))

        self._column_spin = QSpinBox()
        self._column_spin.setValue(self._charuco_params["columns"])
        setup_spinbox_sizing(self._column_spin, min_value=3, max_value=999, padding=10)
        shape_row.addWidget(self._column_spin)

        shape_row.addStretch()
        main_layout.addLayout(shape_row)

        # 12px spacing between rows (style guide: row-to-row spacing)
        main_layout.addSpacing(12)

        # Row 2: Print page preset
        page_row = QHBoxLayout()
        page_row.setAlignment(Qt.AlignmentFlag.AlignLeft)
        page_row.addWidget(QLabel("Print page:"))
        self._page_size_combo = QComboBox()
        for p in self._paper_presets:
            self._page_size_combo.addItem(p.label, p.key)
        self._page_size_combo.addItem("Custom", "custom")
        page_row.addWidget(self._page_size_combo, stretch=1)
        main_layout.addLayout(page_row)

        main_layout.addSpacing(12)

        # Row 3: Board Size: [width] x [height] [units] (Custom only)
        size_row = QHBoxLayout()
        size_row.setAlignment(Qt.AlignmentFlag.AlignLeft)

        size_row.addWidget(QLabel("Board size:"))

        self._width_spin = QDoubleSpinBox()
        self._width_spin.setValue(self._charuco_params["board_width"])
        setup_spinbox_sizing(self._width_spin, min_value=1, max_value=10000, padding=10)
        size_row.addWidget(self._width_spin)

        size_row.addWidget(QLabel("x"))

        self._height_spin = QDoubleSpinBox()
        self._height_spin.setValue(self._charuco_params["board_height"])
        setup_spinbox_sizing(self._height_spin, min_value=1, max_value=10000, padding=10)
        size_row.addWidget(self._height_spin)

        self._units_combo = QComboBox()
        self._units_combo.addItems(["cm", "inch"])
        self._units_combo.setCurrentText(self._charuco_params["units"])
        size_row.addWidget(self._units_combo)

        size_row.addStretch()
        main_layout.addLayout(size_row)

        main_layout.addSpacing(12)

        # Row 4: Invert checkbox
        invert_row = QHBoxLayout()
        invert_row.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self._invert_checkbox = QCheckBox("&Invert")
        self._invert_checkbox.setChecked(self._charuco_params["inverted"])
        invert_row.addWidget(self._invert_checkbox)

        invert_row.addStretch()
        main_layout.addLayout(invert_row)

        main_layout.addSpacing(12)

        # Row 5: Square size (derived for PDF layout)
        edge_row = QHBoxLayout()
        edge_row.setAlignment(Qt.AlignmentFlag.AlignLeft)

        edge_row.addWidget(QLabel("Square size (corner to corner):"))
        self._square_size_label = QLabel()
        edge_row.addWidget(self._square_size_label, stretch=1)

        edge_row.addStretch()
        main_layout.addLayout(edge_row)

        main_layout.addStretch()

        self._sync_page_size_combo_from_dimensions()
        self._update_square_size_display()

    def _square_layout(self) -> tuple[float, float]:
        return derive_charuco_square_cm_and_margin_mm(
            self._width_spin.value(),
            self._height_spin.value(),
            self._units_combo.currentText(),
            self._column_spin.value(),
            self._row_spin.value(),
        )

    def _computed_square_edge_cm(self) -> float:
        return float(self._square_layout()[0])

    def _update_square_size_display(self) -> None:
        cm, _mm = self._square_layout()
        self._square_size_label.setText(f"{cm:.1f} cm")

    def sync_computed_square_to_model(self) -> None:
        """Refresh the square-size label and push ``get_charuco()`` to listeners (after signals are wired)."""
        self._update_square_size_display()
        self.config_changed.emit()

    def _custom_combo_index(self) -> int:
        return self._page_size_combo.count() - 1

    def _sync_page_size_combo_from_dimensions(self) -> None:
        """Select Custom or the matching preset after width/height/units are updated."""
        with QSignalBlocker(self._page_size_combo):
            matched = find_matching_preset(
                self._width_spin.value(),
                self._height_spin.value(),
                self._units_combo.currentText(),
            )
            if matched is not None and matched.key in self._preset_keys:
                idx = self._page_size_combo.findData(matched.key)
                if idx >= 0:
                    self._page_size_combo.setCurrentIndex(idx)
                else:
                    self._page_size_combo.setCurrentIndex(self._custom_combo_index())
            else:
                self._page_size_combo.setCurrentIndex(self._custom_combo_index())
        self._update_dimension_controls_enabled()

    def _update_dimension_controls_enabled(self) -> None:
        custom = self._page_size_combo.currentData() == "custom"
        self._width_spin.setEnabled(custom)
        self._height_spin.setEnabled(custom)
        self._units_combo.setEnabled(custom)

    def _on_page_size_changed(self, _index: int) -> None:
        key = self._page_size_combo.currentData()
        if key == "custom":
            self._update_dimension_controls_enabled()
            self._on_config_changed()
            return
        paper = PRESET_BY_KEY[str(key)]
        bw, bh, units = charuco_fields_for_preset(paper)
        self._width_spin.blockSignals(True)
        self._height_spin.blockSignals(True)
        self._units_combo.blockSignals(True)
        self._width_spin.setValue(bw)
        self._height_spin.setValue(bh)
        self._units_combo.setCurrentText(units)
        self._width_spin.blockSignals(False)
        self._height_spin.blockSignals(False)
        self._units_combo.blockSignals(False)
        self._update_dimension_controls_enabled()
        self._on_config_changed()

    def _connect_signals(self) -> None:
        """Connect widget signals to emit config_changed."""
        self._row_spin.valueChanged.connect(self._on_config_changed)
        self._column_spin.valueChanged.connect(self._on_config_changed)
        self._width_spin.valueChanged.connect(self._on_config_changed)
        self._height_spin.valueChanged.connect(self._on_config_changed)
        self._units_combo.currentIndexChanged.connect(self._on_config_changed)
        self._page_size_combo.currentIndexChanged.connect(self._on_page_size_changed)
        self._invert_checkbox.stateChanged.connect(self._on_config_changed)

    def _on_config_changed(self) -> None:
        """Handle any configuration change."""
        self._update_square_size_display()
        self.config_changed.emit()

    def get_charuco(self) -> Charuco:
        """Build a Charuco instance from current widget values.

        Returns:
            New Charuco with configuration from this panel
        """
        return Charuco(
            columns=self._column_spin.value(),
            rows=self._row_spin.value(),
            board_height=self._height_spin.value(),
            board_width=self._width_spin.value(),
            units=self._units_combo.currentText(),
            dictionary=self._charuco_params["dictionary"],
            aruco_scale=self._charuco_params["aruco_scale"],
            square_size_override_cm=self._computed_square_edge_cm(),
            inverted=self._invert_checkbox.isChecked(),
            legacy_pattern=self._charuco_params["legacy_pattern"],
        )

    def set_values(self, charuco: Charuco) -> None:
        """Repopulate panel with values from a charuco instance.

        Used when syncing the same-as-intrinsic extrinsic panel from
        the intrinsic charuco config.

        Args:
            charuco: Charuco instance to populate widget values from
        """
        # Block signals during bulk update to avoid spurious config_changed emissions
        self._row_spin.blockSignals(True)
        self._column_spin.blockSignals(True)
        self._width_spin.blockSignals(True)
        self._height_spin.blockSignals(True)
        self._units_combo.blockSignals(True)
        self._invert_checkbox.blockSignals(True)
        self._page_size_combo.blockSignals(True)

        self._row_spin.setValue(charuco.rows)
        self._column_spin.setValue(charuco.columns)
        self._width_spin.setValue(charuco.board_width)
        self._height_spin.setValue(charuco.board_height)
        self._units_combo.setCurrentText(charuco.units)
        self._invert_checkbox.setChecked(charuco.inverted)

        self._row_spin.blockSignals(False)
        self._column_spin.blockSignals(False)
        self._width_spin.blockSignals(False)
        self._height_spin.blockSignals(False)
        self._units_combo.blockSignals(False)
        self._invert_checkbox.blockSignals(False)
        self._page_size_combo.blockSignals(False)

        self._sync_page_size_combo_from_dimensions()
        self._update_square_size_display()

        # Update internal params cache (for immutable fields like dictionary)
        self._charuco_params = self._extract_params(charuco)
