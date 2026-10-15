"""Options Matrix Viewer: the work PC window that mirrors the mini PC.

Every mini PC tab is kept here twice:

* a *mirror* tab, rebuilt from the mini PC's option chain, tab settings and
  live quotes with the very same code the mini PC runs, so its fair values
  and signals are identical; and
* a *display* tab, which shows only the part you chose (side, strike range,
  widths) and shares the mirror's live quotes.

Positions and exit alerts live on this PC and watch the full mirror tabs, so
a position keeps being monitored even while your view hides its row.
"""

from __future__ import annotations

import logging
import math
import queue
import time
import tkinter as tk
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Mapping, Sequence

from options_matrix.app import (
    CONNECTION_FAILED_COLOR,
    CONNECTION_LIVE_COLOR,
    CONNECTION_OFFLINE_COLOR,
    CONNECTION_PENDING_COLOR,
    GRID_BG,
    HEADER_BG,
    MATRIX_VIEW_BOTH,
    RECORDER_ACTIVE_COLOR,
    RECORDER_OFF_COLOR,
    RECORDER_PENDING_COLOR,
    SELECTED_BG,
    MatrixInputState,
    MatrixSideState,
    MatrixTabState,
    OptionsMatrixApp,
    build_matrix_view_plan,
    formula_settings_from_mapping,
    leg_quote_ids,
    market_data_quote_counts,
    market_data_quote_status_for_selections,
    matrix_tab_identity,
    matrix_tab_label,
    matrix_view_rights,
    matrix_width_bands,
    normalize_matrix_view,
    seed_cached_matrix_quotes,
    union_matrix_quote_target_selections,
)
from options_matrix.one_desk import GRID_LINE, INK, INK_2, MUTED, NAVY, SUNKEN
from options_matrix.models import (
    DEFAULT_SIGNAL_METHOD,
    MatrixSelectionPlan,
    OptionContractRecord,
    build_ranked_matrix_signals,
    format_strike,
    normalize_signal_method,
)
from options_matrix.positions import position_key_for_selection
from options_matrix.share_protocol import (
    ProtocolError,
    decode_diagnostic,
    decode_quote,
    decode_record,
    decode_search_result,
    math_fingerprint,
    matrix_input_fields,
)

from .client import (
    EVENT_CONNECTED,
    EVENT_CONNECTING,
    EVENT_DISCONNECTED,
    EVENT_MESSAGE,
    EVENT_STOPPED,
    ShareClient,
)
from .remote_service import RemoteMarketDataService
from .viewer_settings import (
    VIEWER_SETTINGS_FILENAME,
    ViewerSettings,
    load_viewer_settings,
    save_viewer_settings,
)

LOG = logging.getLogger(__name__)

LINK_STALE_SECONDS = 3.5
WATCHDOG_MS = 500
MAX_EVENTS_PER_POLL = 200

LINK_OFFLINE = "offline"
LINK_CONNECTING = "connecting"
LINK_LIVE = "live"
LINK_RETRYING = "retrying"
LINK_STOPPED = "stopped"

HEALTH_NOT_CONNECTED = "Mini PC status will appear here once connected."

TabIdentity = tuple[str, str, str]


@dataclass(frozen=True)
class ViewFilter:
    """The part of one mini PC tab that this PC shows."""

    side: str
    low: float
    high: float
    widths: tuple[float, ...]


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _filter_plan(
    plan: MatrixSelectionPlan,
    low: float,
    high: float,
    widths: Sequence[float],
) -> MatrixSelectionPlan:
    allowed = {round(float(width), 4) for width in widths}
    return MatrixSelectionPlan(
        visible={
            key: selection
            for key, selection in plan.visible.items()
            if low <= round(float(selection.pair.lower.strike), 4)
            and round(float(selection.pair.upper.strike), 4) <= high
            and round(float(key[1]), 4) in allowed
        },
        # Context stays complete so the stream-status borders match the mini PC.
        context_by_width=plan.context_by_width,
    )


