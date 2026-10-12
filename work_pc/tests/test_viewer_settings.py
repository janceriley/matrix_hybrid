from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from matrix_viewer.viewer_settings import (
    DEFAULT_PORT,
    ViewerSettings,
    load_viewer_settings,
    save_viewer_settings,
    viewer_app_data_dir,
)


class ViewerSettingsTests(unittest.TestCase):
    def test_new_settings_need_an_address_and_password(self):
        settings = ViewerSettings()
        self.assertEqual(settings.port, DEFAULT_PORT)
        self.assertFalse(settings.complete)
        self.assertTrue(ViewerSettings(host="192.168.1.50", password="ABCD-EFGH-JKLM-NPQR").complete)

    def test_round_trip_and_bad_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "viewer.json"
            settings = ViewerSettings(host="mini-pc", port=50000, password="pw-12345678", connect_at_startup=False)
            save_viewer_settings(settings, path)
            self.assertEqual(load_viewer_settings(path), settings)
            path.write_text(json.dumps({"version": 1, "host": 5, "port": "x", "password": None}), encoding="utf-8")
            self.assertEqual(load_viewer_settings(path), ViewerSettings())
            self.assertEqual(load_viewer_settings(Path(directory) / "none.json"), ViewerSettings())

    def test_settings_live_in_their_own_app_data_folder(self):
        self.assertEqual(
            viewer_app_data_dir(environ={"APPDATA": "C:/AppData"}),
            Path("C:/AppData/OptionsMatrixViewer"),
        )


if __name__ == "__main__":
    unittest.main()
