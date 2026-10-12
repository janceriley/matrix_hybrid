from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from options_matrix.main import main, resolve_settings_root


class MainTestCase(unittest.TestCase):
    def test_unknown_flags_reject_before_gui_creation(self):
        for argv in (["--unknown"], ["--recorder-link"], ["--runtime", "x"]):
            with self.subTest(argv=argv):
                with patch("options_matrix.app.OptionsMatrixApp") as app, \
                     redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        main(argv)
                    self.assertEqual(raised.exception.code, 2)
                    app.assert_not_called()

    def test_normal_startup_uses_the_studio_settings_folder_and_runs(self):
        settings = Path("studio-settings")
        with patch("options_matrix.app.OptionsMatrixApp") as app, \
             patch("options_matrix.main.resolve_settings_root", return_value=settings) as resolve:
            self.assertEqual(main([]), 0)
        resolve.assert_called_once_with(None)
        app.assert_called_once_with(settings_root=settings, market_data_service=None, demo=False)
        app.return_value.run.assert_called_once_with()

    def test_demo_startup_uses_made_up_market_data(self):
        with patch("options_matrix.app.OptionsMatrixApp") as app, \
             patch("options_matrix.main.resolve_settings_root", return_value=Path("s")), \
             patch("options_matrix.demo_service.DemoMarketDataService") as demo:
            self.assertEqual(main(["--demo"]), 0)
        app.assert_called_once_with(settings_root=Path("s"), market_data_service=demo.return_value, demo=True)

    def test_explicit_settings_folder_is_used_without_seeding(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("options_matrix.studio_settings.seed_from_original") as seed:
                self.assertEqual(resolve_settings_root(Path(directory)), Path(directory).resolve())
            seed.assert_not_called()

    def test_default_settings_folder_is_seeded_once_from_original_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "OptionsMatrix"
            original.mkdir()
            (original / "settings.json").write_text(json.dumps({"fair_value_edge_width_rate": 0.12}), encoding="utf-8")
            (original / "positions.json").write_text("{}", encoding="utf-8")
            with patch.dict("os.environ", {"APPDATA": str(root)}):
                studio = resolve_settings_root(None)
                self.assertEqual(studio, root / "OptionsMatrixStudio")
                self.assertTrue((studio / "settings.json").is_file())
                # Positions belong to the work PC and are never copied.
                self.assertFalse((studio / "positions.json").exists())
                (studio / "settings.json").write_text("{}", encoding="utf-8")
                resolve_settings_root(None)
                self.assertEqual((studio / "settings.json").read_text(encoding="utf-8"), "{}")


if __name__ == "__main__":
    unittest.main()