class ViewerApp(OptionsMatrixApp):
    window_title = "Options Matrix Viewer (work PC)"
    settings_read_only = True

    def __init__(self, *, settings_root: Path) -> None:
        self._viewer_root = settings_root
        self.viewer_settings_path = settings_root / VIEWER_SETTINGS_FILENAME
        self.viewer_settings: ViewerSettings = load_viewer_settings(self.viewer_settings_path)
        self._inbound: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._client: ShareClient | None = None
        self._mirror_tabs: dict[int, MatrixTabState] = {}
        self._mirror_keys: dict[int, tuple[Any, ...]] = {}
        self._studio_records: dict[str, list[OptionContractRecord]] = {}
        self._view_filters: dict[TabIdentity, ViewFilter] = {}
        self._link_state = LINK_OFFLINE
        self._link_message = "Not connected to the mini PC."
        self._link_stop_reason = ""
        self._link_last_message_at = 0.0
        self._studio_health: dict[str, Any] = {}
        self._studio_welcome: dict[str, Any] = {}
        self._local_fingerprint = math_fingerprint()
        self._watchdog_id: str | None = None
        self.remote = RemoteMarketDataService()
        super().__init__(settings_root=settings_root, market_data_service=self.remote)
        self.remote.on_disconnect = self._stop_client
        self._set_status(
            "Connecting to the mini PC ..."
            if self.viewer_settings.complete
            else "Click Connection… and enter the mini PC's address and password."
        )

    # ------------------------------------------------------------------
    # Window layout
    # ------------------------------------------------------------------
    def _build_connection_bar(self, main: ttk.Frame) -> None:
        connection = ttk.Frame(main)
        connection.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        connection.columnconfigure(6, weight=1)

        title = ttk.Label(connection, text="MINI PC")
        title.grid(row=0, column=0, sticky="w")
        self._install_tooltip(title, "Live data comes from Options Matrix Studio on the mini PC.")
        self.connection_status_label = ttk.Label(
            connection,
            textvariable=self.connection_var,
            foreground=CONNECTION_OFFLINE_COLOR,
            width=29,  # fits ◐ MINI PC: NO MARKET DATA with a character to spare
            anchor="center",
        )
        self.connection_status_label.grid(row=0, column=1, sticky="w", padx=(10, 4))
        self._install_tooltip(self.connection_status_label, lambda: self._link_message)
        self.auth_status_label = ttk.Label(
            connection,
            textvariable=self.auth_status_var,
            foreground=CONNECTION_PENDING_COLOR,
            width=13,
            anchor="center",
        )
        self.auth_status_label.grid(row=0, column=2, sticky="w", padx=(0, 4))
        self._install_tooltip(self.auth_status_label, lambda: self._studio_auth_detail())
        self.connect_button = ttk.Button(connection, text="Connect", command=self._connect)
        self.connect_button.grid(row=0, column=3, padx=(0, 6))
        self.disconnect_button = ttk.Button(connection, text="Disconnect", command=self._disconnect)
        self.disconnect_button.grid(row=0, column=4, padx=(0, 6))
        ttk.Button(connection, text="Connection…", command=self._open_connection_dialog).grid(
            row=0, column=5, padx=(0, 10)
        )
        self.recorder_indicator_label = ttk.Label(
            connection,
            textvariable=self.recorder_indicator_var,
            foreground=RECORDER_OFF_COLOR,
            width=15,
            anchor="center",
        )
        self.recorder_indicator_label.grid(row=0, column=7, sticky="e")
        self._install_tooltip(self.recorder_indicator_label, "Recording runs on the mini PC.")

        self.health_var = tk.StringVar(value=HEALTH_NOT_CONNECTED)
        health_label = ttk.Label(connection, textvariable=self.health_var, foreground=MUTED, anchor="w")
        health_label.grid(row=1, column=0, columnspan=8, sticky="ew", pady=(2, 0))

        ttk.Label(connection, text="STATUS", foreground=MUTED).grid(row=2, column=0, sticky="w", pady=(1, 0))
        self.status_label = ttk.Label(
            connection,
            textvariable=self.status_display_var,
            foreground=NAVY,
            anchor="w",
            width=1,
        )
        self.status_label.grid(row=2, column=1, columnspan=7, sticky="ew", padx=(8, 0), pady=(1, 0))
        self.status_label.bind("<Configure>", self._on_status_label_configure, add="+")
        self._install_tooltip(self.status_label, lambda: self.status_var.get())

    def _build_matrix_controls(self, main: ttk.Frame) -> None:
        controls = ttk.Frame(main)
        controls.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        controls.columnconfigure(9, weight=1)
        self.view_side_var = tk.StringVar()
        self.view_low_var = tk.StringVar()
        self.view_high_var = tk.StringVar()
        self.view_width_vars: dict[float, tk.BooleanVar] = {}

        ttk.Label(controls, text="VIEW  SIDE").grid(row=0, column=0, sticky="w")
        self.view_side_combo = ttk.Combobox(controls, textvariable=self.view_side_var, state="readonly", width=8)
        self.view_side_combo.grid(row=0, column=1, padx=(4, 10))
        ttk.Label(controls, text="LOW").grid(row=0, column=2, sticky="w")
        self.view_low_combo = ttk.Combobox(controls, textvariable=self.view_low_var, state="readonly", width=9)
        self.view_low_combo.grid(row=0, column=3, padx=(4, 10))
        ttk.Label(controls, text="HIGH").grid(row=0, column=4, sticky="w")
        self.view_high_combo = ttk.Combobox(controls, textvariable=self.view_high_var, state="readonly", width=9)
        self.view_high_combo.grid(row=0, column=5, padx=(4, 10))
        ttk.Label(controls, text="WIDTHS").grid(row=0, column=6, sticky="w")
        self.view_width_frame = ttk.Frame(controls)
        self.view_width_frame.grid(row=0, column=7, sticky="w", padx=(4, 10))
        buttons = ttk.Frame(controls)
        buttons.grid(row=0, column=8, sticky="w")
        ttk.Button(buttons, text="Apply view", command=self._apply_view_from_controls).pack(side="left")
        ttk.Button(buttons, text="Show all", command=self._reset_view_filter).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Settings…", command=self._open_settings_dialog).pack(side="left", padx=(6, 0))
        ttk.Label(
            controls,
            text="Choices are limited to what the mini PC has loaded. * marks a narrowed view.",
            foreground=MUTED,
        ).grid(row=0, column=9, sticky="e")

    def _build_extra_tabs(self, notebook: ttk.Notebook) -> None:
        # Recording happens on the mini PC; the viewer shows its state above.
        return None

    def _redraw_matrix_tabs(self) -> None:
        frame = self.matrix_tabs_frame
        for child in frame.winfo_children():
            child.destroy()
        if not self.matrix_tabs:
            text = "(waiting for the mini PC)" if self._link_state != LINK_LIVE else "(no tabs on the mini PC)"
            tk.Label(frame, text=text, bg=GRID_BG, fg=MUTED, font=self.grid_font).grid(row=0, column=0, sticky="w")
            return
        for index, tab in enumerate(self.matrix_tabs):
            is_active = tab.tab_id == self.active_tab_id
            bg = SELECTED_BG if is_active else HEADER_BG
            label = tk.Label(
                frame,
                text=tab.label,
                bg=bg,
                fg=INK,
                font=self.grid_font_bold if is_active else self.grid_font,
                padx=6,
                pady=2,
                bd=0,
                highlightthickness=1,
                highlightbackground=NAVY if is_active else GRID_LINE,
                highlightcolor=NAVY if is_active else GRID_LINE,
            )
            label.grid(row=0, column=index, padx=(0, 4), sticky="w")
            label.bind("<Button-1>", lambda _event, tab_id=tab.tab_id: self._activate_matrix_tab(tab_id))

    def _draw_stale_matrix_overlay(self, canvas: tk.Canvas, total_width: float, total_height: float) -> None:
        self.matrix_reload_hitbox = None
        visible_width = max(canvas.winfo_width(), total_width, 360)
        visible_height = max(canvas.winfo_height(), total_height, 180)
        canvas.create_rectangle(0, 0, visible_width, visible_height, fill=SUNKEN, outline="", stipple="gray25")
        if self._link_state == LINK_LIVE:
            text = "This tab is waiting for live data on the mini PC."
        elif self._link_state == LINK_STOPPED:
            text = self._link_message
        elif self._link_state == LINK_OFFLINE:
            text = "Not connected to the mini PC. Click Connect to show live data."
        else:
            text = "No live data from the mini PC. Reconnecting ..."
        canvas.create_text(
            visible_width / 2,
            max(90, visible_height / 2),
            text=text,
            fill=INK_2,
            font=self.grid_font_bold,
            anchor="c",
            width=max(240, visible_width - 40),
        )

    # ------------------------------------------------------------------
    # Things the work PC never does
    # ------------------------------------------------------------------
    def _load_cached_workspace_as_stale_tabs(self) -> None:
        return None

    def _save_current_matrix_cache(self, *, clear_when_empty: bool = False) -> None:
        return None

    def _clear_cached_matrix_workspace(self) -> None:
        self.cached_matrix_workspace = None

    def _reload_active_matrix_tab(self) -> None:
        self._set_status("Tabs are loaded on the mini PC; this one is waiting for it.")

    def _sync_quote_subscriptions(self, *, force: bool = False) -> None:
        selections = self._all_matrix_tab_quote_selections()
        self.active_market_data_quote_counts = market_data_quote_counts(selections) if selections else None
        self._refresh_staged_market_data_quote_status()

    def _refresh_staged_market_data_quote_status(self) -> None:
        self._set_market_data_quote_status(
            market_data_quote_status_for_selections(self._all_matrix_tab_quote_selections())
        )

    def _refresh_recorder_controls(self, status_override: Any = None) -> None:
        recording = self._studio_health.get("recording") if isinstance(self._studio_health, Mapping) else None
        if not isinstance(recording, Mapping) or self._link_state != LINK_LIVE:
            self.recorder_indicator_var.set("○ REC ?")
            color = RECORDER_OFF_COLOR
        else:
            indicator = str(recording.get("indicator") or "○ REC OFF")
            self.recorder_indicator_var.set(indicator)
            color = (
                RECORDER_ACTIVE_COLOR
                if "ON" in indicator or "ERROR" in indicator
                else RECORDER_PENDING_COLOR
                if any(word in indicator for word in ("ARMED", "STARTING", "STOPPING"))
                else RECORDER_OFF_COLOR
            )
        label = getattr(self, "recorder_indicator_label", None)
        if label is not None:
            label.configure(foreground=color)

    # ------------------------------------------------------------------
    # Connection to the mini PC
    # ------------------------------------------------------------------
    def start_services(self) -> None:
        self._schedule_watchdog()
        if self.viewer_settings.complete and self.viewer_settings.connect_at_startup:
            self._start_client()
        self._set_connected_state(False)

    def stop_services(self) -> None:
        if self._watchdog_id is not None:
            try:
                self.root.after_cancel(self._watchdog_id)
            except tk.TclError:
                pass
            self._watchdog_id = None
        self._stop_client()

    def _connect(self) -> None:
        if not self.viewer_settings.complete:
            self._open_connection_dialog()
            return
        self._start_client()

    def _disconnect(self) -> None:
        self._stop_client()
        # Drop anything the stopped link had already queued, so a late
        # "connecting" or welcome cannot bring the badge back.
        self._inbound = queue.Queue()
        self._link_state = LINK_OFFLINE
        self._link_message = "Disconnected from the mini PC."
        self._link_lost("Disconnected from the mini PC.")
        self.health_var.set(HEALTH_NOT_CONNECTED)
        self._set_connected_state(False)

    def _start_client(self) -> None:
        self._stop_client()
        settings = self.viewer_settings
        self._inbound = queue.Queue()
        self._client = ShareClient(settings.host, settings.port, settings.password, self._inbound)
        self.remote.link_active = True
        self._link_state = LINK_CONNECTING
        self._link_message = f"Connecting to {settings.host}:{settings.port} ..."
        self._client.start()
        self._set_connected_state(False)

    def _stop_client(self) -> None:
        client = self._client
        self._client = None
        self.remote.link_active = False
        self.remote.link_live = False
        if client is not None:
            client.stop()

    def _schedule_watchdog(self) -> None:
        self._watchdog_id = self.root.after(WATCHDOG_MS, self._watchdog)

    def _watchdog(self) -> None:
        self._watchdog_id = None
        if self._link_state == LINK_LIVE and time.monotonic() - self._link_last_message_at > LINK_STALE_SECONDS:
            LOG.warning("No data from the mini PC for %.1f s; reconnecting", LINK_STALE_SECONDS)
            self._link_lost("No data from the mini PC for a few seconds. Reconnecting ...")
            # Reconnecting is the only way to get a complete fresh copy.
            if self._client is not None:
                self._start_client()
        if not getattr(self, "_shutdown_in_progress", False):
            self._schedule_watchdog()

    def _poll_queues(self) -> None:
        self._poll_studio_messages()
        super()._poll_queues()

    def _poll_studio_messages(self) -> None:
        changed = False
        for _ in range(MAX_EVENTS_PER_POLL):
            try:
                kind, payload = self._inbound.get_nowait()
            except queue.Empty:
                break
            try:
                changed = self._handle_link_event(kind, payload) or changed
            except (ProtocolError, KeyError, TypeError, ValueError) as exc:
                LOG.exception("Invalid data from the mini PC")
                self._link_lost(f"The mini PC sent data this viewer cannot read ({exc}). Reconnecting ...")
                if self._client is not None:
                    self._start_client()
                changed = True
                break
        if changed:
            self._set_connected_state(self.remote.connected)

    def _handle_link_event(self, kind: str, payload: Any) -> bool:
        if kind == EVENT_CONNECTING:
            if self._link_state not in (LINK_LIVE, LINK_RETRYING):
                self._link_state = LINK_CONNECTING
                self._link_message = f"Connecting to {payload} ..."
            return True
        if kind == EVENT_CONNECTED:
            return self._on_welcome(payload if isinstance(payload, Mapping) else {})
        if kind == EVENT_DISCONNECTED:
            self._link_lost(str(payload))
            return True
        if kind == EVENT_STOPPED:
            reason, text = payload if isinstance(payload, tuple) else ("", str(payload))
            self._stop_client()
            self._link_lost(str(text))
            self._link_state = LINK_STOPPED
            self._link_stop_reason = str(reason)
            self._link_message = str(text)
            self._set_status(str(text))
            self._redraw_all()
            return True
        if kind == EVENT_MESSAGE and isinstance(payload, Mapping):
            if self._link_state != LINK_LIVE:
                return False
            self._link_last_message_at = time.monotonic()
            return self._handle_studio_message(payload)
        return False

    def _on_welcome(self, welcome: Mapping[str, Any]) -> bool:
        if welcome.get("fingerprint") != self._local_fingerprint:
            self._stop_client()
            self._link_lost("Update needed.")
            self._link_state = LINK_STOPPED
            self._link_stop_reason = "version"
            self._link_message = (
                "Update needed: the mini PC and this PC run different versions of the matrix "
                "program, so their numbers could differ. Copy the same multi_matrix folder to both PCs."
            )
            self._set_status(self._link_message)
            self._redraw_all()
            return True
        self._studio_welcome = dict(welcome)
        self._reset_mirror_state()
        self._link_state = LINK_LIVE
        self._link_stop_reason = ""
        self._link_last_message_at = time.monotonic()
        self.remote.link_live = True
        name = str(welcome.get("studio_name") or "the mini PC")
        self._link_message = f"Connected to {name} at {self.viewer_settings.host}:{self.viewer_settings.port}."
        demo = " - DEMO DATA" if welcome.get("demo") else ""
        self.root.title(f"{self.window_title}{demo}")
        self._set_status(self._link_message)
        return True

    def _link_lost(self, message: str) -> None:
        """Mark everything stale; prices are never shown once the link is in doubt."""
        was_live = self._link_state == LINK_LIVE
        self.remote.link_live = False
        if self._link_state not in (LINK_OFFLINE, LINK_STOPPED):
            self._link_state = LINK_RETRYING if self._client is not None else LINK_OFFLINE
        self._link_message = message
        self._reset_mirror_state()
        self._studio_health = {}
        self.health_var.set("Waiting for the mini PC ...")
        self.auth_status_var.set("")
        self._sync_view_from_active_tab()
        self._redraw_matrix_tabs()
        self._redraw_all()
        if hasattr(self, "positions"):
            self._refresh_position_alerts()
        if was_live or message:
            self._set_status(message)

    def _reset_mirror_state(self) -> None:
        for tab in list(self._mirror_tabs.values()) + list(self.matrix_tabs):
            OptionsMatrixApp._mark_matrix_tab_stale(tab)
        self._mirror_keys = {}
        self.leg_quote_cache.clear()
        self.underlying_quote = None
        self._subscription_diagnostics_by_con_id = {}
        self._subscription_last_states = {}
        self._subscription_failure_counts = {}

    def _set_connected_state(self, connected: bool) -> None:
        self._service_connected = bool(connected)
        if hasattr(self, "positions") and hasattr(self, "position_summary_var"):
            if self._refresh_position_alerts():
                self._refresh_active_matrix_render()
        badge, color = self._link_badge()
        self.connection_var.set(badge)
        label = getattr(self, "connection_status_label", None)
        if label is not None:
            label.configure(foreground=color)
        running = self._client is not None
        connect_button = getattr(self, "connect_button", None)
        if connect_button is not None:
            connect_button.configure(state=tk.DISABLED if running else tk.NORMAL)
            self.disconnect_button.configure(state=tk.NORMAL if running else tk.DISABLED)
        self._refresh_recorder_controls()

    def _link_badge(self) -> tuple[str, str]:
        state = self._link_state
        if state == LINK_LIVE:
            if self.remote.studio_connected:
                return "● LIVE", CONNECTION_LIVE_COLOR
            return "◐ MINI PC: NO MARKET DATA", CONNECTION_PENDING_COLOR
        if state == LINK_CONNECTING:
            return "◐ CONNECTING", CONNECTION_PENDING_COLOR
        if state == LINK_RETRYING:
            return "↻ RECONNECTING", CONNECTION_PENDING_COLOR
        if state == LINK_STOPPED:
            if self._link_stop_reason == "password":
                return "✕ WRONG PASSWORD", CONNECTION_FAILED_COLOR
            if self._link_stop_reason == "version":
                return "⚠ UPDATE NEEDED", CONNECTION_FAILED_COLOR
            return "✕ NOT CONNECTED", CONNECTION_FAILED_COLOR
        return "○ OFFLINE", CONNECTION_OFFLINE_COLOR

    def _studio_auth_detail(self) -> str:
        auth = self._studio_health.get("auth") if isinstance(self._studio_health, Mapping) else None
        detail = str(auth.get("detail", "")) if isinstance(auth, Mapping) else ""
        return detail or "The mini PC's Schwab authorization status appears here."

    # ------------------------------------------------------------------
    # Messages from the mini PC
    # ------------------------------------------------------------------
    def _handle_studio_message(self, message: Mapping[str, Any]) -> bool:
        kind = message.get("type")
        if kind == "quotes":
            self._apply_studio_quotes(message)
            return False
        if kind == "health":
            return self._apply_studio_health(message)
        if kind == "tabs":
            self._apply_studio_tabs(message)
            return True
        if kind == "settings":
            self._apply_studio_settings(message)
            return False
        if kind == "diagnostics":
            self._apply_studio_diagnostics(message)
            return False
        return False

    def _apply_studio_settings(self, message: Mapping[str, Any]) -> None:
        formula = message.get("formula")
        if not isinstance(formula, Mapping):
            raise ProtocolError("Invalid settings.")
        settings = formula_settings_from_mapping(formula)
        try:
            self.signal_method = normalize_signal_method(str(message.get("signal_method") or DEFAULT_SIGNAL_METHOD))
        except ValueError:
            self.signal_method = DEFAULT_SIGNAL_METHOD
        OptionsMatrixApp._apply_formula_settings(self, settings, save=False)

    def _apply_studio_quotes(self, message: Mapping[str, Any]) -> None:
        from datetime import datetime, timezone

        items = message.get("quotes")
        if not isinstance(items, list):
            raise ProtocolError("Invalid quotes.")
        now_utc = datetime.now(timezone.utc)
        now_local = datetime.now()
        for item in items:
            self._handle_quote(decode_quote(item, now_utc=now_utc, now_local=now_local))

    def _apply_studio_diagnostics(self, message: Mapping[str, Any]) -> None:
        items = message.get("items")
        if not isinstance(items, list):
            raise ProtocolError("Invalid subscription status.")
        generation = int(getattr(self, "_quote_event_generation", 0))
        self._handle_subscription_status([decode_diagnostic(item, generation=generation) for item in items])

    def _apply_studio_health(self, message: Mapping[str, Any]) -> bool:
        self._studio_health = dict(message)
        schwab = message.get("schwab") if isinstance(message.get("schwab"), Mapping) else {}
        recording = message.get("recording") if isinstance(message.get("recording"), Mapping) else {}
        auth = message.get("auth") if isinstance(message.get("auth"), Mapping) else {}
        studio_connected = bool(schwab.get("connected"))
        changed = studio_connected != self.remote.studio_connected
        self.remote.studio_connected = studio_connected
        auth_badge = str(auth.get("badge", ""))
        self.auth_status_var.set(auth_badge)
        auth_label = getattr(self, "auth_status_label", None)
        if auth_label is not None:
            # The same colors as on the mini PC: red once re-authorization is overdue.
            auth_label.configure(
                foreground=CONNECTION_FAILED_COLOR if auth_badge == "⚠ AUTH DUE" else CONNECTION_PENDING_COLOR
            )
        rows = recording.get("rows_written")
        parts = [
            "DEMO DATA" if message.get("demo") else "",
            f"Mini PC market data {schwab.get('badge', '?')}",
            str(recording.get("indicator") or ""),
            f"{rows:,} rows saved" if isinstance(rows, int) and rows else "",
            str(recording.get("market") or ""),
            f"Autopilot: {message.get('autopilot')}" if message.get("autopilot") else "",
        ]
        self.health_var.set(" · ".join(part for part in parts if part))
        self._refresh_recorder_controls()
        return changed

    # ------------------------------------------------------------------
    # Mirror tabs (identical math) and display tabs (your view)
    # ------------------------------------------------------------------
    def _apply_studio_tabs(self, message: Mapping[str, Any]) -> None:
        search = decode_search_result(message.get("search"))
        previous = self.search_result
        if (
            search is None
            or previous is None
            or (search.underlying.symbol, search.underlying.con_id)
            != (previous.underlying.symbol, previous.underlying.con_id)
        ):
            self.underlying_quote = None
        self.search_result = search

        records_payload = message.get("records")
        if not isinstance(records_payload, Mapping):
            raise ProtocolError("Invalid option chains.")
        for key, items in records_payload.items():
            self._studio_records[str(key)] = [decode_record(item) for item in items]

        entries = message.get("tabs")
        if not isinstance(entries, list):
            raise ProtocolError("Invalid tabs.")
        used_keys = {entry.get("records") for entry in entries if isinstance(entry, Mapping)}
        self._studio_records = {key: value for key, value in self._studio_records.items() if key in used_keys}

        mirrors: dict[int, MatrixTabState] = {}
        keys: dict[int, tuple[Any, ...]] = {}
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ProtocolError("Invalid tab.")
            tab_id = int(entry["id"])
            mirror_key = (
                tuple(sorted(matrix_input_fields(entry["input"]).items())),
                entry.get("records"),
                bool(entry.get("stale")),
                _finite(entry.get("hint")),
            )
            existing = self._mirror_tabs.get(tab_id)
            if existing is not None and self._mirror_keys.get(tab_id) == mirror_key and not existing.is_stale:
                mirrors[tab_id] = existing
            else:
                mirrors[tab_id] = self._build_mirror_tab(entry)
            keys[tab_id] = mirror_key
        self._mirror_tabs = mirrors
        self._mirror_keys = keys

        live_identities = {matrix_tab_identity(tab.matrix_input) for tab in mirrors.values()}
        self._view_filters = {
            identity: view_filter
            for identity, view_filter in self._view_filters.items()
            if identity in live_identities
        }
        previous_displays = {tab.tab_id: tab for tab in self.matrix_tabs}
        self.matrix_tabs = [
            self._build_display_tab(mirror, previous_displays.get(tab_id))
            for tab_id, mirror in mirrors.items()
        ]
        if self.active_tab_id not in mirrors:
            studio_active = message.get("active")
            self.active_tab_id = studio_active if studio_active in mirrors else next(iter(mirrors), None)
        for display in self.matrix_tabs:
            self._rebuild_tab_from_quotes(display)
        self._sync_quote_subscriptions()
        self._sync_view_from_active_tab()
        self._redraw_matrix_tabs()
        self._redraw_all()
        self._refresh_view_controls()
        if hasattr(self, "positions"):
            self._refresh_position_alerts()

    def _build_mirror_tab(self, entry: Mapping[str, Any]) -> MatrixTabState:
        matrix_input = MatrixInputState(**matrix_input_fields(entry["input"]))
        tab = MatrixTabState(
            tab_id=int(entry["id"]),
            label=str(entry.get("label") or matrix_tab_label(matrix_input)),
            matrix_input=matrix_input,
            row_strikes=(),
            matrix_plan=MatrixSelectionPlan(visible={}, context_by_width={}),
            is_stale=True,
            underlying_price_hint=_finite(entry.get("hint")),
        )
        records = self._studio_records.get(entry.get("records")) if entry.get("records") else None
        if bool(entry.get("stale")) or records is None:
            return tab
        # Exactly the mini PC's plan: same chain, same tab settings, same code.
        view_plan = build_matrix_view_plan(records, matrix_input)
        if view_plan is None:
            return tab
        is_both = normalize_matrix_view(matrix_input.side) == MATRIX_VIEW_BOTH
        tab.side_states = (
            {
                right: MatrixSideState(right=right, row_strikes=side_plan.row_strikes, matrix_plan=side_plan.plan)
                for right, side_plan in view_plan.side_plans.items()
            }
            if is_both
            else {}
        )
        tab.matrix_plan = (
            MatrixSelectionPlan(visible={}, context_by_width={})
            if is_both
            else next(iter(view_plan.side_plans.values())).plan
        )
        targets = union_matrix_quote_target_selections([side.plan for side in view_plan.side_plans.values()])
        tab.matrix_input = replace(matrix_input, widths=view_plan.widths)
        tab.row_strikes = view_plan.row_strikes
        tab.leg_quotes = seed_cached_matrix_quotes(targets, self.leg_quote_cache)
        tab.leg_quote_ids = frozenset(leg_quote_ids(targets))
        tab.is_stale = False
        return tab

    def _allowed_sides(self, mirror: MatrixTabState) -> tuple[str, ...]:
        view = normalize_matrix_view(mirror.matrix_input.side)
        if view == MATRIX_VIEW_BOTH:
            return (*matrix_view_rights(view)[::-1], MATRIX_VIEW_BOTH)
        return (view,)

    def _effective_filter(self, mirror: MatrixTabState) -> ViewFilter:
        full = ViewFilter(
            side=normalize_matrix_view(mirror.matrix_input.side),
            low=round(float(mirror.matrix_input.low_strike), 4),
            high=round(float(mirror.matrix_input.high_strike), 4),
            widths=tuple(round(float(width), 4) for width in mirror.matrix_input.widths),
        )
        chosen = self._view_filters.get(matrix_tab_identity(mirror.matrix_input))
        if chosen is None:
            return full
        widths = tuple(width for width in full.widths if width in chosen.widths) or full.widths
        low = min(max(chosen.low, full.low), full.high)
        high = max(min(chosen.high, full.high), low)
        side = chosen.side if chosen.side in self._allowed_sides(mirror) else full.side
        return ViewFilter(side=side, low=low, high=high, widths=widths)

    def _build_display_tab(self, mirror: MatrixTabState, previous: MatrixTabState | None) -> MatrixTabState:
        view = self._effective_filter(mirror)
        full = self._effective_filter_full(mirror)
        narrowed = view != full
        display_input = replace(
            mirror.matrix_input,
            side=view.side,
            low_strike=view.low,
            high_strike=view.high,
            widths=view.widths,
            width_bands=matrix_width_bands(view.widths) or mirror.matrix_input.width_bands,
        )
        display = MatrixTabState(
            tab_id=mirror.tab_id,
            label=matrix_tab_label(display_input) + (" *" if narrowed else ""),
            matrix_input=display_input,
            row_strikes=(),
            matrix_plan=MatrixSelectionPlan(visible={}, context_by_width={}),
            # Shared with the mirror: one set of live quotes feeds both.
            leg_quotes=mirror.leg_quotes,
            leg_quote_ids=mirror.leg_quote_ids,
            selected_cell_key=previous.selected_cell_key if previous is not None else None,
            is_stale=mirror.is_stale,
            underlying_price_hint=mirror.underlying_price_hint,
        )
        if mirror.is_stale:
            display.row_strikes = tuple(
                strike for strike in mirror.row_strikes if view.low <= strike <= view.high
            )
            return display

        def rows(strikes: Sequence[float]) -> tuple[float, ...]:
            return tuple(strike for strike in strikes if view.low <= round(float(strike), 4) <= view.high)

        if mirror.side_states:
            if view.side == MATRIX_VIEW_BOTH:
                display.side_states = {
                    right: MatrixSideState(
                        right=right,
                        row_strikes=rows(side.row_strikes),
                        matrix_plan=_filter_plan(side.matrix_plan, view.low, view.high, view.widths),
                    )
                    for right, side in mirror.side_states.items()
                }
                display.row_strikes = rows(mirror.row_strikes)
            else:
                side = mirror.side_states[view.side]
                display.matrix_plan = _filter_plan(side.matrix_plan, view.low, view.high, view.widths)
                display.row_strikes = rows(side.row_strikes)
        else:
            display.matrix_plan = _filter_plan(mirror.matrix_plan, view.low, view.high, view.widths)
            display.row_strikes = rows(mirror.row_strikes)
        return display

    @staticmethod
    def _effective_filter_full(mirror: MatrixTabState) -> ViewFilter:
        return ViewFilter(
            side=normalize_matrix_view(mirror.matrix_input.side),
            low=round(float(mirror.matrix_input.low_strike), 4),
            high=round(float(mirror.matrix_input.high_strike), 4),
            widths=tuple(round(float(width), 4) for width in mirror.matrix_input.widths),
        )

    def _rebuild_tab_from_quotes(self, tab: MatrixTabState) -> None:
        mirror = self._mirror_tabs.get(tab.tab_id)
        if mirror is None or mirror.is_stale or tab.is_stale:
            OptionsMatrixApp._rebuild_tab_from_quotes(self, tab)
            return
        # The identical computation the mini PC performs, on its whole tab.
        OptionsMatrixApp._rebuild_tab_from_quotes(self, mirror)
        self._fill_display_snapshots(tab, mirror)

    def _fill_display_snapshots(self, display: MatrixTabState, mirror: MatrixTabState) -> None:
        if display.side_states:
            display.matrix_snapshots = {}
            display.ranked_signals = []
            for right in ("PUT", "CALL"):
                side = display.side_states.get(right)
                source = mirror.side_states.get(right)
                if side is None or source is None:
                    continue
                side.matrix_snapshots = {
                    key: snapshot
                    for key, snapshot in source.matrix_snapshots.items()
                    if key in side.matrix_plan.visible
                }
                side.ranked_signals = build_ranked_matrix_signals(side.matrix_snapshots, signal_method=self.signal_method)
                display.ranked_signals.extend(side.ranked_signals)
        else:
            if mirror.side_states:
                source = mirror.side_states[normalize_matrix_view(display.matrix_input.side)].matrix_snapshots
            else:
                source = mirror.matrix_snapshots
            display.matrix_snapshots = {
                key: snapshot for key, snapshot in source.items() if key in display.matrix_plan.visible
            }
            display.ranked_signals = build_ranked_matrix_signals(display.matrix_snapshots, signal_method=self.signal_method)
        self._ensure_tab_selected_cell(display)

    # ------------------------------------------------------------------
    # View controls
    # ------------------------------------------------------------------
    def _activate_matrix_tab(self, tab_id: int) -> None:
        super()._activate_matrix_tab(tab_id)
        self._refresh_view_controls()

    def _active_mirror(self) -> MatrixTabState | None:
        return self._mirror_tabs.get(self.active_tab_id) if self.active_tab_id is not None else None

    def _refresh_view_controls(self) -> None:
        if not hasattr(self, "view_side_combo"):
            return
        for child in self.view_width_frame.winfo_children():
            child.destroy()
        self.view_width_vars = {}
        mirror = self._active_mirror()
        if mirror is None:
            for combo in (self.view_side_combo, self.view_low_combo, self.view_high_combo):
                combo["values"] = []
            for variable in (self.view_side_var, self.view_low_var, self.view_high_var):
                variable.set("")
            return
        view = self._effective_filter(mirror)
        self.view_side_combo["values"] = list(self._allowed_sides(mirror))
        self.view_side_var.set(view.side)
        strikes = [format_strike(strike) for strike in mirror.row_strikes] or [
            format_strike(mirror.matrix_input.low_strike),
            format_strike(mirror.matrix_input.high_strike),
        ]
        self.view_low_combo["values"] = strikes
        self.view_high_combo["values"] = strikes
        self.view_low_var.set(format_strike(view.low))
        self.view_high_var.set(format_strike(view.high))
        for index, width in enumerate(round(float(w), 4) for w in mirror.matrix_input.widths):
            variable = tk.BooleanVar(value=width in view.widths)
            self.view_width_vars[width] = variable
            ttk.Checkbutton(self.view_width_frame, text=f"{width:g}", variable=variable).grid(
                row=0, column=index, padx=(0, 4)
            )

    def _apply_view_from_controls(self) -> None:
        mirror = self._active_mirror()
        if mirror is None:
            self._set_status("Nothing to filter yet: the mini PC has no live tab here.")
            return
        try:
            low = round(float(self.view_low_var.get()), 4)
            high = round(float(self.view_high_var.get()), 4)
        except ValueError:
            self._set_status("Choose a low and high strike from the lists.")
            return
        if low > high:
            low, high = high, low
        widths = tuple(width for width, variable in self.view_width_vars.items() if variable.get())
        if not widths:
            messagebox.showerror("View", "Choose at least one width.", parent=self.root)
            return
        side = self.view_side_var.get() or normalize_matrix_view(mirror.matrix_input.side)
        chosen = ViewFilter(side=side, low=low, high=high, widths=widths)
        identity = matrix_tab_identity(mirror.matrix_input)
        if chosen == self._effective_filter_full(mirror):
            self._view_filters.pop(identity, None)
        else:
            self._view_filters[identity] = chosen
        self._refresh_display_tab(mirror.tab_id)
        self._set_status(f"Showing {self._display_tab(mirror.tab_id).label}.")

    def _reset_view_filter(self) -> None:
        mirror = self._active_mirror()
        if mirror is None:
            return
        self._view_filters.pop(matrix_tab_identity(mirror.matrix_input), None)
        self._refresh_display_tab(mirror.tab_id)
        self._set_status("Showing the mini PC's whole tab.")

    def _display_tab(self, tab_id: int) -> MatrixTabState | None:
        return next((tab for tab in self.matrix_tabs if tab.tab_id == tab_id), None)

    def _refresh_display_tab(self, tab_id: int) -> None:
        mirror = self._mirror_tabs.get(tab_id)
        if mirror is None:
            return
        self.matrix_tabs = [
            self._build_display_tab(mirror, tab) if tab.tab_id == tab_id else tab
            for tab in self.matrix_tabs
        ]
        display = self._display_tab(tab_id)
        if display is not None:
            self._rebuild_tab_from_quotes(display)
        self._sync_view_from_active_tab()
        self._redraw_matrix_tabs()
        self._redraw_all()
        self._refresh_view_controls()
        if hasattr(self, "positions"):
            self._refresh_position_alerts()

    # ------------------------------------------------------------------
    # Positions watch the full mirror tabs
    # ------------------------------------------------------------------
    def _loaded_position_pairs(self) -> dict[Any, Any]:
        pairs: dict[Any, Any] = {}
        if not self.positions:
            return pairs
        displays = {tab.tab_id: tab for tab in self.matrix_tabs}
        for tab_id, mirror in self._mirror_tabs.items():
            display = displays.get(tab_id)
            if display is None or mirror.is_stale or display.is_stale:
                continue
            groups = (
                ((right, side.matrix_plan, side.matrix_snapshots) for right, side in mirror.side_states.items())
                if mirror.side_states
                else ((None, mirror.matrix_plan, mirror.matrix_snapshots),)
            )
            for right, plan, snapshots in groups:
                for local_key, selection in plan.visible.items():
                    key = position_key_for_selection(selection)
                    if key in self.positions and key not in pairs:
                        cell_key = self._display_cell_key(display, right, local_key)
                        pairs[key] = (display, cell_key, selection, snapshots.get(local_key))
        return pairs

    @staticmethod
    def _display_cell_key(display: MatrixTabState, right: str | None, local_key: tuple[float, float]) -> Any:
        if display.side_states:
            side = display.side_states.get(right) if right is not None else None
            return (right, *local_key) if side is not None and local_key in side.matrix_plan.visible else None
        if right is not None and normalize_matrix_view(display.matrix_input.side) != right:
            return None
        return local_key if local_key in display.matrix_plan.visible else None

    def _show_position_in_matrix(self, _event: Any = None) -> None:
        location = self._position_locations.get(self._selected_position_row())
        if location is not None and location[1] is None:
            # The position's row is hidden by this PC's view: show the whole tab.
            mirror = self._mirror_tabs.get(location[0].tab_id)
            if mirror is not None:
                self._view_filters.pop(matrix_tab_identity(mirror.matrix_input), None)
                self._refresh_display_tab(mirror.tab_id)
        super()._show_position_in_matrix(_event)

    # ------------------------------------------------------------------
    # Connection settings
    # ------------------------------------------------------------------
    def _open_connection_dialog(self) -> None:
        from .connection_dialog import ConnectionDialog

        ConnectionDialog(self)

    def apply_viewer_settings(self, settings: ViewerSettings, *, connect: bool) -> None:
        self.viewer_settings = settings
        try:
            save_viewer_settings(settings, self.viewer_settings_path)
        except OSError as exc:
            self._set_status(f"Connection settings used, but they could not be saved: {exc}")
        if connect and settings.complete:
            self._link_state = LINK_OFFLINE
            self._start_client()
        self._set_connected_state(self.remote.connected)
