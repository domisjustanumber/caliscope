"""Tests for print page preset filtering (no GUI)."""

from caliscope.gui.utils.paper_sizes import PRESET_BY_KEY, paper_presets_for_region, preset_area_mm2


def test_min_area_letter_includes_a4_and_legal() -> None:
    floor = preset_area_mm2(PRESET_BY_KEY["letter"])
    presets = paper_presets_for_region(min_area_mm2=floor)
    keys = [p.key for p in presets]
    assert keys == sorted(keys, key=lambda k: preset_area_mm2(PRESET_BY_KEY[k]))
    assert "letter" in keys
    assert "a4" in keys
    assert "legal" in keys


def test_min_area_a4_excludes_letter() -> None:
    floor = preset_area_mm2(PRESET_BY_KEY["a4"])
    presets = paper_presets_for_region(min_area_mm2=floor)
    keys = {p.key for p in presets}
    assert "a4" in keys
    assert "letter" not in keys
    assert "legal" in keys
