from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from options_matrix.app import (
    MATRIX_WIDTH_BANDS,
    CachedMatrixTab,
    CachedMatrixWorkspace,
    OptionsMatrixApp,
    load_width_band_preferences,
    parse_matrix_input_state,
    save_width_band_preferences,
    width_preferences_config_path,
)


class _Value:
    def __init__(self, value: bool) -> None:
        self.value = value

    def get(self) -> bool:
        return self.value

    def set(self, value: bool) -> None:
        self.value = bool(value)


class _Button:
    state = ""

    def configure(self, *, state: str) -> None:
        self.state = state


class _Controls:
    _refresh_width_band_controls = OptionsMatrixApp._refresh_width_band_controls
    _remember_width_band_preferences = OptionsMatrixApp._remember_width_band_preferences
    _on_matrix_input_changed = OptionsMatrixApp._on_matrix_input_changed
    _on_width_band_changed = OptionsMatrixApp._on_width_band_changed

    def __init__(self, preferences: dict[float, bool], path: Path) -> None:
        self._width_band_preferences = dict(preferences)
        self._width_preferences_path = path
        self.width_vars = {band: _Value(selected) for band, selected in preferences.items()}
        self.width_checkbuttons = {band: _Button() for band in MATRIX_WIDTH_BANDS}
        self.available_width_bands = ()
        self.availability = None
        self.status = ""

    def _staged_width_band_availability(self):
        return self.availability

    def _refresh_staged_market_data_quote_status(self) -> None:
        pass

    def _set_status(self, message: str) -> None:
        self.status = message

    def checked(self) -> tuple[float, ...]:
        return tuple(band for band, value in self.width_vars.items() if value.get())


def _preferences(*selected: float) -> dict[float, bool]:
    return {band: band in selected for band in MATRIX_WIDTH_BANDS}


def _cached_workspace() -> CachedMatrixWorkspace:
    return CachedMatrixWorkspace(
        ticker_symbol="TEST",
        tabs=(
            CachedMatrixTab("20300117", "CALL", 100.0, 110.0, (1.0, 2.0)),
            CachedMatrixTab("20300215", "PUT", 100.0, 110.0, (5.0,)),
        ),
        active_tab_identity=("TEST", "20300215", "PUT"),
    )


class WidthPreferenceTests(unittest.TestCase):
    def test_new_install_defaults_to_one_and_two_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = width_preferences_config_path(Path(directory) / "settings.json")
            self.assertEqual(path, Path(directory) / "width_preferences.json")
            self.assertEqual(load_width_band_preferences(path), _preferences(1.0, 2.0))
            self.assertFalse(path.exists())

    def test_legacy_active_cache_migrates_only_when_preference_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "width_preferences.json"
            workspace = _cached_workspace()
            self.assertEqual(load_width_band_preferences(path, cached_workspace=workspace), _preferences(5.0))
            self.assertFalse(path.exists())
            save_width_band_preferences(_preferences(1.0, 2.0), path)
            self.assertEqual(load_width_band_preferences(path, cached_workspace=workspace), _preferences(1.0, 2.0))
            save_width_band_preferences(_preferences(), path)
            self.assertEqual(load_width_band_preferences(path, cached_workspace=workspace), _preferences())

    def test_invalid_preferences_use_defaults_without_reenabling_cached_wider_bands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "width_preferences.json"
            for contents in (
                "invalid json", "[]", '{"version": 2, "selected_bands": [5]}',
                '{"version": 1, "selected_bands": [true, 5]}',
                '{"version": 1, "selected_bands": [1, 99]}',
            ):
                with self.subTest(contents=contents):
                    path.write_text(contents, encoding="utf-8")
                    self.assertEqual(
                        load_width_band_preferences(path, cached_workspace=_cached_workspace()),
                        _preferences(1.0, 2.0),
                    )

    def test_chain_availability_never_forces_five_or_overwrites_choices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "width_preferences.json"
            controls = _Controls(_preferences(1.0, 2.0), path)
            for available, checked in (
                (MATRIX_WIDTH_BANDS, (1.0, 2.0)),
                (None, (1.0, 2.0)),
                ((5.0,), ()),
                ((), ()),
                ((2.0,), (2.0,)),
                (MATRIX_WIDTH_BANDS, (1.0, 2.0)),
            ):
                with self.subTest(available=available):
                    controls.availability = available
                    controls._on_matrix_input_changed()
                    self.assertEqual(controls.checked(), checked)
                    self.assertEqual(controls._width_band_preferences, _preferences(1.0, 2.0))
                    self.assertFalse(path.exists())
                    for band, button in controls.width_checkbuttons.items():
                        self.assertEqual(button.state, "normal" if available and band in available else "disabled")

    def test_manual_choice_saves_immediately_and_survives_restart_without_loading_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "width_preferences.json"
            controls = _Controls(_preferences(1.0, 2.0), path)
            controls.availability = MATRIX_WIDTH_BANDS
            controls._refresh_width_band_controls()
            controls.width_vars[5.0].set(True)
            controls._on_width_band_changed()
            self.assertEqual(load_width_band_preferences(path), _preferences(1.0, 2.0, 5.0))
            controls.width_vars[5.0].set(False)
            controls.width_vars[1.0].set(False)
            controls._on_width_band_changed()
            self.assertEqual(load_width_band_preferences(path), _preferences(2.0))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["selected_bands"], [2.0])
            self.assertEqual([file.name for file in Path(directory).iterdir()], ["width_preferences.json"])

    def test_manual_edit_preserves_preferences_for_temporarily_unavailable_bands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "width_preferences.json"
            controls = _Controls(_preferences(1.0, 2.0, 5.0), path)
            controls.availability = (2.0,)
            controls._refresh_width_band_controls()
            controls.width_vars[2.0].set(False)
            controls._on_width_band_changed()
            self.assertEqual(load_width_band_preferences(path), _preferences(1.0, 5.0))
            controls.availability = MATRIX_WIDTH_BANDS
            controls._refresh_width_band_controls()
            self.assertEqual(controls.checked(), (1.0, 5.0))

    def test_staging_cached_or_reloaded_actual_widths_does_not_replace_global_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "width_preferences.json"
            controls = _Controls(_preferences(1.0, 2.0), path)
            matrix_input = parse_matrix_input_state(
                display_expiration="2030-01-17", raw_expiration="20300117", side="CALL",
                low_strike="100", high_strike="110", width_states=_preferences(5.0),
            )
            OptionsMatrixApp._stage_width_band_preferences(controls, matrix_input)
            self.assertEqual(controls.checked(), (1.0, 2.0))
            self.assertEqual(controls._width_band_preferences, _preferences(1.0, 2.0))
            self.assertFalse(path.exists())

    def test_save_failure_retains_in_memory_choice_and_reports_failure(self) -> None:
        controls = _Controls(_preferences(1.0, 2.0), Path("unused-preferences.json"))
        controls.availability = MATRIX_WIDTH_BANDS
        controls._refresh_width_band_controls()
        controls.width_vars[5.0].set(True)
        with patch("options_matrix.app.save_width_band_preferences", side_effect=OSError("read only")):
            controls._on_width_band_changed()
        self.assertTrue(controls._width_band_preferences[5.0])
        self.assertIn("could not be saved", controls.status)


if __name__ == "__main__":
    unittest.main()
