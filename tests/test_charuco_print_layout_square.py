"""ChArUco square size (one decimal cm) and derived PDF margin."""

from caliscope.core.charuco import (
    Charuco,
    compute_square_edge_cm_for_print_layout,
    derive_charuco_square_cm_and_margin_mm,
)


def _assert_one_decimal_cm(sq: float) -> None:
    assert abs(round(sq, 1) - sq) < 1e-9


def test_default_letter_board_square_and_margin() -> None:
    sq, mm = derive_charuco_square_cm_and_margin_mm(8.5, 11.0, "inch", 4, 5)
    _assert_one_decimal_cm(sq)
    assert 5.0 <= sq <= 5.5
    assert mm >= 5.0 - 1e-6


def test_compute_wrapper_matches_derive() -> None:
    sq1 = compute_square_edge_cm_for_print_layout(8.5, 11.0, "inch", 4, 5)
    sq2, _mm = derive_charuco_square_cm_and_margin_mm(8.5, 11.0, "inch", 4, 5)
    assert sq1 == sq2


def test_a4_cm_board() -> None:
    sq, mm = derive_charuco_square_cm_and_margin_mm(21.0, 29.7, "cm", 4, 5)
    _assert_one_decimal_cm(sq)
    assert sq > 4.0
    assert mm >= 5.0 - 1e-6


def test_board_img_matches_grid_aspect_no_letterbox() -> None:
    """board_img output aspect is columns : rows so the raster is tight on the grid."""
    ch = Charuco(4, 5, 11.0, 8.5, units="inch", square_size_override_cm=5.2)
    img = ch.board_img(pixmap_scale=400)
    h, w = img.shape[:2]
    assert abs(w / h - ch.columns / ch.rows) < 0.06


def test_pattern_size_mm_matches_grid() -> None:
    ch = Charuco(4, 5, 11.0, 8.5, units="inch", square_size_override_cm=5.2)
    pw, ph = ch.pattern_size_mm()
    assert abs(pw - ch.columns * 5.2 * 10.0) < 1e-6
    assert abs(ph - ch.rows * 5.2 * 10.0) < 1e-6
