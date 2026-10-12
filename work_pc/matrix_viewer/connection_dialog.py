"""Window for entering the mini PC's address, port and password."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from options_matrix.window_placement import center_dialog_on_parent_monitor

from .viewer_settings import ViewerSettings

if TYPE_CHECKING:
    from .viewer_app import ViewerApp


class ConnectionDialog:
    def __init__(self, app: ViewerApp) -> None:
        self.app = app
        settings = app.viewer_settings
        self.dialog = tk.Toplevel(app.root)
        self.dialog.withdraw()
        self.dialog.title("Connect to the mini PC")
        self.dialog.transient(app.root)
        self.dialog.resizable(False, False)

        box = ttk.Frame(self.dialog, padding=14)
        box.grid(sticky="nsew")
        ttk.Label(
            box,
            text=(
                "Enter what the mini PC shows under MINI PC… > Share with the work PC.\n"
                "The address can be its IP address (for example 192.168.1.50) or its computer name."
            ),
            foreground="#667085",
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self.host = tk.StringVar(value=settings.host)
        self.port = tk.StringVar(value=str(settings.port))
        self.password = tk.StringVar(value=settings.password)
        self.connect_at_startup = tk.BooleanVar(value=settings.connect_at_startup)

        ttk.Label(box, text="Mini PC address").grid(row=1, column=0, sticky="w")
        host_entry = ttk.Entry(box, textvariable=self.host, width=30)
        host_entry.grid(row=1, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=2)
        ttk.Label(box, text="Port").grid(row=2, column=0, sticky="w")
        ttk.Entry(box, textvariable=self.port, width=8).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=2)
        ttk.Label(box, text="Password").grid(row=3, column=0, sticky="w")
        self.password_entry = ttk.Entry(box, textvariable=self.password, width=30, show="•")
        self.password_entry.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=2)
        self.show_button = ttk.Button(box, text="Show", command=self._toggle_password)
        self.show_button.grid(row=3, column=2, sticky="w", padx=(6, 0))
        ttk.Checkbutton(
            box, text="Connect automatically when this program opens", variable=self.connect_at_startup
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.error = tk.StringVar()
        ttk.Label(box, textvariable=self.error, foreground="#b42318", wraplength=420).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )
        buttons = ttk.Frame(box)
        buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Save and connect", command=self._save).pack(side="right", padx=(0, 6))
        self.dialog.bind("<Return>", lambda _event: self._save())
        self.dialog.bind("<Escape>", lambda _event: self.dialog.destroy())
        center_dialog_on_parent_monitor(self.dialog, app.root)
        self.dialog.deiconify()
        self.dialog.grab_set()
        host_entry.focus_set()

    def _toggle_password(self) -> None:
        hidden = str(self.password_entry.cget("show")) != ""
        self.password_entry.configure(show="" if hidden else "•")
        self.show_button.configure(text="Hide" if hidden else "Show")

    def _save(self) -> None:
        host = self.host.get().strip()
        password = self.password.get().strip()
        try:
            port = int(self.port.get().strip())
        except ValueError:
            port = 0
        if not host:
            self.error.set("Enter the mini PC's address.")
            return
        if not 1 <= port <= 65535:
            self.error.set("Enter the port number shown on the mini PC (for example 47210).")
            return
        if not password:
            self.error.set("Enter the password shown on the mini PC.")
            return
        settings = ViewerSettings(
            host=host, port=port, password=password, connect_at_startup=bool(self.connect_at_startup.get())
        )
        self.dialog.destroy()
        try:
            self.app.apply_viewer_settings(settings, connect=True)
        except OSError as exc:
            messagebox.showerror("Connect to the mini PC", str(exc), parent=self.app.root)
