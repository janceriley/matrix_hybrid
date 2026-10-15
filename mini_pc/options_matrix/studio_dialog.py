"""The Mini PC settings window: sharing with the work PC, and the autopilot."""

from __future__ import annotations

import socket
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from .one_desk import MUTED, NAVY, TITLE_FONT
from .studio_settings import MAX_SHARE_PORT, MIN_SHARE_PORT
from .window_placement import center_dialog_on_parent_monitor

if TYPE_CHECKING:
    from .app import OptionsMatrixApp

REFRESH_MS = 1000


class StudioSettingsDialog:
    def __init__(self, app: OptionsMatrixApp) -> None:
        self.app = app
        self.dialog = tk.Toplevel(app.root)
        self.dialog.withdraw()
        self.dialog.title("Mini PC settings")
        self.dialog.transient(app.root)
        self.dialog.resizable(False, False)
        self._after_id: str | None = None
        settings = app.studio_settings

        box = ttk.Frame(self.dialog, padding=14)
        box.grid(sticky="nsew")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Share with the work PC", font=TITLE_FONT).grid(
            row=0, column=0, columnspan=4, sticky="w"
        )
        self.share_enabled = tk.BooleanVar(value=settings.share_enabled)
        ttk.Checkbutton(
            box,
            text="Let work PC viewers on this network see this matrix",
            variable=self.share_enabled,
            command=self._on_share_enabled,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 6))

        ttk.Label(box, text="This PC's address").grid(row=2, column=0, sticky="w")
        self.address_var = tk.StringVar(value=self._address_text())
        ttk.Label(box, textvariable=self.address_var, wraplength=420, justify="left").grid(
            row=2, column=1, columnspan=3, sticky="w", padx=(8, 0)
        )

        ttk.Label(box, text="Port").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.port_var = tk.StringVar(value=str(settings.share_port))
        port_entry = ttk.Entry(box, textvariable=self.port_var, width=8)
        port_entry.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Button(box, text="Use this port", command=self._apply_port).grid(
            row=3, column=2, sticky="w", padx=(6, 0), pady=(6, 0)
        )

        ttk.Label(box, text="Password").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.password_var = tk.StringVar(value=settings.share_password or "(created when sharing starts)")
        self.password_entry = ttk.Entry(box, textvariable=self.password_var, width=26, show="•")
        self.password_entry.configure(state="readonly")
        self.password_entry.grid(row=4, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        password_buttons = ttk.Frame(box)
        password_buttons.grid(row=4, column=2, columnspan=2, sticky="w", padx=(6, 0), pady=(6, 0))
        self.show_button = ttk.Button(password_buttons, text="Show", command=self._toggle_password)
        self.show_button.pack(side="left")
        ttk.Button(password_buttons, text="Copy", command=self._copy_password).pack(side="left", padx=(6, 0))
        ttk.Button(password_buttons, text="New password", command=self._new_password).pack(side="left", padx=(6, 0))

        self.share_status_var = tk.StringVar()
        ttk.Label(box, textvariable=self.share_status_var, foreground=NAVY, wraplength=520, justify="left").grid(
            row=5, column=0, columnspan=4, sticky="w", pady=(8, 0)
        )
        ttk.Label(
            box,
            text=(
                "On the work PC, enter this address, port and password. The first time sharing "
                "starts, Windows may ask whether Python can use the network: allow Private networks "
                "only. Keep this on your home network; never forward this port on your router."
            ),
            foreground=MUTED,
            wraplength=520,
            justify="left",
        ).grid(row=6, column=0, columnspan=4, sticky="w", pady=(4, 0))

        ttk.Separator(box).grid(row=7, column=0, columnspan=4, sticky="ew", pady=12)

        ttk.Label(box, text="Autopilot", font=TITLE_FONT).grid(
            row=8, column=0, columnspan=4, sticky="w"
        )
        self.connect_at_startup = tk.BooleanVar(value=settings.connect_at_startup)
        ttk.Checkbutton(
            box,
            text="Connect to Schwab when this program starts, and keep reconnecting",
            variable=self.connect_at_startup,
            command=lambda: self._save(connect_at_startup=bool(self.connect_at_startup.get())),
        ).grid(row=9, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.reload_tabs = tk.BooleanVar(value=settings.reload_tabs_after_connect)
        ttk.Checkbutton(
            box,
            text="Reload the saved matrix tabs after connecting",
            variable=self.reload_tabs,
            command=lambda: self._save(reload_tabs_after_connect=bool(self.reload_tabs.get())),
        ).grid(row=10, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(
            box,
            text="Record every trading day automatically",
            variable=app.record_daily_var,
            command=app._on_record_daily_changed,
        ).grid(row=11, column=0, columnspan=4, sticky="w")
        self.autopilot_var = tk.StringVar()
        ttk.Label(box, textvariable=self.autopilot_var, foreground=NAVY, wraplength=520, justify="left").grid(
            row=12, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )
        ttk.Label(
            box,
            text=(
                "To open this program whenever Windows signs in, double-click add_to_startup.bat "
                "in the mini_pc folder. Also turn off sleep for this PC."
            ),
            foreground=MUTED,
            wraplength=520,
            justify="left",
        ).grid(row=13, column=0, columnspan=4, sticky="w", pady=(4, 0))

        ttk.Button(box, text="Close", command=self.close).grid(row=14, column=3, sticky="e", pady=(12, 0))
        self.dialog.protocol("WM_DELETE_WINDOW", self.close)
        self.dialog.bind("<Escape>", lambda _event: self.close())
        self._refresh()
        center_dialog_on_parent_monitor(self.dialog, app.root)
        self.dialog.deiconify()

    # Helpers ------------------------------------------------------------------
    @staticmethod
    def _address_text() -> str:
        from .studio_services import local_ipv4_addresses

        addresses = local_ipv4_addresses()
        name = socket.gethostname()
        if not addresses:
            return f"{name} (no network address found)"
        if len(addresses) == 1:
            return f"{addresses[0]}   (computer name: {name})"
        return f"{addresses[0]}   (also {', '.join(addresses[1:])}; computer name: {name})"

    def _save(self, **changes: object) -> None:
        self.app._update_studio_settings(**changes)
        self._refresh(reschedule=False)

    def _on_share_enabled(self) -> None:
        self._save(share_enabled=bool(self.share_enabled.get()))

    def _apply_port(self) -> None:
        raw = self.port_var.get().strip()
        try:
            port = int(raw)
        except ValueError:
            port = -1
        if not MIN_SHARE_PORT <= port <= MAX_SHARE_PORT:
            messagebox.showerror(
                "Mini PC settings",
                f"Enter a port number from {MIN_SHARE_PORT} to {MAX_SHARE_PORT}.",
                parent=self.dialog,
            )
            return
        self._save(share_port=port)

    def _toggle_password(self) -> None:
        hidden = str(self.password_entry.cget("show")) != ""
        self.password_entry.configure(show="" if hidden else "•")
        self.show_button.configure(text="Hide" if hidden else "Show")

    def _copy_password(self) -> None:
        password = self.app.studio_settings.share_password
        if password:
            self.dialog.clipboard_clear()
            self.dialog.clipboard_append(password)
            self.share_status_var.set("Password copied.")

    def _new_password(self) -> None:
        if not messagebox.askyesno(
            "New password",
            "Connected work PCs will be disconnected and will need the new password. Continue?",
            parent=self.dialog,
        ):
            return
        services = self.app.services
        if services is not None:
            password = services.regenerate_password()
        else:
            from .studio_settings import generate_share_password

            password = generate_share_password()
            self.app._update_studio_settings(share_password=password)
        self.password_var.set(password)

    def _refresh(self, *, reschedule: bool = True) -> None:
        services = self.app.services
        settings = self.app.studio_settings
        if settings.share_password:
            self.password_var.set(settings.share_password)
        if services is None:
            self.share_status_var.set("Sharing and autopilot start when the program is opened normally.")
            self.autopilot_var.set("")
        else:
            self.share_status_var.set(services.share_status_text())
            self.autopilot_var.set(f"Autopilot: {services.autopilot.status_text}")
        if reschedule:
            self._after_id = self.dialog.after(REFRESH_MS, self._refresh)

    def close(self) -> None:
        if self._after_id is not None:
            try:
                self.dialog.after_cancel(self._after_id)
            except tk.TclError:
                pass
        self.dialog.destroy()
