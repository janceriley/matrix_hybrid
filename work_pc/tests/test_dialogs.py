"""Open every new window once to make sure each builds without errors."""

from __future__ import annotations

import gc
import socket
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    _root = tk.Tk()
    _root.destroy()
    TK_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the machine
    TK_AVAILABLE = False

from options_matrix.app import OptionsMatrixApp
from options_matrix.demo_service import DemoMarketDataService

from matrix_viewer.viewer_app import ViewerApp
from matrix_viewer.viewer_settings import ViewerSettings


@unittest.skipUnless(TK_AVAILABLE, "needs a desktop session with Tk")
class WindowSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        gc.collect()
        gc.disable()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        gc.enable()
        gc.collect()
        self.temporary.cleanup()

    def test_mini_pc_settings_window_and_recording_tab(self) -> None:
        studio = OptionsMatrixApp(
            settings_root=self.root / "studio", market_data_service=DemoMarketDataService(), demo=True
        )
        try:
            studio.share_bind_host = "127.0.0.1"
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            studio._update_studio_settings(share_port=port)
            studio.start_services()
            from options_matrix.studio_dialog import StudioSettingsDialog

            dialog = StudioSettingsDialog(studio)
            studio.root.update()
            self.assertIn(str(port), dialog.share_status_var.get())
            self.assertTrue(dialog.password_var.get())
            dialog._toggle_password()
            dialog.close()
            self.assertIn("DEMO DATA", studio.root.title())
            self.assertTrue(studio.recording_start_button.winfo_exists())
            self.assertIn("VIEWER", studio.share_indicator_var.get())
        finally:
            studio.stop_services()
            studio.root.destroy()

    def test_work_pc_connection_window_and_read_only_settings(self) -> None:
        viewer = ViewerApp(settings_root=self.root / "viewer")
        try:
            from matrix_viewer.connection_dialog import ConnectionDialog

            dialog = ConnectionDialog(viewer)
            dialog.host.set("192.168.1.50")
            dialog.password.set("ABCD-EFGH-JKLM-NPQR")
            with patch.object(viewer, "apply_viewer_settings") as apply:
                dialog._save()
            saved = apply.call_args.args[0]
            self.assertEqual(saved, ViewerSettings(host="192.168.1.50", port=47210, password="ABCD-EFGH-JKLM-NPQR"))

            viewer._open_settings_dialog()
            viewer.root.update()
            dialogs = [w for w in viewer.root.winfo_children() if isinstance(w, tk.Toplevel)]
            self.assertTrue(dialogs)
            texts = []

            def collect(widget):
                try:
                    texts.append(str(widget.cget("text")))
                except tk.TclError:
                    pass
                for child in widget.winfo_children():
                    collect(child)

            for window in dialogs:
                collect(window)
                window.destroy()
            self.assertTrue(any("come from the mini PC" in text for text in texts))
            self.assertFalse(any(text == "Apply" for text in texts))
            self.assertEqual(viewer.connection_var.get(), "○ OFFLINE")
        finally:
            viewer.stop_services()
            viewer.root.destroy()


if __name__ == "__main__":
    unittest.main()
