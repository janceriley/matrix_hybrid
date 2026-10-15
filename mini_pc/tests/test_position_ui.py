from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from options_matrix.app import (
    BUY_COLOR, SELL_COLOR, ITM_SELECTED_BG, POSITION_BUY_BG, POSITION_SELL_BG,
    OptionsMatrixApp, compact_matrix_signal_text, resolve_matrix_cell_fills,
)
from options_matrix.models import (
    DEFAULT_FORMULA_SETTINGS,
    DIRECTION_DEBIT,
    LIVE_MARKET_DATA_TYPE,
    MatrixSelectionPlan,
    MonitoredSideSnapshot,
    OptionContractRecord,
    QuoteSnapshot,
    RIGHT_CALL,
    RIGHT_PUT,
    SIGNAL_BUY,
    SIGNAL_SELL,
    SyntheticSpreadQuote,
    VerticalPair,
    resolve_vertical_selection,
)
from options_matrix.position_ui import (
    POSITION_QUOTE_MAX_AGE_SECONDS,
    PositionAlertsMixin,
    average_entry_from_text,
    position_exit_for_selection,
    position_quotes_live,
    position_record_for_selection,
)
from options_matrix.positions import (
    POSITION_BOUGHT,
    POSITION_SOLD,
    PositionRecord,
    load_positions,
    position_key_for_selection,
)


NOW = datetime(2030, 1, 10, 15, 0, tzinfo=timezone.utc)


def _snapshot(mid=0.38, *, right=RIGHT_CALL, con_id=1, ticker="SPY", expiration="20300117"):
    lower = OptionContractRecord(
        con_id=con_id, symbol=ticker, expiration=expiration, strike=100.0, right=right,
        exchange="SMART", currency="USD", trading_class=ticker, multiplier="100",
        local_symbol=f"{ticker} {right} 100",
    )
    upper = replace(lower, con_id=con_id + 1, strike=101.0, local_symbol=f"{ticker} {right} 101")
    selection = resolve_vertical_selection(VerticalPair(right, lower, upper), DIRECTION_DEBIT)
    return MonitoredSideSnapshot(
        title=selection.label, pair_key=selection.pair.label, selection=selection,
        synthetic_quote=SyntheticSpreadQuote(bid=mid - 0.02, ask=mid + 0.02),
    )


def _tab(*snapshots, tab_id=1, both=False):
    def group(snapshot):
        cell = (snapshot.selection.pair.lower.strike, snapshot.width)
        return SimpleNamespace(
            matrix_plan=MatrixSelectionPlan({cell: snapshot.selection}, {snapshot.width: (snapshot.selection,)}),
            matrix_snapshots={cell: snapshot},
        )
    primary = group(snapshots[0])
    tab = SimpleNamespace(
        tab_id=tab_id, is_stale=False,
        side_states={snapshot.selection.pair.right: group(snapshot) for snapshot in snapshots} if both else {},
        matrix_plan=primary.matrix_plan, matrix_snapshots=primary.matrix_snapshots,
        leg_quotes={}, selected_cell_key=None,
        matrix_input=SimpleNamespace(
            ticker_symbol=snapshots[0].selection.pair.lower.symbol,
            raw_expiration=snapshots[0].selection.pair.lower.expiration,
        ),
    )
    for snapshot in snapshots:
        _set_leg_mid(tab, snapshot, snapshot.synthetic_mid)
    return tab


def _set_leg_mid(tab, snapshot, mid):
    for leg in snapshot.selection.legs:
        leg_mid = 1.0 + mid if leg.action == "BUY" else 1.0
        tab.leg_quotes[leg.contract.con_id] = QuoteSnapshot(
            label=leg.contract.local_symbol, contract_con_id=leg.contract.con_id,
            bid=round(leg_mid - 0.05, 4), ask=round(leg_mid + 0.05, 4),
            market_data_type=LIVE_MARKET_DATA_TYPE,
            updated_at=NOW, bid_confirmed_at=NOW, ask_confirmed_at=NOW,
        )


