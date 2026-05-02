"""Standard print page sizes for calibration board dimensions (ChArUco).

Regional defaults follow common practice: Letter for the United States and Canada,
A4 elsewhere. Only presets at least as large as that default (by area) are offered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_LETTER_DEFAULT_COUNTRIES_CACHE: frozenset[Any] | None = None


def _get_letter_default_countries() -> frozenset[Any]:
    """Countries that use US Letter as the typical office paper size."""
    global _LETTER_DEFAULT_COUNTRIES_CACHE
    if _LETTER_DEFAULT_COUNTRIES_CACHE is None:
        from PySide6.QtCore import QLocale

        items: set[Any] = {
            QLocale.Country.UnitedStates,
            QLocale.Country.Canada,
        }
        for name in ("PuertoRico", "UnitedStatesMinorOutlyingIslands", "UnitedStatesVirginIslands"):
            c = getattr(QLocale.Country, name, None)
            if c is not None:
                items.add(c)
        _LETTER_DEFAULT_COUNTRIES_CACHE = frozenset(items)
    return _LETTER_DEFAULT_COUNTRIES_CACHE


@dataclass(frozen=True, slots=True)
class PaperPreset:
    """A named ISO or ANSI sheet in portrait orientation (short × long mm)."""

    key: str
    label: str
    width_mm: float
    height_mm: float


# All presets we support (portrait mm; width is the shorter edge).
_PRESETS: tuple[PaperPreset, ...] = (
    PaperPreset("a4", "A4 (210 × 297 mm)", 210.0, 297.0),
    PaperPreset("a3", "A3 (297 × 420 mm)", 297.0, 420.0),
    PaperPreset("a2", "A2 (420 × 594 mm)", 420.0, 594.0),
    PaperPreset("a1", "A1 (594 × 841 mm)", 594.0, 841.0),
    PaperPreset("a0", "A0 (841 × 1189 mm)", 841.0, 1189.0),
    PaperPreset("letter", "Letter (8.5 × 11 in)", 215.9, 279.4),
    PaperPreset("legal", "Legal (8.5 × 14 in)", 215.9, 355.6),
    PaperPreset("tabloid", "Tabloid (11 × 17 in)", 279.4, 431.8),
)

PRESET_BY_KEY: dict[str, PaperPreset] = {p.key: p for p in _PRESETS}


def preset_area_mm2(paper: PaperPreset) -> float:
    return paper.width_mm * paper.height_mm


def uses_letter_default_region() -> bool:
    """True if the system locale uses Letter as the default minimum size."""
    try:
        from PySide6.QtCore import QLocale
    except ImportError:
        return False
    return QLocale.system().country() in _get_letter_default_countries()


def regional_minimum_area_mm2() -> float:
    """Smallest sheet area (mm²) used as the floor for the preset list."""
    letter = PRESET_BY_KEY["letter"]
    a4 = PRESET_BY_KEY["a4"]
    return preset_area_mm2(letter) if uses_letter_default_region() else preset_area_mm2(a4)


def paper_presets_for_region(*, min_area_mm2: float | None = None) -> list[PaperPreset]:
    """Presets with area >= regional default (or ``min_area_mm2``), sorted by increasing area."""
    floor = regional_minimum_area_mm2() if min_area_mm2 is None else min_area_mm2
    eligible = [p for p in _PRESETS if preset_area_mm2(p) + 1e-6 >= floor]
    return sorted(eligible, key=preset_area_mm2)


def board_edges_mm(board_width: float, board_height: float, units: str) -> tuple[float, float]:
    """Return (short_mm, long_mm) for the board rectangle."""
    if units == "inch":
        wmm = board_width * 25.4
        hmm = board_height * 25.4
    else:
        wmm = board_width * 10.0
        hmm = board_height * 10.0
    return (min(wmm, hmm), max(wmm, hmm))


def find_matching_preset(
    board_width: float,
    board_height: float,
    units: str,
    *,
    tol_mm: float = 1.5,
) -> PaperPreset | None:
    """Return the preset that matches board dimensions, if any."""
    w0, h0 = board_edges_mm(board_width, board_height, units)
    for p in _PRESETS:
        pw, ph = min(p.width_mm, p.height_mm), max(p.width_mm, p.height_mm)
        if abs(w0 - pw) <= tol_mm and abs(h0 - ph) <= tol_mm:
            return p
    return None


def charuco_fields_for_preset(paper: PaperPreset) -> tuple[float, float, str]:
    """Return (board_width, board_height, units) for :class:`~caliscope.core.charuco.Charuco`."""
    if paper.key in ("letter", "legal", "tabloid"):
        w_in = paper.width_mm / 25.4
        h_in = paper.height_mm / 25.4
        return (w_in, h_in, "inch")
    w_cm = paper.width_mm / 10.0
    h_cm = paper.height_mm / 10.0
    return (w_cm, h_cm, "cm")
