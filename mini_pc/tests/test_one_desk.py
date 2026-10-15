from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from options_matrix import one_desk
from options_matrix.onedesk.tokens import load as load_tokens
from options_matrix.share_protocol import MATH_SOURCE_FILES

try:
    import tkinter as tk
    from tkinter import ttk

    _root = tk.Tk()
    _root.destroy()
    TK_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the machine
    TK_AVAILABLE = False


class OneDeskBridgeTestCase(unittest.TestCase):
    def test_theme_mode_opens_light_unless_the_override_says_otherwise(self) -> None:
        with patch("options_matrix.onedesk.tokens.system_prefers_dark", return_value=True):
            with patch.dict(os.environ):
                os.environ.pop(one_desk.THEME_ENV_VAR, None)
                self.assertEqual(one_desk.theme_mode(), "light")
            for value, expected in (
                ("light", "light"),
                ("DARK", "dark"),
                (" dark ", "dark"),
                ("auto", "dark"),
                ("", "light"),
                ("sepia", "light"),
            ):
                with patch.dict(os.environ, {one_desk.THEME_ENV_VAR: value}):
                    self.assertEqual(one_desk.theme_mode(), expected)

    def test_matrix_colors_are_one_desk_tokens_for_the_mode(self) -> None:
        palette = load_tokens().palette(one_desk.MODE)
        self.assertEqual(one_desk.GRID_LINE, palette.rule_strong)
        self.assertEqual(one_desk.GRID_BG, palette.surface)
        self.assertEqual(one_desk.BUY_COLOR, palette.up)
        self.assertEqual(one_desk.SELL_COLOR, palette.down)
        self.assertEqual(one_desk.FAIR_VALUE_COLOR, palette.model)
        self.assertEqual((one_desk.BID_COLOR, one_desk.ASK_COLOR), (palette.quote, palette.quote_2))
        self.assertEqual(one_desk.HATCH, palette.hatch)
        self.assertNotEqual(one_desk.ITM_SELECTED_BG, one_desk.ITM_BG)

    def test_the_theme_stays_out_of_the_math_fingerprint(self) -> None:
        self.assertEqual(MATH_SOURCE_FILES, ("models.py", "app.py"))
        self.assertNotIn("one_desk.py", MATH_SOURCE_FILES)

    @unittest.skipUnless(TK_AVAILABLE, "needs a desktop session with Tk")
    def test_caps_labels_style_only_fixed_all_caps_labels(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            theme = one_desk.apply(root)
            fonts = one_desk.matrix_fonts(theme)
            self.assertEqual(fonts.header.actual("size"), fonts.detail_bold.actual("size"))
            caps = ttk.Label(root, text="TICKER")
            words = ttk.Label(root, text="Mini PC address")
            live = ttk.Label(root, text="", textvariable=tk.StringVar(root, value="● LIVE"))
            styled = ttk.Label(root, text="STATUS", style="Muted.TLabel")
            self.assertEqual(one_desk.caps_labels(root), 1)
            self.assertEqual(str(caps.cget("style")), "Caps.TLabel")
            self.assertEqual(str(words.cget("style")), "")
            self.assertEqual(str(live.cget("style")), "")
            self.assertEqual(str(styled.cget("style")), "Muted.TLabel")
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