class _Host(PositionAlertsMixin):
    def __init__(self, root: Path):
        self._initialize_positions(root)
        self.now = NOW
        self.matrix_tabs = []
        self.formula_settings = DEFAULT_FORMULA_SETTINGS
        self.service = SimpleNamespace(connected=True)
        self._quote_event_generation = 4
        self._subscription_diagnostics_by_con_id = {}
        self.status = ""
        self.row_key = None
        self.redraws = 0
        self.active_tab_id = 1

    def add_tab(self, tab):
        self.matrix_tabs.append(tab)
        for con_id in tab.leg_quotes:
            self._subscription_diagnostics_by_con_id[con_id] = SimpleNamespace(state="subscribed", generation=4)

    def _refresh_position_alerts(self, *, now=None):
        return super()._refresh_position_alerts(now=now or self.now)

    def _selected_position_row(self):
        return self.row_key

    def _set_status(self, message):
        self.status = message

    def _redraw_all(self):
        self.redraws += 1

    def _sync_view_from_active_tab(self):
        pass

    def _redraw_matrix_tabs(self):
        pass


class PositionUiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.host = _Host(self.root)
        self.snapshot = _snapshot()
        self.tab = _tab(self.snapshot)
        self.host.add_tab(self.tab)
        self.key = position_key_for_selection(self.snapshot.selection)

    def store(self, *, side=POSITION_SOLD, average=0.48, armed=True):
        position = PositionRecord(self.key, side, average, armed=armed)
        self.assertTrue(self.host._store_position(position))
        self.host.row_key = self.key
        return self.host.positions[self.key]

    def set_mid(self, value):
        self.snapshot.synthetic_quote = SyntheticSpreadQuote(bid=value - 0.02, ask=value + 0.02)
        _set_leg_mid(self.tab, self.snapshot, value)

    def test_only_current_confirmed_live_leg_books_are_eligible(self):
        selection = self.snapshot.selection
        self.assertTrue(position_quotes_live(self.host, self.tab, selection, NOW))
        con_id = selection.legs[0].contract.con_id
        original = self.tab.leg_quotes[con_id]
        mutations = (
            {"market_data_type": 3}, {"bid": None}, {"ask": float("nan")},
            {"bid": -1.0}, {"bid": 1.2, "ask": 1.1},
            {"bid_confirmed_at": None}, {"ask_confirmed_at": None},
            {"bid_confirmed_at": NOW.replace(tzinfo=None)},
            {"ask_confirmed_at": NOW + timedelta(seconds=1)},
            {"bid_confirmed_at": NOW - timedelta(seconds=POSITION_QUOTE_MAX_AGE_SECONDS + 1)},
            {"ask_confirmed_at": NOW - timedelta(seconds=POSITION_QUOTE_MAX_AGE_SECONDS + 1)},
        )
        for values in mutations:
            with self.subTest(values=values):
                self.tab.leg_quotes[con_id] = replace(original, **values)
                self.assertFalse(position_quotes_live(self.host, self.tab, selection, NOW))
        self.tab.leg_quotes[con_id] = original
        for diagnostic in (None, SimpleNamespace(state="pending", generation=4),
                           SimpleNamespace(state="subscribed", generation=3),
                           SimpleNamespace(state="subscribed", generation=None)):
            with self.subTest(diagnostic=diagnostic):
                self.host._subscription_diagnostics_by_con_id[con_id] = diagnostic
                self.assertFalse(position_quotes_live(self.host, self.tab, selection, NOW))
        self.host._subscription_diagnostics_by_con_id[con_id] = SimpleNamespace(state="subscribed", generation=4)
        self.host.service.connected = False
        self.assertFalse(position_quotes_live(self.host, self.tab, selection, NOW))
        self.host.service.connected = True
        self.tab.is_stale = True
        self.assertFalse(position_quotes_live(self.host, self.tab, selection, NOW))

    def test_unconfirmed_quotes_wait_and_signal_only_after_confirmation(self):
        con_id = self.snapshot.selection.legs[0].contract.con_id
        original = self.tab.leg_quotes[con_id]
        self.tab.leg_quotes[con_id] = replace(original, bid_confirmed_at=None)
        self.store()
        self.assertIsNone(self.host._position_results[self.key].signal)
        self.assertEqual(self.host._position_results[self.key].status, "waiting")
        self.assertEqual(self.host._position_status(self.host.positions[self.key], self.host._position_results[self.key]),
                         "Waiting for confirmed live quotes")
        self.tab.leg_quotes[con_id] = original
        self.host._refresh_position_alerts()
        self.assertEqual(self.host._position_results[self.key].signal, SIGNAL_BUY)

    def test_lagging_matrix_snapshot_cannot_trigger_against_newer_leg_quotes(self):
        # A timer can run between the quote event and the delayed matrix redraw.
        # The old displayed midpoint reached the target; current leg quotes did not.
        _set_leg_mid(self.tab, self.snapshot, 0.60)
        self.store()
        self.assertEqual(self.snapshot.synthetic_mid, 0.38)
        self.assertIsNone(self.host._position_results[self.key].signal)
        self.assertAlmostEqual(self.host._position_results[self.key].mid, 0.60)
        # The next current book can trigger even while the displayed snapshot lags.
        self.snapshot.synthetic_quote = SyntheticSpreadQuote(bid=0.60, ask=0.60)
        _set_leg_mid(self.tab, self.snapshot, 0.38)
        self.host._refresh_position_alerts()
        self.assertEqual(self.host._position_results[self.key].signal, SIGNAL_BUY)
        self.assertAlmostEqual(self.host._position_results[self.key].mid, 0.38)

    def test_continuous_exit_signals_never_open_windows_across_recross_and_restart(self):
        with patch("options_matrix.position_ui.tk.Toplevel") as popup:
            original = self.store()
            self.assertEqual(self.host._position_results[self.key].signal, SIGNAL_BUY)
            for mid, signal in ((0.60, None), (0.38, SIGNAL_BUY), (0.30, SIGNAL_BUY), (0.60, None), (0.38, SIGNAL_BUY)):
                self.set_mid(mid)
                self.host._refresh_position_alerts()
                self.assertEqual(self.host._position_results[self.key].signal, signal)
                expected_status = "Monitoring" if signal is None else "EXIT BUY — target reached"
                self.assertEqual(self.host._position_status(original, self.host._position_results[self.key]), expected_status)
            self.host.formula_settings = replace(DEFAULT_FORMULA_SETTINGS, fair_value_edge_width_rate=0.20)
            self.host._refresh_position_alerts()
            self.assertIsNone(self.host._position_results[self.key].signal)
            self.host.formula_settings = DEFAULT_FORMULA_SETTINGS
            self.tab.is_stale = True
            self.host._refresh_position_alerts()
            self.assertIsNone(self.host._position_results[self.key].signal)
            self.tab.is_stale = False
            self.host._refresh_position_alerts()
            self.assertEqual(self.host._position_results[self.key].signal, SIGNAL_BUY)
            self.assertEqual(load_positions(self.host._positions_path)[self.key], original)
            restarted = _Host(self.root)
            restarted.add_tab(self.tab)
            restarted._refresh_position_alerts()
            self.assertEqual(restarted._position_results[self.key].signal, SIGNAL_BUY)
            popup.assert_not_called()

    def test_pause_keeps_position_information_and_resume_restores_live_exit_signal(self):
        original = self.store()
        self.host._toggle_position_row()
        paused = self.host.positions[self.key]
        self.assertEqual((paused.side, paused.average_price), (original.side, original.average_price))
        self.assertFalse(paused.armed)
        self.assertEqual(self.host._position_results[self.key].status, "paused")
        self.assertIsNone(position_exit_for_selection(self.host, self.snapshot.selection))
        self.host._toggle_position_row()
        self.assertEqual(self.host._position_results[self.key].signal, SIGNAL_BUY)
        self.assertEqual(load_positions(self.host._positions_path)[self.key], original)
        self.set_mid(0.60)
        self.host._refresh_position_alerts()
        self.assertIsNone(self.host._position_results[self.key].signal)
        self.set_mid(0.38)
        self.host._refresh_position_alerts()
        self.assertEqual(self.host._position_results[self.key].signal, SIGNAL_BUY)

    def test_unloaded_positions_remain_saved_and_resume_when_pair_returns(self):
        self.set_mid(0.60)
        original = self.store()
        self.host.matrix_tabs = []
        self.host._refresh_position_alerts()
        self.assertEqual(self.host.positions[self.key], original)
        self.assertEqual(load_positions(self.host._positions_path)[self.key], original)
        self.assertEqual(self.host._position_status(original, self.host._position_results[self.key]),
                         "Paused — pair not loaded")
        self.assertIsNone(self.host._position_results[self.key].signal)
        self.host.add_tab(self.tab)
        self.set_mid(0.38)
        self.host._refresh_position_alerts()
        self.assertEqual(self.host._position_results[self.key].signal, SIGNAL_BUY)

    def test_inactive_both_view_tracks_call_and_put_positions_independently(self):
        call = _snapshot(right=RIGHT_CALL, con_id=11, ticker="QQQ")
        put = _snapshot(0.58, right=RIGHT_PUT, con_id=21, ticker="QQQ")
        both = _tab(call, put, tab_id=2, both=True)
        self.host.add_tab(both)
        for snapshot, side in ((call, POSITION_SOLD), (put, POSITION_BOUGHT)):
            key = position_key_for_selection(snapshot.selection)
            self.assertTrue(self.host._store_position(PositionRecord(key, side, 0.48)))
            self.assertEqual(self.host._position_locations[key][1], (snapshot.selection.pair.right, 100.0, 1.0))
        self.assertEqual(self.host.active_tab_id, 1)
        self.assertEqual([self.host._position_results[position_key_for_selection(snapshot.selection)].signal
                          for snapshot in (call, put)], [SIGNAL_BUY, SIGNAL_SELL])
        put_key = position_key_for_selection(put.selection)
        self.host.row_key = put_key
        self.host._show_position_in_matrix()
        self.assertEqual(self.host.active_tab_id, 2)
        self.assertEqual(both.selected_cell_key, (RIGHT_PUT, 100.0, 1.0))

    def test_position_helpers_require_a_saved_armed_position(self):
        self.assertIsNone(position_record_for_selection(self.host, self.snapshot.selection))
        self.assertIsNone(position_exit_for_selection(self.host, self.snapshot.selection))
        self.store(armed=False)
        self.assertIsNone(position_exit_for_selection(self.host, self.snapshot.selection))
        self.assertEqual(position_record_for_selection(self.host, self.snapshot.selection), self.host.positions[self.key])
        self.host._toggle_position_row()
        self.assertEqual(position_exit_for_selection(self.host, self.snapshot.selection).signal, SIGNAL_BUY)

    def test_ready_exit_text_uses_position_target_and_stays_distinct_from_entry_signal(self):
        self.store()
        text, color = OptionsMatrixApp._cell_signal_text(self.host, self.snapshot, muted=True)
        self.assertEqual((text, color), ("EXIT BUY 0.38", BUY_COLOR))
        self.assertEqual(compact_matrix_signal_text(text), "XB.38")
        self.set_mid(0.58)
        self.assertTrue(self.host._store_position(PositionRecord(self.key, POSITION_BOUGHT, 0.48)))
        text, color = OptionsMatrixApp._cell_signal_text(self.host, self.snapshot, muted=True)
        self.assertEqual((text, color), ("EXIT SELL 0.58", SELL_COLOR))
        self.assertEqual(compact_matrix_signal_text(text), "XS.58")

    def test_average_entry_errors_are_plain_words_not_python_messages(self):
        self.assertEqual(average_entry_from_text(" 0.485 "), 0.485)
        for text in ("abc", "", "nan", "inf", "0.38.1"):
            with self.assertRaises(ValueError) as caught:
                average_entry_from_text(text)
            self.assertEqual(str(caught.exception), "Enter your average entry as a number, such as 0.38.")
        for text in ("0", "-0.25", "0.00001"):
            with self.assertRaises(ValueError) as caught:
                average_entry_from_text(text)
            self.assertEqual(str(caught.exception), "Enter an average entry above zero.")

    def test_exit_target_label_preserves_subcent_average_precision(self):
        self.set_mid(0.385)
        self.store(average=0.485)
        self.assertAlmostEqual(self.host._position_results[self.key].target_price, 0.385)
        text, color = OptionsMatrixApp._cell_signal_text(self.host, self.snapshot)
        self.assertEqual((text, color), ("EXIT BUY 0.385", BUY_COLOR))
        self.assertEqual(compact_matrix_signal_text(text), "XB.385")

    def test_inactive_dirty_tab_refresh_redraws_active_exit_when_its_quotes_expire(self):
        self.store()
        self.assertEqual(self.host._position_results[self.key].signal, SIGNAL_BUY)
        inactive = _tab(_snapshot(con_id=11, ticker="QQQ"), tab_id=2)
        self.host.add_tab(inactive)
        self.host._dirty_tab_ids = {2}
        self.host._quote_redraw_after_id = "pending"
        self.host.now = NOW + timedelta(seconds=POSITION_QUOTE_MAX_AGE_SECONDS + 1)
        rebuilt = []
        rendered_signals = []
        details = []
        ranks = []
        self.host._rebuild_tab_from_quotes = lambda tab: rebuilt.append(tab.tab_id)
        self.host._refresh_active_matrix_render = lambda: rendered_signals.append(self.host._position_results[self.key].signal)
        self.host._redraw_selected_detail = lambda: details.append(True)
        self.host._redraw_rank_table_if_changed = lambda: ranks.append(True)

        OptionsMatrixApp._refresh_matrix_quotes(self.host)

        self.assertEqual(rebuilt, [2])
        self.assertEqual(rendered_signals, [None])
        self.assertEqual(details, [True])
        self.assertEqual(ranks, [True])
        self.assertEqual(self.host.active_tab_id, 1)
        self.assertEqual(self.host._position_results[self.key].status, "waiting")
        self.assertEqual(self.host._dirty_tab_ids, set())
        self.assertIsNone(self.host._quote_redraw_after_id)

    def test_connection_state_change_redraws_active_matrix_if_position_state_changes(self):
        renders = []
        harness = SimpleNamespace(
            positions={}, position_summary_var=object(),
            _refresh_position_alerts=lambda: True,
            _refresh_active_matrix_render=lambda: renders.append(True),
            connection_var=SimpleNamespace(set=lambda value: None),
            connect_button=SimpleNamespace(configure=lambda **kwargs: None),
            disconnect_button=SimpleNamespace(configure=lambda **kwargs: None),
        )
        with patch("options_matrix.app.refresh_token_issued_at", return_value=None):
            OptionsMatrixApp._set_connected_state(harness, False)
        self.assertEqual(renders, [True])
        self.assertFalse(harness._service_connected)

    def test_ready_exit_highlights_itm_position_but_not_paused_or_stale_position(self):
        for action, fill in ((SIGNAL_BUY, POSITION_BUY_BG), (SIGNAL_SELL, POSITION_SELL_BG)):
            with self.subTest(action=action):
                self.assertEqual(
                    resolve_matrix_cell_fills(
                        self.snapshot.selection, selected=True, is_itm=True,
                        live=True, exit_signal=action,
                    ),
                    (ITM_SELECTED_BG, fill),
                )
                for live, exit_signal in ((False, action), (True, None)):
                    self.assertEqual(
                        resolve_matrix_cell_fills(
                            self.snapshot.selection, selected=True, is_itm=True,
                            live=live, exit_signal=exit_signal,
                        ),
                        (ITM_SELECTED_BG, ITM_SELECTED_BG),
                    )

    def test_delete_selected_row_persists_removal_and_clears_exit_result(self):
        self.store()
        self.host._clear_position_row()
        self.assertNotIn(self.key, self.host.positions)
        self.assertEqual(load_positions(self.host._positions_path), {})
        self.assertIsNone(position_exit_for_selection(self.host, self.snapshot.selection))

    def test_delete_by_key_stops_alerts_and_allows_a_fresh_position(self):
        self.set_mid(0.60)
        self.store()
        other_key = ("OTHER", *self.key[1:])
        other = PositionRecord(other_key, POSITION_SOLD, 0.48)
        self.assertTrue(self.host._store_position(other))
        # The editor must delete its own pair even if another table row is selected.
        self.host.row_key = other_key
        self.assertTrue(self.host._delete_position(self.key))
        self.assertEqual(load_positions(self.host._positions_path), {other_key: other})
        self.assertNotIn(self.key, self.host._position_results)
        self.assertNotIn(self.key, self.host._position_locations)

        self.set_mid(0.38)
        self.host._refresh_position_alerts()
        self.assertIsNone(position_exit_for_selection(self.host, self.snapshot.selection))
        self.store()
        self.assertEqual(position_exit_for_selection(self.host, self.snapshot.selection).signal, SIGNAL_BUY)

    def test_failed_save_or_clear_rolls_back_position_and_reports_error(self):
        self.set_mid(0.60)
        original = self.store()
        changed = replace(original, average_price=0.65)
        with patch("options_matrix.position_ui.save_positions", side_effect=OSError("read only")):
            self.assertFalse(self.host._store_position(changed))
            self.assertEqual(self.host.positions[self.key], original)
            self.assertFalse(self.host._delete_position(self.key))
            self.assertEqual(self.host.positions[self.key], original)
        self.assertIn("could not be saved", self.host.status)
        self.assertEqual(load_positions(self.host._positions_path)[self.key], original)
        self.assertIsNone(self.host._position_results[self.key].signal)

    def test_target_crossing_does_not_write_position_store_or_open_a_window(self):
        self.set_mid(0.60)
        original = self.store()
        before = self.host._positions_path.read_bytes()
        self.set_mid(0.38)
        with patch("options_matrix.position_ui.save_positions") as save, patch("options_matrix.position_ui.tk.Toplevel") as popup:
            self.host._refresh_position_alerts()
            save.assert_not_called()
            popup.assert_not_called()
        self.assertEqual(self.host._position_results[self.key].signal, SIGNAL_BUY)
        self.assertEqual(self.host.positions[self.key], original)
        self.assertEqual(self.host._positions_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
