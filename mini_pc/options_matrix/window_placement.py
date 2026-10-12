"""Place Tk dialogs on the monitor containing their parent window."""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk


class _Rect(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int32) for name in ("left", "top", "right", "bottom")]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", ctypes.c_uint32),
    ]


def _parent_monitor_work_area(parent) -> tuple[int, int, int, int] | None:
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # HWND and HMONITOR are pointer-sized, including on 64-bit Windows.
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MonitorInfo)]
        user32.GetMonitorInfoW.restype = ctypes.c_int32

        hwnd = int(parent.winfo_id())
        if not hwnd:
            return None
        # Tk's id is its client window; GA_ROOT resolves its native outer frame.
        hwnd = user32.GetAncestor(hwnd, 2) or hwnd
        monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not monitor:
            return None
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        rect = info.rcWork
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return rect.left, rect.top, rect.right, rect.bottom
    except (AttributeError, OSError, TypeError, ValueError, tk.TclError):
        return None


def center_dialog_on_parent_monitor(dialog, parent) -> None:
    """Place a constructed, withdrawn dialog before the caller deiconifies it.

    Windows uses the parent's monitor work area, excluding taskbars. If native
    monitor information is unavailable, center over the parent itself. This
    helper leaves visibility, focus, and modal grabs to the caller.
    """
    try:
        dialog.update_idletasks()
        width = max(1, dialog.winfo_reqwidth(), dialog.winfo_width())
        height = max(1, dialog.winfo_reqheight(), dialog.winfo_height())
        work_area = _parent_monitor_work_area(parent)
        if work_area is not None:
            left, top, right, bottom = work_area
            x = max(left, left + (right - left - width) // 2)
            y = max(top, top + (bottom - top - height) // 2)
        else:
            # Do not clamp against the primary screen: parent coordinates may
            # be negative when its monitor is left of or above that screen.
            x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
        # The explicit '+' anchors to the left/top, even for a negative value.
        # Tk accepts '+-1200'; '-1200' instead measures from the right edge.
        # Set only position so later validation messages can grow the dialog.
        dialog.geometry(f"+{x}+{y}")
    except tk.TclError:
        # Parent/dialog destruction during idle geometry work needs no dialog.
        return
