"""Export calibration targets as multi-page, print-ready PDFs.

Page sizes use millimeters so printed output matches physical board / marker
dimensions where the model defines scale (ChArUco, ArUco). Chessboard targets
use A4 pages with the pattern centered at high resolution (no built-in size).
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QMarginsF, QRect, QSizeF, Qt
from PySide6.QtGui import QColor, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter

from caliscope.core.aruco_target import ArucoTarget
from caliscope.core.charuco import Charuco, derive_charuco_square_cm_and_margin_mm
from caliscope.core.chessboard import Chessboard
from caliscope.gui.utils.chessboard_preview import render_chessboard_pixmap

# Fixed margin for ArUco marker PDFs (image bundle, not grid-derived).
_ARUCO_CHART_MARGIN_MM = 5.0

# Must match :meth:`QPdfWriter.setResolution` below (used for ChArUco inner pattern sizing).
_PDF_EXPORT_DPI = 300


def _qimage_from_grayscale(arr: np.ndarray) -> QImage:
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, w, QImage.Format.Format_Grayscale8).copy()


def _qimage_from_bgr(arr: np.ndarray) -> QImage:
    arr = np.ascontiguousarray(arr)
    h, w, _ = arr.shape
    bytes_per_line = int(arr.strides[0])
    return QImage(arr.data, w, h, bytes_per_line, QImage.Format.Format_BGR888).copy()


def _margin_px(margin_mm: float, dpi: int) -> int:
    """Convert millimeters to device pixels at ``dpi`` dots per inch."""
    return max(0, int(round(float(margin_mm) * float(dpi) / 25.4)))


def _inset_content_rect(writer: QPdfWriter, margin_mm: float) -> QRect:
    """Return the content rectangle inset by ``margin_mm`` on all sides.

    Used for chessboard PDFs (no model-defined physical size); the chart is
    scaled to fit this rect.

    Qt's :meth:`QPageLayout.paintRectPixels` can round unevenly for PDF output,
    which shows up as missing margin on the right and bottom. We use zero layout
    margins and inset explicitly in device pixels so all four sides match.
    """
    m = _margin_px(margin_mm, writer.resolution())
    pw = writer.width()
    ph = writer.height()
    return QRect(m, m, max(1, pw - 2 * m), max(1, ph - 2 * m))


def _centered_1to1_chart_rect_mm(writer: QPdfWriter, chart_w_mm: float, chart_h_mm: float) -> QRect:
    """Pixel rectangle whose printed size is exactly ``chart_w_mm`` × ``chart_h_mm`` at writer DPI.

    The rect is centered on the page so borders match a page size of
    ``chart + 2 × margin`` in mm (within integer pixel quantization).
    """
    dpi = int(writer.resolution())
    cw = max(1, int(round(float(chart_w_mm) * dpi / 25.4)))
    ch = max(1, int(round(float(chart_h_mm) * dpi / 25.4)))
    pw = writer.width()
    ph = writer.height()
    x = max(0, (pw - cw) // 2)
    y = max(0, (ph - ch) // 2)
    return QRect(x, y, cw, ch)


def _write_pdf_two_pages(
    path: Path,
    *,
    title: str,
    page_w_mm: float,
    page_h_mm: float,
    margin_mm: float,
    draw_page,
    physical_chart_mm: tuple[float, float] | None = None,
) -> None:
    """Create a PDF with two pages using the same page geometry.

    draw_page: (painter, paint_rect) -> None, called once per page.

    If ``physical_chart_mm`` is set (ChArUco, ArUco), ``paint_rect`` is sized
    exactly from millimeters at the PDF resolution and centered (1:1 print
    scale independent of Qt page pixel rounding). Otherwise ``paint_rect``
    is the margin inset (chessboard).
    """
    writer = QPdfWriter(str(path))
    writer.setTitle(title)
    writer.setCreator("Caliscope")
    writer.setResolution(_PDF_EXPORT_DPI)

    page_size = QPageSize(QSizeF(page_w_mm, page_h_mm), QPageSize.Unit.Millimeter)
    layout = QPageLayout(
        page_size,
        QPageLayout.Orientation.Portrait,
        QMarginsF(0.0, 0.0, 0.0, 0.0),
        QPageLayout.Unit.Millimeter,
    )
    writer.setPageLayout(layout)

    painter = QPainter(writer)
    try:

        def _paint_rect() -> QRect:
            if physical_chart_mm is not None:
                w_mm, h_mm = physical_chart_mm
                return _centered_1to1_chart_rect_mm(writer, w_mm, h_mm)
            return _inset_content_rect(writer, margin_mm)

        paint_rect = _paint_rect()
        draw_page(painter, paint_rect)
        if not writer.newPage():
            msg = "Failed to add second PDF page"
            raise RuntimeError(msg)
        paint_rect = _paint_rect()
        draw_page(painter, paint_rect)
    finally:
        painter.end()


def save_charuco_board_pdf(
    path: str | Path,
    charuco: Charuco,
) -> None:
    """Two-page PDF: front board and horizontal mirror, 1:1 scale from board size (cm)."""
    path = Path(path)
    _, margin_mm = derive_charuco_square_cm_and_margin_mm(
        charuco.board_width,
        charuco.board_height,
        charuco.units,
        charuco.columns,
        charuco.rows,
    )
    margin_mm = float(margin_mm)
    bw_mm = float(charuco.board_width_cm) * 10.0
    bh_mm = float(charuco.board_height_cm) * 10.0
    page_w_mm = bw_mm + 2.0 * margin_mm
    page_h_mm = bh_mm + 2.0 * margin_mm

    front = charuco.board_img(pixmap_scale=10000)
    mirror = cv2.flip(front, 1)
    page_images = iter((_qimage_from_grayscale(front), _qimage_from_grayscale(mirror)))
    pat_w_mm, pat_h_mm = charuco.pattern_size_mm()
    dpi = float(_PDF_EXPORT_DPI)

    def draw_page(painter: QPainter, paint_rect) -> None:
        pw = max(1, int(round(pat_w_mm * dpi / 25.4)))
        ph = max(1, int(round(pat_h_mm * dpi / 25.4)))
        x = paint_rect.x() + (paint_rect.width() - pw) // 2
        y = paint_rect.y() + (paint_rect.height() - ph) // 2
        painter.fillRect(paint_rect, QColor(255, 255, 255))
        painter.drawImage(QRect(x, y, pw, ph), next(page_images))

    _write_pdf_two_pages(
        path,
        title="Caliscope ChArUco board",
        page_w_mm=page_w_mm,
        page_h_mm=page_h_mm,
        margin_mm=margin_mm,
        draw_page=draw_page,
        physical_chart_mm=(bw_mm, bh_mm),
    )


def save_chessboard_pdf(path: str | Path, chessboard: Chessboard, *, margin_mm: float = 12.0) -> None:
    """Two-page PDF on A4: identical chessboards centered (intrinsic has no physical scale)."""
    path = Path(path)
    margin_mm = float(margin_mm)

    pixmap = render_chessboard_pixmap(chessboard, 4000)
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB32)

    def draw_page(painter: QPainter, paint_rect) -> None:
        target = QSizeF(float(paint_rect.width()), float(paint_rect.height())).toSize()
        scaled = image.scaled(target, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        x = paint_rect.x() + (paint_rect.width() - scaled.width()) // 2
        y = paint_rect.y() + (paint_rect.height() - scaled.height()) // 2
        painter.drawImage(x, y, scaled)

    _write_pdf_two_pages(
        path,
        title="Caliscope chessboard",
        page_w_mm=210.0,
        page_h_mm=297.0,
        margin_mm=margin_mm,
        draw_page=draw_page,
    )


def save_aruco_marker_pdf(
    path: str | Path,
    target: ArucoTarget,
    marker_id: int,
    *,
    pixels_per_meter: int = 8000,
    margin_mm: float = _ARUCO_CHART_MARGIN_MM,
) -> None:
    """Two-page PDF: same printable marker on each page, size from image / pixels_per_meter."""
    path = Path(path)
    margin_mm = float(margin_mm)
    bgr = target.generate_marker_image(marker_id, pixels_per_meter=pixels_per_meter)
    h, w = bgr.shape[:2]
    ppm = float(pixels_per_meter)
    content_w_mm = (w / ppm) * 1000.0
    content_h_mm = (h / ppm) * 1000.0
    page_w_mm = content_w_mm + 2.0 * margin_mm
    page_h_mm = content_h_mm + 2.0 * margin_mm

    qimg = _qimage_from_bgr(bgr)

    def draw_page(painter: QPainter, paint_rect) -> None:
        painter.drawImage(paint_rect, qimg)

    _write_pdf_two_pages(
        path,
        title="Caliscope ArUco marker",
        page_w_mm=page_w_mm,
        page_h_mm=page_h_mm,
        margin_mm=margin_mm,
        draw_page=draw_page,
        physical_chart_mm=(content_w_mm, content_h_mm),
    )
