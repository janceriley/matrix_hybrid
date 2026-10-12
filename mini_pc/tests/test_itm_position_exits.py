from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from options_matrix.app import (
    BUY_COLOR,
    ITM_BG,
    ITM_SELECTED_BG,
    POSITION_BUY_BG,
    POSITION_SELL_BG,
    SELL_COLOR,
    MatrixTabState,
    OptionsMatrixApp,
    build_matrix_input_plan,
    compact_matrix_signal_text,
    matrix_quote_target_selections,
    parse_matrix_input_state,
    resolve_matrix_cell_fills,
)
from options_matrix.models import (
    DEFAULT_FORMULA_SETTINGS,
    DEFAULT_SIGNAL_METHOD,
    LIVE_MARKET_DATA_TYPE,
    RIGHT_CALL,
    RIGHT_PUT,
    SIGNAL_BUY,
    SIGNAL_SELL,
    OptionContractRecord,
    QuoteSnapshot,
    build_matrix_snapshots,
    compute_matrix_signal,
)
from options_matrix.position_ui import (
    POSITION_QUOTE_MAX_AGE_SECONDS,
    PositionAlertsMixin,
    position_exit_for_selection,
)
from options_matrix.positions import POSITION_BOUGHT, POSITION_SOLD, PositionRecord, position_key_for_selection


NOW = datetime(2030, 1, 10, 15, 0, tzinfo=timezone.utc)


class _PositionHost(PositionAlertsMixin):
    def __init__(self, directory):
        self._initialize_positions(directory)
        self.formula_settings = DEFAULT_FORMULA_SETTINGS
        self.signal_method = DEFAULT_SIGNAL_METHOD
        self.service = SimpleNamespace(connected=True)
        self._quote_event_generation = 1
        self._subscription_diagnostics_by_con_id = {}
        self.matrix_tabs = []
        self.underlying_quote = None

    def _active_tab(self):
        return self.matrix_tabs[0]

    def _refresh_position_alerts(self, *, now=NOW):
        return super()._refresh_position_alerts(now=now)

    def _refresh_position_table(self):
        pass

    def _refresh_position_summary(self):
        pass

    def _redraw_all(self):
        pass


class ItmPositionExitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def make_position(self, right, entry_side):
        host = _PositionHost(self.directory / f"{right}-{entry_side}")
        lower = OptionContractRecord(
            con_id=1, symbol="SPY", expiration="20300117", strike=100.0, right=right,
            exchange="SMART", currency="USD", trading_class="SPY", multiplier="100",
            local_symbol=f"SPY {right} 100",
        )
        upper = replace(lower, con_id=2, strike=101.0, local_symbol=f"SPY {right} 101")
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17", raw_expiration="20300117", side=right,
            low_strike="100", high_strike="101", width_states={1.0: True}, ticker_symbol="SPY",
        )
        spot = 102.0 if right == RIGHT_CALL else 99.0
        input_plan = build_matrix_input_plan((lower, upper), matrix_input, underlying_price=spot)
        self.assertIsNotNone(input_plan)
        selection = input_plan.plan.visible[(100.0, 1.0)]
        self.assertIn(selection, matrix_quote_target_selections(input_plan.plan))
        tab = MatrixTabState(
            tab_id=1, label="ITM position", matrix_input=matrix_input,
            row_strikes=input_plan.row_strikes, matrix_plan=input_plan.plan,
            underlying_price_hint=spot,
        )
        mid = 0.38 if entry_side == POSITION_SOLD else 0.58
        for leg in selection.legs:
            leg_mid = 1.0 + mid if leg.action == "BUY" else 1.0
            tab.leg_quotes[leg.contract.con_id] = QuoteSnapshot(
                label=leg.contract.local_symbol, contract_con_id=leg.contract.con_id,
                bid=round(leg_mid - 0.02, 4), ask=round(leg_mid + 0.02, 4),
                market_data_type=LIVE_MARKET_DATA_TYPE,
                updated_at=NOW, bid_confirmed_at=NOW, ask_confirmed_at=NOW,
            )
            host._subscription_diagnostics_by_con_id[leg.contract.con_id] = SimpleNamespace(
                state="subscribed", generation=1,
            )
        tab.matrix_snapshots = build_matrix_snapshots(input_plan.plan, tab.leg_quotes, underlying_price=spot)
        host.matrix_tabs.append(tab)
        snapshot = tab.matrix_snapshots[(100.0, 1.0)]
        # Use the same real moneyness classification as all three matrix layouts.
        self.assertTrue(OptionsMatrixApp._selection_is_itm(host, selection))
        self.assertIsNone(compute_matrix_signal(snapshot))
        position = PositionRecord(position_key_for_selection(selection), entry_side, 0.48)
        self.assertTrue(host._store_position(position))
        return host, tab, snapshot, position

    def rendered_state(self, host, snapshot, *, selected=False):
        exit_result = position_exit_for_selection(host, snapshot.selection)
        is_itm = OptionsMatrixApp._selection_is_itm(host, snapshot.selection)
        fills = resolve_matrix_cell_fills(
            snapshot.selection, selected=selected, is_itm=is_itm,
            live=not host._active_tab().is_stale, exit_signal=getattr(exit_result, "signal", None),
        )
        text, color = OptionsMatrixApp._cell_signal_text(host, snapshot, muted=is_itm)
        return fills, text, color

    def test_live_itm_call_and_put_positions_show_both_exit_directions_without_fair_value_signal(self):
        for right in (RIGHT_CALL, RIGHT_PUT):
            for side, action, fill, color, target, compact in (
                (POSITION_SOLD, SIGNAL_BUY, POSITION_BUY_BG, BUY_COLOR, "0.38", "XB.38"),
                (POSITION_BOUGHT, SIGNAL_SELL, POSITION_SELL_BG, SELL_COLOR, "0.58", "XS.58"),
            ):
                with self.subTest(right=right, side=side):
                    host, tab, snapshot, position = self.make_position(right, side)
                    self.assertEqual(host._position_results[position.key].signal, action)
                    for selected in (False, True):
                        fills, text, text_color = self.rendered_state(host, snapshot, selected=selected)
                        self.assertEqual(fills, (ITM_SELECTED_BG if selected else ITM_BG, fill))
                        self.assertEqual((text, text_color), (f"EXIT {action} {target}", color))
                        self.assertEqual(compact_matrix_signal_text(text), compact)

    def test_itm_exit_stops_when_paused_or_quotes_expire_and_returns_when_live(self):
        for right in (RIGHT_CALL, RIGHT_PUT):
            for side in (POSITION_SOLD, POSITION_BOUGHT):
                with self.subTest(right=right, side=side):
                    host, tab, snapshot, position = self.make_position(right, side)
                    for state in ("paused", "expired_quotes", "stale_tab"):
                        with self.subTest(state=state):
                            if state == "paused":
                                self.assertTrue(host._store_position(replace(position, armed=False)))
                            elif state == "expired_quotes":
                                host._refresh_position_alerts(now=NOW + timedelta(seconds=POSITION_QUOTE_MAX_AGE_SECONDS + 1))
                            else:
                                tab.is_stale = True
                                host._refresh_position_alerts()
                            fills, text, _color = self.rendered_state(host, snapshot)
                            self.assertEqual(fills, (ITM_BG, ITM_BG))
                            self.assertFalse(text.startswith("EXIT "))
                            tab.is_stale = False
                            self.assertTrue(host._store_position(position))
                            self.assertTrue(self.rendered_state(host, snapshot)[1].startswith("EXIT "))


if __name__ == "__main__":
    unittest.main()
