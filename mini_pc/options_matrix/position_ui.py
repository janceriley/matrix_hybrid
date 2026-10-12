"""Saved position controls and continuous in-app exit signals."""
from __future__ import annotations

import math
import tkinter as tk
from dataclasses import replace
from datetime import datetime, timezone
from tkinter import ttk

from .models import (
    LIVE_MARKET_DATA_TYPE, SIGNAL_BUY, MonitoredSideSnapshot,
    build_synthetic_vertical_quote, format_price, format_strike, pair_key_for_pair,
)
from .positions import (
    POSITION_SOLD, PositionRecord, evaluate_position,
    load_positions, position_key_for_selection, position_store_path, save_positions,
)
from .window_placement import center_dialog_on_parent_monitor

POSITION_QUOTE_MAX_AGE_SECONDS = 60.0


def position_record_for_selection(app, selection):
    if selection is None:
        return None
    return getattr(app, "positions", {}).get(position_key_for_selection(selection))


def position_exit_for_selection(app, selection):
    record = position_record_for_selection(app, selection)
    if record is None or not record.armed:
        return None
    return getattr(app, "_position_results", {}).get(record.key)


def position_quotes_live(app, tab, selection, now):
    """Only confirmed current leg books may show an exit signal."""
    if tab.is_stale or not bool(getattr(getattr(app, "service", None), "connected", False)):
        return False
    if not getattr(app, "_service_connected", True):
        return False
    diagnostics = getattr(app, "_subscription_diagnostics_by_con_id", {})
    for leg in selection.legs:
        con_id = leg.contract.con_id
        diagnostic = diagnostics.get(con_id)
        if getattr(diagnostic, "state", None) != "subscribed":
            return False
        generation = getattr(diagnostic, "generation", None)
        if generation is None or generation != getattr(app, "_quote_event_generation", None):
            return False
        quote = tab.leg_quotes.get(con_id)
        if quote is None or quote.market_data_type != LIVE_MARKET_DATA_TYPE:
            return False
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (quote.bid, quote.ask)):
            return False
        if quote.bid < 0 or quote.ask < quote.bid:
            return False
        for confirmed in (quote.bid_confirmed_at, quote.ask_confirmed_at):
            if confirmed is None or confirmed.tzinfo is None:
                return False
            age = (now - confirmed).total_seconds()
            if not 0 <= age <= POSITION_QUOTE_MAX_AGE_SECONDS:
                return False
    return True


def position_label(key):
    ticker, expiry, right, lower, upper = key
    return f"{ticker} {expiry} {right} {format_strike(lower)}/{format_strike(upper)}"


def position_price(price):
    if price is not None and round(price, 4) != round(price, 2):
        return f"{price:.4f}".rstrip("0")
    return format_price(price)


