from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from options_matrix.studio_settings import (
    DEFAULT_SHARE_PORT,
    StudioSettings,
    generate_share_password,
    load_studio_settings,
    save_studio_settings,
    seed_from_original,
    studio_app_data_dir,
    valid_share_password,
)


class StudioSettingsTests(unittest.TestCase):
    def test_defaults_share_on_the_standard_port_with_no_password_yet(self):
        settings = StudioSettings()
        self.assertTrue(settings.share_enabled)
        self.assertEqual(settings.share_port, DEFAULT_SHARE_PORT)
        self.assertEqual(settings.share_password, "")
        self.assertFalse(settings.record_every_trading_day)

    def test_generated_passwords_are_readable_random_and_valid(self):
        passwords = {generate_share_password() for _ in range(50)}
        self.assertEqual(len(passwords), 50)
        for password in passwords:
            self.assertRegex(password, r"^[A-HJ-NP-Z2-9]{4}(-[A-HJ-NP-Z2-9]{4}){3}$")
            self.assertTrue(valid_share_password(password))
        self.assertFalse(valid_share_password("short"))
        self.assertFalse(valid_share_password(" padded-password "))

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "studio.json"
            settings = StudioSettings(
                recording_folder="D:/data", record_every_trading_day=True, connect_at_startup=True,
                reload_tabs_after_connect=False, share_enabled=False, share_port=50000,
                share_password="ABCD-EFGH-JKLM-NPQR",
            )
            save_studio_settings(settings, path)
            self.assertEqual(load_studio_settings(path), settings)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 1)

    def test_invalid_values_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "studio.json"
            path.write_text(json.dumps({
                "version": 1, "share_port": 80, "share_password": "x", "share_enabled": "yes",
                "record_every_trading_day": 1, "recording_folder": 5,
            }), encoding="utf-8")
            self.assertEqual(load_studio_settings(path), StudioSettings())
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_studio_settings(path), StudioSettings())
            self.assertEqual(load_studio_settings(Path(directory) / "missing.json"), StudioSettings())

    def test_app_data_folder_and_one_time_seeding(self):
        self.assertEqual(studio_app_data_dir(environ={"APPDATA": "C:/AppData"}), Path("C:/AppData/OptionsMatrixStudio"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original"
            original.mkdir()
            (original / "matrix_cache.json").write_text("{}", encoding="utf-8")
            (original / "positions.json").write_text("{}", encoding="utf-8")
            studio = root / "studio"
            self.assertEqual(seed_from_original(studio, original), ["matrix_cache.json"])
            self.assertEqual(seed_from_original(studio, original), [])
            self.assertFalse((studio / "positions.json").exists())


if __name__ == "__main__":
    unittest.main()
