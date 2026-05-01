"""Shared charuco board preview rendering utility.

Used by ProjectSetupView and CamerasTabWidget to render charuco boards
as QPixmap without coupling the domain model to Qt more than it already is.

Note: Charuco.board_img() is a Qt-free method that generates the board
using OpenCV. This utility handles the OpenCV -> Qt conversion.

Preview geometry matches :func:`calibration_target_pdf.save_charuco_board_pdf`:
full page uses symmetric margins; the ChArUco bitmap is only the grid,
centered on the board rectangle (same as calibration / :attr:`Charuco.board`).
"""

import cv2
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap

from caliscope.core.charuco import Charuco, derive_charuco_square_cm_and_margin_mm


def render_charuco_pixmap(charuco: Charuco, max_dimension: int) -> QPixmap:
    """Render charuco board as a QPixmap for preview display.

    Includes the same printable margins as the ChArUco PDF export so the
    preview matches the page layout. Uses Charuco.board_img() (OpenCV)
    and converts to QPixmap. Scales to fit within max_dimension while
    maintaining the page aspect ratio.

    Args:
        charuco: The charuco board to render
        max_dimension: Maximum width or height in pixels

    Returns:
        QPixmap scaled to fit within max_dimension
    """
    _, margin_mm = derive_charuco_square_cm_and_margin_mm(
        charuco.board_width,
        charuco.board_height,
        charuco.units,
        charuco.columns,
        charuco.rows,
    )
    bw_mm = float(charuco.board_width_cm) * 10.0
    bh_mm = float(charuco.board_height_cm) * 10.0
    margin_mm = float(margin_mm)
    page_w_mm = bw_mm + 2.0 * margin_mm
    page_h_mm = bh_mm + 2.0 * margin_mm

    fit = max(page_w_mm, page_h_mm)
    page_px_w = max(1, int(round(max_dimension * (page_w_mm / fit))))
    page_px_h = max(1, int(round(max_dimension * (page_h_mm / fit))))

    chart_w = max(1, int(round(bw_mm / page_w_mm * page_px_w)))
    chart_h = max(1, int(round(bh_mm / page_h_mm * page_px_h)))
    mx = (page_px_w - chart_w) // 2
    my = (page_px_h - chart_h) // 2

    pat_w_mm, pat_h_mm = charuco.pattern_size_mm()
    pat_px_w = max(1, int(round(pat_w_mm / bw_mm * chart_w)))
    pat_px_h = max(1, int(round(pat_h_mm / bh_mm * chart_h)))
    ox = mx + (chart_w - pat_px_w) // 2
    oy = my + (chart_h - pat_px_h) // 2

    inner_scale = max(pat_px_w, pat_px_h, 32)
    img = charuco.board_img(pixmap_scale=inner_scale)
    img = cv2.resize(img, (pat_px_w, pat_px_h), interpolation=cv2.INTER_AREA)

    canvas = np.full((page_px_h, page_px_w), 255, dtype=np.uint8)
    canvas[oy : oy + pat_px_h, ox : ox + pat_px_w] = img

    rgb = np.ascontiguousarray(cv2.cvtColor(canvas, cv2.COLOR_GRAY2RGB))
    h, w, ch = rgb.shape
    bytes_per_line = ch * w
    qimage = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)

    return QPixmap.fromImage(qimage.copy()).scaled(
        max_dimension,
        max_dimension,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
