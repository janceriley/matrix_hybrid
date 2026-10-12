from __future__ import annotations

import ctypes
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from options_matrix.window_placement import (
    _MonitorInfo,
    _Rect,
    _parent_monitor_work_area,
    center_dialog_on_parent_monitor,
)


def _window(*, width=1, height=1, requested_width=400, requested_height=300, x=0, y=0):
    return SimpleNamespace(
        update_idletasks=Mock(),
        winfo_reqwidth=Mock(return_value=requested_width),
        winfo_reqheight=Mock(return_value=requested_height),
        winfo_width=Mock(return_value=width),
        winfo_height=Mock(return_value=height),
        winfo_rootx=Mock(return_value=x),
        winfo_rooty=Mock(return_value=y),
        winfo_id=Mock(return_value=0x123400000001),
        geometry=Mock(),
    )


class WindowPlacementTests(unittest.TestCase):
    def test_centers_inside_parent_monitor_work_area_including_taskbar_offset(self) -> None:
        dialog, parent = _window(), _window()
        with patch("options_matrix.window_placement._parent_monitor_work_area", return_value=(1920, 40, 3840, 1080)):
            center_dialog_on_parent_monitor(dialog, parent)
        dialog.update_idletasks.assert_called_once_with()
        dialog.geometry.assert_called_once_with("+2680+410")

    def test_negative_monitor_origins_use_absolute_left_and_top_offsets(self) -> None:
        dialog, parent = _window(), _window()
        with patch("options_matrix.window_placement._parent_monitor_work_area", return_value=(-1920, -1080, 0, -40)):
            center_dialog_on_parent_monitor(dialog, parent)
        dialog.geometry.assert_called_once_with("+-1160+-710")

    def test_oversized_dialog_keeps_its_upper_left_controls_in_work_area(self) -> None:
        dialog, parent = _window(requested_width=2100, requested_height=1500), _window()
        with patch("options_matrix.window_placement._parent_monitor_work_area", return_value=(-1920, 40, 0, 1080)):
            center_dialog_on_parent_monitor(dialog, parent)
        dialog.geometry.assert_called_once_with("+-1920+40")

    def test_tk_fallback_centers_over_parent_on_negative_monitor(self) -> None:
        dialog = _window()
        parent = _window(width=1200, height=800, x=-1600, y=100)
        with patch("options_matrix.window_placement._parent_monitor_work_area", return_value=None):
            center_dialog_on_parent_monitor(dialog, parent)
        dialog.geometry.assert_called_once_with("+-1200+350")

    def test_destroyed_dialog_during_idle_work_is_harmless(self) -> None:
        dialog, parent = _window(), _window()
        dialog.update_idletasks.side_effect = tk.TclError("window destroyed")
        center_dialog_on_parent_monitor(dialog, parent)
        dialog.geometry.assert_not_called()


class WindowsMonitorTests(unittest.TestCase):
    def user32(self):
        def fill_monitor(_monitor, info_pointer):
            info = ctypes.cast(info_pointer, ctypes.POINTER(_MonitorInfo)).contents
            self.assertEqual(info.cbSize, 40)
            info.rcMonitor = _Rect(-1920, 0, 0, 1080)
            info.rcWork = _Rect(-1880, 0, 0, 1080)
            return 1
        return SimpleNamespace(
            GetAncestor=Mock(return_value=0x123400000002),
            MonitorFromWindow=Mock(return_value=0x123400000003),
            GetMonitorInfoW=Mock(side_effect=fill_monitor),
        )

    def test_native_monitor_uses_outer_parent_handle_and_pointer_sized_signatures(self) -> None:
        api = self.user32()
        with patch("options_matrix.window_placement.sys.platform", "win32"), patch(
            "options_matrix.window_placement.ctypes.WinDLL", return_value=api, create=True
        ):
            self.assertEqual(_parent_monitor_work_area(_window()), (-1880, 0, 0, 1080))
        api.GetAncestor.assert_called_once_with(0x123400000001, 2)
        api.MonitorFromWindow.assert_called_once_with(0x123400000002, 2)
        self.assertIs(api.GetAncestor.restype, ctypes.c_void_p)
        self.assertIs(api.MonitorFromWindow.restype, ctypes.c_void_p)
        self.assertIs(api.GetMonitorInfoW.argtypes[0], ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(_MonitorInfo), 40)

    def test_failed_native_lookup_requests_tk_fallback(self) -> None:
        for failed_function in ("MonitorFromWindow", "GetMonitorInfoW"):
            with self.subTest(failed_function=failed_function):
                api = self.user32()
                getattr(api, failed_function).side_effect = None
                getattr(api, failed_function).return_value = 0
                with patch("options_matrix.window_placement.sys.platform", "win32"), patch(
                    "options_matrix.window_placement.ctypes.WinDLL", return_value=api, create=True
                ):
                    self.assertIsNone(_parent_monitor_work_area(_window()))

    def test_native_load_failure_requests_tk_fallback(self) -> None:
        with patch("options_matrix.window_placement.sys.platform", "win32"), patch(
            "options_matrix.window_placement.ctypes.WinDLL", side_effect=OSError("unavailable"), create=True
        ):
            self.assertIsNone(_parent_monitor_work_area(_window()))

    def test_non_windows_uses_tk_without_loading_native_api(self) -> None:
        with patch("options_matrix.window_placement.sys.platform", "linux"), patch(
            "options_matrix.window_placement.ctypes.WinDLL", create=True
        ) as loader:
            self.assertIsNone(_parent_monitor_work_area(_window()))
            loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