class PositionAlertsMixin:
    def _initialize_positions(self, settings_root):
        self._positions_path = settings_root / "positions.json" if settings_root is not None else position_store_path()
        self.positions = load_positions(self._positions_path)
        self._position_results = {}
        self._position_locations = {}
        self._position_rows_signature = None
        self._position_tick_id = None

    def _build_position_controls(self, notebook, detail):
        controls = ttk.Frame(detail)
        controls.grid(row=2, column=0, sticky="ew", pady=(3, 0))
        controls.columnconfigure(0, weight=1)
        self.position_summary_var = tk.StringVar(value="Select a spread to enter your position.")
        summary = ttk.Label(controls, textvariable=self.position_summary_var, wraplength=360)
        summary.grid(row=0, column=0, sticky="w")
        self.position_edit_button = ttk.Button(controls, text="Position…", command=self._edit_selected_position)
        self.position_edit_button.grid(row=0, column=1, padx=4)
        controls.bind("<Configure>", lambda event: summary.configure(
            wraplength=max(150, event.width - self.position_edit_button.winfo_reqwidth() - 16)))
        positions_tab = ttk.Frame(notebook)
        notebook.add(positions_tab, text="Positions / Exit Alerts")
        positions_tab.columnconfigure(0, weight=1)
        positions_tab.rowconfigure(0, weight=1)
        columns = ("pair", "side", "average", "edge", "target", "mid", "profit", "status")
        self.position_tree = ttk.Treeview(positions_tab, columns=columns, show="headings", height=7, selectmode="browse")
        for column, label, width in zip(columns,
                ("POSITION", "ENTRY", "AVERAGE", "WIDTH EDGE", "EXIT TARGET", "MID", "MOVE FROM ENTRY", "STATUS"),
                (270, 75, 80, 90, 95, 75, 120, 210)):
            self.position_tree.heading(column, text=label)
            self.position_tree.column(column, width=width, minwidth=60, stretch=column in {"pair", "status"})
        self.position_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(positions_tab, orient="vertical", command=self.position_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.position_tree.configure(yscrollcommand=scroll.set)
        self.position_tree.tag_configure("BUY", background="#b7ebcc", foreground="#146c43")
        self.position_tree.tag_configure("SELL", background="#ffc9c5", foreground="#a12118")
        self.position_tree.bind("<Double-1>", self._show_position_in_matrix)
        self._position_tree_keys = {}
        actions = ttk.Frame(positions_tab)
        actions.grid(row=1, column=0, sticky="w", pady=3)
        for label, command in (("Edit position", self._edit_position_row), ("Arm / Pause", self._toggle_position_row),
                               ("Delete position", self._clear_position_row)):
            ttk.Button(actions, text=label, command=command).pack(side="left", padx=(0, 6))
        ttk.Label(positions_tab, text="Targets use your configured width edges and the market midpoint. Unloaded positions are paused.").grid(row=2, column=0, sticky="w")

    def _loaded_position_pairs(self):
        pairs = {}
        if not self.positions:
            return pairs
        for tab in self.matrix_tabs:
            if tab.is_stale:
                continue
            groups = ((right, side.matrix_plan, side.matrix_snapshots) for right, side in tab.side_states.items()) if tab.side_states else ((None, tab.matrix_plan, tab.matrix_snapshots),)
            for right, plan, snapshots in groups:
                for local_key, selection in plan.visible.items():
                    key = position_key_for_selection(selection)
                    if key in self.positions:
                        cell_key = (right, *local_key) if right is not None else local_key
                        pairs[key] = (tab, cell_key, selection, snapshots.get(local_key))
        return pairs

    def _refresh_position_alerts(self, *, now=None):
        if not hasattr(self, "positions"):
            return False
        now = now or datetime.now(timezone.utc)
        self._position_locations = self._loaded_position_pairs()
        results = {}
        for key, position in list(self.positions.items()):
            location = self._position_locations.get(key)
            snapshot = None
            live = location is not None and position_quotes_live(self, location[0], location[2], now)
            if live:
                # A timer/subscription event can precede the deferred matrix
                # redraw. Price from the very same books we just validated.
                selection = location[2]
                snapshot = MonitoredSideSnapshot(
                    title=selection.pair.label,
                    pair_key=pair_key_for_pair(selection.pair),
                    selection=selection,
                    is_center=True,
                    synthetic_quote=build_synthetic_vertical_quote(selection, location[0].leg_quotes),
                )
            result = evaluate_position(position, snapshot, self.formula_settings, live=live)
            results[key] = result
        changed = results != self._position_results
        self._position_results = results
        self._refresh_position_table()
        self._refresh_position_summary()
        return changed

    def _start_position_timer(self):
        def tick():
            self._position_tick_id = None
            if getattr(self, "_shutdown_in_progress", False):
                return
            if self._refresh_position_alerts():
                self._refresh_active_matrix_render()
            self._position_tick_id = self.root.after(1000, tick)
        tick()

    def _persist_positions(self):
        try:
            save_positions(self.positions, self._positions_path)
            return True
        except OSError as exc:
            self._set_status(f"Position alerts could not be saved: {exc}")
            return False

    def _store_position(self, record):
        previous = self.positions.get(record.key)
        self.positions[record.key] = record
        if not self._persist_positions():
            if previous is None:
                self.positions.pop(record.key, None)
            else:
                self.positions[record.key] = previous
            return False
        self._refresh_position_alerts()
        self._redraw_all()
        return True

    def _position_status(self, position, result):
        if not position.armed:
            return "Paused by you"
        if position.key not in self._position_locations:
            return "Paused — pair not loaded"
        if result is None or result.status == "waiting":
            return "Waiting for confirmed live quotes"
        if result.signal is not None:
            return "EXIT BUY — target reached" if result.signal == SIGNAL_BUY else "EXIT SELL — target reached"
        return "Monitoring"

    def _refresh_position_table(self):
        tree = getattr(self, "position_tree", None)
        if tree is None:
            return
        rows = []
        for key, position in sorted(self.positions.items()):
            result = self._position_results.get(key) or evaluate_position(position, None, self.formula_settings, live=False)
            rows.append((key, (position_label(key), position.side.title(), position_price(position.average_price),
                              position_price(result.edge), position_price(result.target_price), position_price(result.mid),
                              position_price(result.profit), self._position_status(position, result)), result.signal))
        signature = tuple(rows)
        if signature == self._position_rows_signature:
            return
        selected_key = self._selected_position_row()
        tree.delete(*tree.get_children())
        self._position_tree_keys = {}
        for index, (key, values, signal) in enumerate(rows):
            item = str(index)
            tree.insert("", "end", iid=item, values=values, tags=(signal,) if signal else ())
            self._position_tree_keys[item] = key
            if key == selected_key:
                tree.selection_set(item)
        self._position_rows_signature = signature

    def _selected_position_row(self):
        tree = getattr(self, "position_tree", None)
        selection = tree.selection() if tree is not None else ()
        return self._position_tree_keys.get(selection[0]) if selection else None

    def _selected_position_key(self):
        tab = self._active_tab()
        if tab is None or tab.is_stale:
            return None
        selection = self._tab_cell_selection(tab, self.selected_cell_key)
        return position_key_for_selection(selection) if selection is not None else None

    def _refresh_position_summary(self):
        summary = getattr(self, "position_summary_var", None)
        if summary is None:
            return
        key = self._selected_position_key()
        self.position_edit_button.configure(state="normal" if key is not None else "disabled")
        if key is None:
            summary.set("Select a live spread to enter your position. Saved positions are in Positions / Exit Alerts.")
            return
        position = self.positions.get(key)
        if position is None:
            summary.set("No position saved. Position… lets you enter Bought / Sold and your average entry.")
            return
        result = self._position_results.get(key) or evaluate_position(position, None, self.formula_settings, live=False)
        action = "BUY TO CLOSE" if position.side == POSITION_SOLD else "SELL TO CLOSE"
        summary.set(f"{position.side.title()} avg {position_price(position.average_price)} · Edge {position_price(result.edge)} · "
                    f"{action} at {position_price(result.target_price)} · Mid {position_price(result.mid)}\n"
                    f"{self._position_status(position, result)}")

    def _edit_selected_position(self):
        key = self._selected_position_key()
        if key is not None:
            self._open_position_editor(key)

    def _edit_position_row(self):
        key = self._selected_position_row()
        if key is not None:
            self._open_position_editor(key)

    def _open_position_editor(self, key):
        existing = self.positions.get(key)
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("Position exit alert")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        box = ttk.Frame(dialog, padding=14)
        box.grid(sticky="nsew")
        ttk.Label(box, text=position_label(key)).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        side = tk.StringVar(value=existing.side.title() if existing else "Sold")
        average = tk.StringVar(value=position_price(existing.average_price) if existing else "")
        armed = tk.BooleanVar(value=existing.armed if existing else True)
        ttk.Label(box, text="Entry side").grid(row=1, column=0, sticky="w")
        ttk.Combobox(box, textvariable=side, values=("Bought", "Sold"), state="readonly", width=16).grid(row=1, column=1, pady=4)
        ttk.Label(box, text="Average entry ($)").grid(row=2, column=0, sticky="w", padx=(0, 12))
        entry = ttk.Entry(box, textvariable=average, width=18)
        entry.grid(row=2, column=1, pady=4)
        ttk.Checkbutton(box, text="Arm exit alert", variable=armed).grid(row=3, column=0, columnspan=2, sticky="w", pady=4)
        target = tk.StringVar()
        ttk.Label(box, textvariable=target, wraplength=380).grid(row=4, column=0, columnspan=2, sticky="w", pady=8)
        error = tk.StringVar()
        ttk.Label(box, textvariable=error, foreground="#b42318", wraplength=380).grid(row=5, column=0, columnspan=2, sticky="w")

        def draft():
            return PositionRecord(key, side.get().upper(), float(average.get()), armed.get())

        def preview(*_args):
            try:
                record = draft()
                result = evaluate_position(record, None, self.formula_settings, live=False)
                action = "Buy to close" if record.side == POSITION_SOLD else "Sell to close"
                target.set(f"Width edge: ${position_price(result.edge)}. {action} when market mid is "
                           f"{'at or below' if record.side == POSITION_SOLD else 'at or above'} ${position_price(result.target_price)}.")
            except ValueError:
                target.set("Enter your average to see the target from your configured width edge.")

        def save():
            try:
                record = draft()
            except ValueError as exc:
                error.set(str(exc))
                return
            if self._store_position(record):
                dialog.destroy()
            else:
                error.set("Could not save the position. See the status message and try again.")

        def delete():
            if self._delete_position(key):
                dialog.destroy()
            else:
                error.set("Could not delete the position. See the status message and try again.")

        average.trace_add("write", preview)
        side.trace_add("write", preview)
        preview()
        buttons = ttk.Frame(box)
        buttons.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        if existing is not None:
            ttk.Button(buttons, text="Delete position", command=delete).pack(side="left", padx=(0, 12))
        ttk.Button(buttons, text="Save position", command=save).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right", padx=5)
        dialog.bind("<Return>", lambda _event: save())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        center_dialog_on_parent_monitor(dialog, self.root)
        dialog.deiconify()
        dialog.grab_set()
        entry.focus_set()

    def _toggle_position_row(self):
        key = self._selected_position_row()
        if key in self.positions:
            position = self.positions[key]
            self._store_position(replace(position, armed=not position.armed))

    def _clear_position_row(self):
        self._delete_position(self._selected_position_row())

    def _delete_position(self, key):
        if key not in self.positions:
            return True
        previous = self.positions.pop(key)
        if not self._persist_positions():
            self.positions[key] = previous
            return False
        self._refresh_position_alerts()
        self._redraw_all()
        self._set_status(f"Position deleted: {position_label(key)}")
        return True

    def _show_position_in_matrix(self, _event=None):
        location = self._position_locations.get(self._selected_position_row())
        if location is None:
            return
        tab, cell_key, _selection, _snapshot = location
        self.active_tab_id = tab.tab_id
        tab.selected_cell_key = cell_key
        self._sync_view_from_active_tab()
        self._redraw_matrix_tabs()
        self._redraw_all()
