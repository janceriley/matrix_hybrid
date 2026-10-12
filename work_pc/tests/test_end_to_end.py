"""Mini PC (demo data) and work PC viewer connected over this PC's loopback.

Runs both real Tk windows in one process, shares the matrix over the actual
network link, then freezes prices and checks every number matches. Needs a
desktop session with Tk; it never contacts Schwab and stays on 127.0.0.1.
"""

from __future__ import annotations

import gc
import socket
import tempfile
import time
import unittest
from pathlib import Path

try:
    import tkinter as tk

    _root = tk.Tk()
    _root.destroy()
    TK_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the machine
    TK_AVAILABLE = False

from options_matrix.app import OptionsMatrixApp
from options_matrix.demo_service import DemoMarketDataService
from options_matrix.models import RIGHT_CALL

from matrix_viewer.viewer_app import ViewerApp
from matrix_viewer.viewer_settings import ViewerSettings

COMPARED_FIELDS = (
    "synthetic_mid",
    "fair_value_edge_curve_mid",
    "fair_value_edge_curve_edge",
    "fair_value_edge_curve_buy_endpoint",
    "fair_value_edge_curve_sell_endpoint",
    "fair_value_edge_curve_fit_points",
    "opportunity_direction",
    "opportunity_price",
    "anomaly_direction",
)


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _pump(apps, seconds, until=None) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for app in apps:
            app.root.update()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return bool(until()) if until is not None else True


def _numbers(snapshot) -> tuple:
    return tuple(getattr(snapshot, name) for name in COMPARED_FIELDS)


@unittest.skipUnless(TK_AVAILABLE, "needs a desktop session with Tk")
class StudioToViewerEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        # These tests drive Tk with update() instead of mainloop(). Garbage
        # collection on a network thread would then make Tk objects wait for a
        # main loop that never runs, so collect only on this thread.
        gc.collect()
        gc.disable()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.demo = DemoMarketDataService(seed=11, tick_seconds=0.1)
        self.studio = OptionsMatrixApp(settings_root=root / "studio", market_data_service=self.demo, demo=True)
        self.studio.share_bind_host = "127.0.0.1"
        self.port = _free_port()
        self.studio._update_studio_settings(share_port=self.port, share_enabled=True)
        self.studio.start_services()
        self.viewer = ViewerApp(settings_root=root / "viewer")
        self.apps = [self.studio, self.viewer]

    def tearDown(self) -> None:
        self.demo._stop.set()
        for app in (self.viewer, self.studio):
            try:
                app.stop_services()
                app.root.destroy()
            except tk.TclError:
                pass
        self.viewer = self.studio = self.apps = None
        gc.enable()
        gc.collect()
        self.temporary.cleanup()

    def _load_studio_matrix(self) -> None:
        studio = self.studio
        studio._connect()
        self.assertTrue(_pump([studio], 5, lambda: studio.service.connected))
        studio.ticker_var.set("SPY")
        studio._load_ticker()
        self.assertTrue(_pump([studio], 5, lambda: bool(studio.option_records)))
        studio.side_var.set("BOTH")
        studio.low_strike_var.set("594")
        studio.high_strike_var.set("607")
        for band, variable in studio.width_vars.items():
            variable.set(band in (1.0, 2.0, 3.0))
        studio._load_matrix()
        self.assertTrue(_pump([studio], 5, lambda: any(not tab.is_stale for tab in studio.matrix_tabs)))

    def _connect_viewer(self, password: str | None = None) -> None:
        self.viewer.apply_viewer_settings(
            ViewerSettings(
                host="127.0.0.1",
                port=self.port,
                password=password or self.studio.studio_settings.share_password,
            ),
            connect=False,
        )
        self.viewer.start_services()

    def test_viewer_numbers_match_the_mini_pc_exactly(self) -> None:
        self._load_studio_matrix()
        self._connect_viewer()
        live = _pump(
            self.apps,
            10,
            lambda: self.viewer._link_state == "live"
            and any(not tab.is_stale for tab in self.viewer.matrix_tabs),
        )
        self.assertTrue(live, self.viewer._link_message)
        _pump(self.apps, 3)
        # Freeze the made-up market, let the last updates arrive, then compare.
        self.demo._stop.set()
        _pump(self.apps, 1.5)

        studio_tab = self.studio.matrix_tabs[0]
        mirror = self.viewer._mirror_tabs[studio_tab.tab_id]
        compared = 0
        for right, side in studio_tab.side_states.items():
            for key, snapshot in side.matrix_snapshots.items():
                other = mirror.side_states[right].matrix_snapshots[key]
                self.assertEqual(_numbers(snapshot), _numbers(other), (right, key))
                compared += 1
        self.assertGreater(compared, 50)
        self.assertEqual(self.viewer.underlying_quote.mark, self.studio.underlying_quote.mark)
        studio_signals = [(row.pair_key, row.width, row.signal) for row in self.studio.ranked_signals]
        viewer_signals = [(row.pair_key, row.width, row.signal) for row in self.viewer.ranked_signals]
        self.assertEqual(studio_signals, viewer_signals)
        self.assertTrue(self.viewer.service.connected)
        self.assertIn("LIVE", self.viewer.connection_var.get())

    def test_narrowed_view_shows_a_subset_with_the_full_tab_numbers(self) -> None:
        self._load_studio_matrix()
        self._connect_viewer()
        self.assertTrue(_pump(self.apps, 10, lambda: any(not t.is_stale for t in self.viewer.matrix_tabs)))
        self.demo._stop.set()
        _pump(self.apps, 1.5)
        viewer = self.viewer
        viewer.view_side_var.set(RIGHT_CALL)
        viewer.view_low_var.set("598")
        viewer.view_high_var.set("603")
        for width, variable in viewer.view_width_vars.items():
            variable.set(width in (1.0, 2.0))
        viewer._apply_view_from_controls()

        display = viewer.matrix_tabs[0]
        self.assertTrue(display.label.endswith("*"))
        self.assertEqual(display.row_strikes, (598.0, 599.0, 600.0, 601.0, 602.0, 603.0))
        self.assertTrue(display.matrix_plan.visible)
        studio_calls = self.studio.matrix_tabs[0].side_states[RIGHT_CALL].matrix_snapshots
        for key, snapshot in display.matrix_snapshots.items():
            lower, width = key
            self.assertTrue(598.0 <= lower and lower + width <= 603.0 and width in (1.0, 2.0))
            self.assertEqual(_numbers(snapshot), _numbers(studio_calls[key]))
        viewer._reset_view_filter()
        self.assertFalse(viewer.matrix_tabs[0].label.endswith("*"))

    def test_wrong_password_stops_without_showing_data(self) -> None:
        self._load_studio_matrix()
        self._connect_viewer(password="WRONG-PASS-WORD")
        stopped = _pump(self.apps, 10, lambda: self.viewer._link_state == "stopped")
        self.assertTrue(stopped)
        self.assertIn("WRONG PASSWORD", self.viewer.connection_var.get())
        self.assertFalse(self.viewer.service.connected)
        self.assertFalse(self.viewer.matrix_tabs)

    def test_positions_on_hidden_rows_keep_being_watched(self) -> None:
        from options_matrix.positions import PositionRecord, position_key_for_selection

        self._load_studio_matrix()
        self._connect_viewer()
        self.assertTrue(_pump(self.apps, 10, lambda: any(not t.is_stale for t in self.viewer.matrix_tabs)))
        _pump(self.apps, 1)
        viewer = self.viewer
        mirror = viewer._mirror_tabs[viewer.matrix_tabs[0].tab_id]
        selection = mirror.side_states[RIGHT_CALL].matrix_plan.visible[(600.0, 1.0)]
        key = position_key_for_selection(selection)
        self.assertTrue(viewer._store_position(PositionRecord(key, "SOLD", 0.5)))
        try:
            # Show only puts: the call position's row is hidden from view.
            viewer.view_side_var.set("PUT")
            viewer._apply_view_from_controls()
            viewer._refresh_position_alerts()
            location = viewer._position_locations[key]
            self.assertIsNone(location[1])
            status = viewer._position_status(viewer.positions[key], viewer._position_results[key])
            self.assertNotIn("not loaded", status)
            # Double-clicking the position shows the whole tab again and selects it.
            viewer._selected_position_row = lambda: key
            viewer._show_position_in_matrix()
            self.assertFalse(viewer.matrix_tabs[0].label.endswith("*"))
            self.assertEqual(viewer.matrix_tabs[0].selected_cell_key, (RIGHT_CALL, 600.0, 1.0))
        finally:
            viewer._delete_position(key)

    def test_different_program_versions_refuse_to_show_numbers(self) -> None:
        self._load_studio_matrix()
        self.viewer._local_fingerprint = "different"
        self._connect_viewer()
        self.assertTrue(_pump(self.apps, 10, lambda: self.viewer._link_state == "stopped"))
        self.assertIn("UPDATE NEEDED", self.viewer.connection_var.get())
        self.assertFalse(self.viewer.matrix_tabs)

    def test_mini_pc_records_while_a_viewer_watches(self) -> None:
        self._load_studio_matrix()
        self._connect_viewer()
        recordings = Path(self.temporary.name) / "recordings"
        self.studio._update_studio_settings(recording_folder=str(recordings))
        self.assertTrue(_pump(self.apps, 10, lambda: any(not t.is_stale for t in self.viewer.matrix_tabs)))
        self.studio.start_capture()
        self.assertTrue(_pump(self.apps, 10, lambda: self.studio.recorder is not None and self.studio.recorder.running))
        _pump(self.apps, 3)
        self.assertIn("REC ON", self.viewer.recorder_indicator_var.get())
        self.studio._stop_recording(silent=True)
        self.assertTrue(_pump(self.apps, 15, lambda: self.studio.recorder is None))
        parts = list(recordings.rglob("part-*.parquet"))
        self.assertEqual(len(parts), 1)
        import pyarrow.parquet as pq

        table = pq.read_table(parts[0])
        self.assertEqual(table.num_columns, 42)
        self.assertGreater(table.num_rows, 50)

    def test_link_loss_greys_out_the_viewer_and_it_recovers(self) -> None:
        self._load_studio_matrix()
        self._connect_viewer()
        self.assertTrue(_pump(self.apps, 10, lambda: any(not t.is_stale for t in self.viewer.matrix_tabs)))
        self.studio.services.stop()
        self.assertTrue(_pump(self.apps, 8, lambda: self.viewer._link_state != "live"))
        self.assertTrue(all(tab.is_stale for tab in self.viewer.matrix_tabs))
        self.assertFalse(self.viewer.service.connected)
        self.studio.services = None
        self.studio.start_services()
        recovered = _pump(
            self.apps,
            15,
            lambda: self.viewer._link_state == "live"
            and any(not tab.is_stale for tab in self.viewer.matrix_tabs),
        )
        self.assertTrue(recovered, self.viewer._link_message)


if __name__ == "__main__":
    unittest.main()
