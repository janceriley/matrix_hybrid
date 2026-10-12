from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from options_matrix.app import (
    MatrixInputState,
    MatrixSideState,
    MatrixTabState,
    flatten_matrix_tabs_for_recording,
)
from options_matrix.models import (
    DIRECTION_DEBIT,
    LIVE_MARKET_DATA_TYPE,
    RIGHT_CALL,
    RIGHT_PUT,
    SIGNAL_BUY,
    SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,
    SIGNAL_SELL,
    FormulaSettings,
    MonitoredSideSnapshot,
    MatrixSelectionPlan,
    OptionContractRecord,
    QuoteSnapshot,
    SyntheticSpreadQuote,
    VerticalPair,
    build_exact_width_vertical_pairs,
    build_matrix_selection_plan,
    pair_key_for_pair,
    resolve_vertical_selection,
)
from options_matrix.recorder import (
    MarketSessionGuard,
    SNAPSHOT_COLUMNS,
    RecorderStatus,
    SnapshotRowBuilder,
    normalize_recording_ticker_label,
    recording_ticker_label_from_tabs,
)


EXPECTED_COLUMNS = (
    "snapshot_id",
    "session_id",
    "timestamp_utc",
    "timestamp_local",
    "ticker",
    "expiration",
    "option_type/right",
    "lower_strike",
    "higher_strike",
    "dte",
    "underlying_price",
    "underlying_volume",
    "lower_bid",
    "lower_ask",
    "lower_mid",
    "higher_bid",
    "higher_ask",
    "higher_mid",
    "lower_bid_size",
    "lower_ask_size",
    "higher_bid_size",
    "higher_ask_size",
    "lower_volume",
    "higher_volume",
    "lower_open_interest",
    "higher_open_interest",
    "lower_implied_vol",
    "higher_implied_vol",
    "lower_model_option_price",
    "higher_model_option_price",
    "market_data_type",
    "quote_age_seconds",
    "bid_ask_timestamp_mismatch",
    "synthetic_spread_bid",
    "synthetic_spread_ask",
    "synthetic_spread_mid",
    "synthetic_spread_bid_size",
    "synthetic_spread_ask_size",
    "signal_method",
    "time_bucket",
    "minute_from_open",
    "session_period",
)

REMOVED_RECORDING_COLUMNS = (
    "spread_width",
    "quote_width_pct_of_spread_width",
    "moneyness_at_snapshot",
    "fair_value_edge_curve_x",
    "fair_value_edge_curve_mid",
    "fair_value_edge_curve_edge",
    "fair_value_edge_curve_buy_endpoint",
    "fair_value_edge_curve_sell_endpoint",
    "fair_value_edge_curve_residual",
    "fair_value_edge_curve_fit_points",
    "fair_value_edge_curve_fit_error",
    "anomaly_signal_label",
    "anomaly_signal_direction",
    "anomaly_signal_price",
    "anomaly_signal_strength_ratio",
    "fair_value_edge_curve_signal_direction",
    "fair_value_edge_curve_signal_price",
    "fair_value_edge_curve_signal_edge",
    "fair_value_edge_curve_signal_strength_ratio",
)

REMOVED_LEGACY_SIGNAL_COLUMNS = (
    "opportunity_signal_label",
    "opportunity_signal_direction",
    "opportunity_signal_strength_ratio",
    "signal_direction",
    "signal_strength_ratio",
    "entry_quote_side_available_size",
    "exit_quote_side_available_size",
)

REMOVED_IBKR_LEG_COLUMNS = (
    "lower_bid_implied_vol",
    "lower_ask_implied_vol",
    "higher_bid_implied_vol",
    "higher_ask_implied_vol",
)

REMOVED_COMBO_QUOTE_COLUMNS = (
    "combo_bid",
    "combo_ask",
    "combo_mid",
    "combo_bid_size",
    "combo_ask_size",
    "combo_last_update",
    "combo_bid_update_received_at",
    "combo_ask_update_received_at",
    "combo_quote_width",
    "combo_bid_ask_size_imbalance",
    "minimum_displayed_combo_size",
    "combo_mid_vs_synthetic_mid_difference",
    "combo_bid_vs_synthetic_bid_difference",
    "combo_ask_vs_synthetic_ask_difference",
    "normalized_combo_mid_pct_of_spread_width",
)


def _option(strike: float, right: str = RIGHT_CALL) -> OptionContractRecord:
    return OptionContractRecord(
        con_id=int(round(strike * 1000)),
        symbol="SPY",
        expiration="20300117",
        strike=strike,
        right=right,
        exchange="SMART",
        currency="USD",
        trading_class="SPY",
        multiplier="100",
        local_symbol=f"SPY {right} {strike}",
    )


def _leg_quote(strike: float, mid: float) -> QuoteSnapshot:
    con_id = int(round(strike * 1000))
    return QuoteSnapshot(
        label=str(con_id),
        kind="leg",
        contract_con_id=con_id,
        bid=round(mid - 0.05, 4),
        ask=round(mid + 0.05, 4),
        bid_size=10,
        ask_size=10,
        market_data_type=LIVE_MARKET_DATA_TYPE,
    )


class _FakeCalendar:
    def __init__(self, sessions: dict[str, tuple[datetime, datetime]]) -> None:
        self.sessions = sessions
        self.schedule_calls: list[tuple[str, str]] = []

    def schedule(self, *, start_date: str, end_date: str) -> list[dict[str, datetime]]:
        self.schedule_calls.append((start_date, end_date))
        start = datetime.fromisoformat(start_date).date()
        end = datetime.fromisoformat(end_date).date()
        rows: list[dict[str, datetime]] = []
        current = start
        while current <= end:
            session = self.sessions.get(current.isoformat())
            if session is not None:
                rows.append({"market_open": session[0], "market_close": session[1]})
            current += timedelta(days=1)
        return rows


class RecorderTestCase(unittest.TestCase):
    def test_snapshot_schema_contains_only_the_42_recorded_columns(self) -> None:
        self.assertEqual(SNAPSHOT_COLUMNS, EXPECTED_COLUMNS)
        self.assertEqual(len(SNAPSHOT_COLUMNS), 42)
        self.assertNotIn("selected_group_id", SNAPSHOT_COLUMNS)
        self.assertNotIn("center_spread_id", SNAPSHOT_COLUMNS)
        self.assertNotIn("row_position_vs_center", SNAPSHOT_COLUMNS)
        for column in REMOVED_RECORDING_COLUMNS:
            self.assertNotIn(column, SNAPSHOT_COLUMNS)
        for column in REMOVED_LEGACY_SIGNAL_COLUMNS:
            self.assertNotIn(column, SNAPSHOT_COLUMNS)
        for column in REMOVED_COMBO_QUOTE_COLUMNS:
            self.assertNotIn(column, SNAPSHOT_COLUMNS)
        for column in REMOVED_IBKR_LEG_COLUMNS:
            self.assertNotIn(column, SNAPSHOT_COLUMNS)

        builder = SnapshotRowBuilder()
        self.assertEqual(builder.signal_method, SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE)
        self.assertEqual(SNAPSHOT_COLUMNS, EXPECTED_COLUMNS)

    def test_recording_ticker_label_from_tabs_uses_single_multi_and_unknown(self) -> None:
        spy_tab = SimpleNamespace(is_stale=False, matrix_input=SimpleNamespace(ticker_symbol="spy"))
        qqq_tab = SimpleNamespace(is_stale=False, matrix_input=SimpleNamespace(ticker_symbol="QQQ"))
        blank_tab = SimpleNamespace(is_stale=False, matrix_input=SimpleNamespace(ticker_symbol=""))
        stale_tab = SimpleNamespace(is_stale=True, matrix_input=SimpleNamespace(ticker_symbol="IWM"))

        self.assertEqual(recording_ticker_label_from_tabs([spy_tab, stale_tab]), "SPY")
        self.assertEqual(recording_ticker_label_from_tabs([spy_tab, qqq_tab]), "MULTI")
        self.assertEqual(recording_ticker_label_from_tabs([blank_tab]), "UNKNOWN")
        self.assertEqual(recording_ticker_label_from_tabs([]), "UNKNOWN")
        self.assertEqual(normalize_recording_ticker_label(" bad/ticker:* "), "BAD-TICKER")

    def test_recorder_status_tracks_pending_rows_and_timestamps(self) -> None:
        started_at = datetime(2026, 6, 17, 14, 30, tzinfo=timezone.utc)
        last_write_at = datetime(2026, 6, 17, 14, 31, tzinfo=timezone.utc)

        status = RecorderStatus(
            running=True,
            session_id="session-1",
            session_dir=Path("C:/recordings/session-1"),
            current_part_path=Path("C:/recordings/session-1/part-0001.parquet.inprogress"),
            rows_enqueued=25,
            rows_written=20,
            last_snapshot_rows=5,
            started_at=started_at,
            last_write_at=last_write_at,
        )

        self.assertEqual(status.pending_rows, 5)
        self.assertEqual(status.started_at, started_at)
        self.assertEqual(status.last_write_at, last_write_at)

    def test_market_session_guard_classifies_regular_session_boundaries(self) -> None:
        guard = MarketSessionGuard(
            calendar=_FakeCalendar(
                {
                    "2026-06-17": (
                        datetime(2026, 6, 17, 13, 30, tzinfo=timezone.utc),
                        datetime(2026, 6, 17, 20, 0, tzinfo=timezone.utc),
                    ),
                    "2026-06-18": (
                        datetime(2026, 6, 18, 13, 30, tzinfo=timezone.utc),
                        datetime(2026, 6, 18, 20, 0, tzinfo=timezone.utc),
                    ),
                }
            )
        )

        pre_open = guard.status_at(datetime(2026, 6, 17, 13, 29, tzinfo=timezone.utc))
        open_status = guard.status_at(datetime(2026, 6, 17, 13, 30, tzinfo=timezone.utc))
        exact_close = guard.status_at(datetime(2026, 6, 17, 20, 0, tzinfo=timezone.utc))
        after_close = guard.status_at(datetime(2026, 6, 17, 20, 1, tzinfo=timezone.utc))

        self.assertFalse(pre_open.is_open)
        self.assertEqual(pre_open.reason, "pre_open")
        self.assertTrue(open_status.is_open)
        self.assertEqual(open_status.reason, "open")
        self.assertFalse(exact_close.is_open)
        self.assertEqual(exact_close.reason, "after_close")
        self.assertEqual(exact_close.next_open_utc, datetime(2026, 6, 18, 13, 30, tzinfo=timezone.utc))
        self.assertFalse(after_close.is_open)
        self.assertIn("closed at", after_close.message)

    def test_market_session_guard_handles_holiday_and_early_close(self) -> None:
        guard = MarketSessionGuard(
            calendar=_FakeCalendar(
                {
                    "2026-07-02": (
                        datetime(2026, 7, 2, 13, 30, tzinfo=timezone.utc),
                        datetime(2026, 7, 2, 17, 0, tzinfo=timezone.utc),
                    ),
                    "2026-07-06": (
                        datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc),
                        datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc),
                    ),
                }
            )
        )

        early_open = guard.status_at(datetime(2026, 7, 2, 16, 59, tzinfo=timezone.utc))
        early_close = guard.status_at(datetime(2026, 7, 2, 17, 0, tzinfo=timezone.utc))
        holiday = guard.status_at(datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc))

        self.assertTrue(early_open.is_open)
        self.assertEqual(early_open.market_close_utc, datetime(2026, 7, 2, 17, 0, tzinfo=timezone.utc))
        self.assertFalse(early_close.is_open)
        self.assertEqual(early_close.reason, "after_close")
        self.assertFalse(holiday.is_open)
        self.assertEqual(holiday.reason, "no_session")
        self.assertEqual(holiday.next_open_utc, datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc))

    def test_market_session_guard_caches_same_day_calendar_schedules(self) -> None:
        calendar = _FakeCalendar(
            {
                "2026-06-17": (
                    datetime(2026, 6, 17, 13, 30, tzinfo=timezone.utc),
                    datetime(2026, 6, 17, 20, 0, tzinfo=timezone.utc),
                ),
                "2026-06-18": (
                    datetime(2026, 6, 18, 13, 30, tzinfo=timezone.utc),
                    datetime(2026, 6, 18, 20, 0, tzinfo=timezone.utc),
                ),
            }
        )
        guard = MarketSessionGuard(calendar=calendar)

        for second in range(3):
            guard.status_at(datetime(2026, 6, 17, 14, 0, second, tzinfo=timezone.utc))
        self.assertEqual(calendar.schedule_calls, [("2026-06-17", "2026-06-17")])

        guard.status_at(datetime(2026, 6, 17, 20, 1, tzinfo=timezone.utc))
        guard.status_at(datetime(2026, 6, 17, 20, 2, tzinfo=timezone.utc))
        self.assertEqual(
            calendar.schedule_calls,
            [
                ("2026-06-17", "2026-06-17"),
                ("2026-06-18", "2026-07-01"),
            ],
        )

    def test_row_builder_records_only_visible_selection_and_maps_metrics(self) -> None:
        captured_at = datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc)
        contracts = [_option(float(strike)) for strike in range(100, 107)]
        plan = build_matrix_selection_plan(contracts, [102.0], [1.0])
        # Leg mids telescope (3.30, 2.80, 2.25, 1.90, 1.25, 0.55) so each
        # adjacent spread's synthetic mid matches mids_by_pair.
        mids_by_pair = {
            "100/101": 0.50,
            "101/102": 0.55,
            "102/103": 0.35,
            "103/104": 0.65,
            "104/105": 0.70,
        }

        center_pair = next(pair for pair in plan.visible.values() if pair_key_for_pair(pair.pair) == "102/103").pair
        lower_quote = QuoteSnapshot(
            label=str(center_pair.lower.con_id),
            kind="leg",
            contract_con_id=center_pair.lower.con_id,
            bid=2.20,
            ask=2.30,
            bid_size=7,
            ask_size=8,
            volume=101,
            open_interest=1001,
            implied_volatility=0.21,
            model_option_price=1.25,
            market_data_type=LIVE_MARKET_DATA_TYPE,
            quote_time=captured_at - timedelta(seconds=3),
            updated_at=captured_at - timedelta(seconds=2),
            bid_update_received_at=captured_at - timedelta(seconds=2),
            ask_update_received_at=captured_at - timedelta(seconds=2),
        )
        higher_quote = QuoteSnapshot(
            label=str(center_pair.upper.con_id),
            kind="leg",
            contract_con_id=center_pair.upper.con_id,
            bid=1.90,
            ask=1.90,
            bid_size=9,
            ask_size=5,
            volume=202,
            open_interest=2002,
            implied_volatility=0.25,
            model_option_price=0.70,
            market_data_type=LIVE_MARKET_DATA_TYPE,
            quote_time=captured_at - timedelta(seconds=6),
            updated_at=captured_at - timedelta(seconds=5),
            bid_update_received_at=captured_at - timedelta(seconds=1),
            ask_update_received_at=captured_at - timedelta(seconds=2),
        )
        leg_quotes = {
            center_pair.lower.con_id: lower_quote,
            center_pair.upper.con_id: higher_quote,
        }
        for strike, leg_mid in {100.0: 3.30, 101.0: 2.80, 104.0: 1.25, 105.0: 0.55}.items():
            leg_quotes[int(round(strike * 1000))] = _leg_quote(strike, leg_mid)
        tab = SimpleNamespace(
            is_stale=False,
            matrix_input=SimpleNamespace(ticker_symbol="SPY", raw_expiration="20300117"),
            matrix_plan=plan,
            leg_quotes=leg_quotes,
        )
        underlying_quote = QuoteSnapshot(
            label="SPY",
            kind="underlying",
            mark=102.75,
            volume=123456,
        )

        rows = SnapshotRowBuilder(
            formula_settings=FormulaSettings(fair_value_edge_curve_min_points=3),
        ).build_rows(
            session_id="session-1",
            snapshot_id="snapshot-1",
            captured_at=captured_at,
            tabs=[tab],
            underlying_quote=underlying_quote,
        )

        self.assertEqual(len(rows), len(plan.visible))
        row = next(candidate for candidate in rows if candidate["lower_strike"] == 102.0)
        self.assertEqual(row["snapshot_id"], "snapshot-1")
        self.assertEqual(row["session_id"], "session-1")
        self.assertEqual(row["ticker"], "SPY")
        self.assertEqual(row["expiration"], "20300117")
        self.assertEqual(row["option_type/right"], RIGHT_CALL)
        self.assertEqual(row["underlying_price"], 102.75)
        self.assertEqual(row["underlying_volume"], 123456)
        self.assertEqual(row["market_data_type"], LIVE_MARKET_DATA_TYPE)
        # Age uses the oldest required leg quote_time (6s), not the freshest leg.
        self.assertEqual(row["quote_age_seconds"], 6.0)
        self.assertTrue(row["bid_ask_timestamp_mismatch"])
        self.assertEqual(row["lower_volume"], 101)
        self.assertEqual(row["higher_open_interest"], 2002)
        self.assertEqual(row["lower_implied_vol"], 0.21)
        self.assertEqual(row["higher_implied_vol"], 0.25)
        self.assertEqual(row["lower_model_option_price"], 1.25)
        self.assertAlmostEqual(row["synthetic_spread_bid"], 0.30)
        self.assertAlmostEqual(row["synthetic_spread_ask"], 0.40)
        self.assertAlmostEqual(row["synthetic_spread_mid"], 0.35)
        self.assertEqual(row["synthetic_spread_bid_size"], 5)
        self.assertEqual(row["synthetic_spread_ask_size"], 8)
        self.assertEqual(row["signal_method"], SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE)
        self.assertEqual(tuple(row), EXPECTED_COLUMNS)
        for column in REMOVED_RECORDING_COLUMNS:
            self.assertNotIn(column, row)
        for column in REMOVED_LEGACY_SIGNAL_COLUMNS:
            self.assertNotIn(column, row)
        for column in REMOVED_COMBO_QUOTE_COLUMNS:
            self.assertNotIn(column, row)
        self.assertEqual(row["time_bucket"], "2026-06-17 10:45")
        self.assertEqual(row["minute_from_open"], 75)
        self.assertEqual(row["session_period"], "REGULAR")

    def test_bid_ask_timestamp_mismatch_accounts_for_both_legs(self) -> None:
        captured_at = datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc)
        contracts = [_option(100.0), _option(101.0)]
        plan = build_matrix_selection_plan(contracts, [100.0], [1.0])
        lower = QuoteSnapshot(
            label="100000",
            kind="leg",
            contract_con_id=100000,
            bid=1.0,
            ask=1.1,
            bid_update_received_at=captured_at - timedelta(seconds=4),
            ask_update_received_at=captured_at - timedelta(seconds=4),
        )
        higher = QuoteSnapshot(
            label="101000",
            kind="leg",
            contract_con_id=101000,
            bid=0.4,
            ask=0.5,
            bid_update_received_at=captured_at - timedelta(seconds=1),
            ask_update_received_at=captured_at - timedelta(seconds=1),
        )
        tab = SimpleNamespace(
            is_stale=False,
            matrix_input=SimpleNamespace(ticker_symbol="SPY", raw_expiration="20300117"),
            matrix_plan=plan,
            leg_quotes={100000: lower, 101000: higher},
        )

        row = SnapshotRowBuilder().build_rows(
            session_id="session-1",
            snapshot_id="snapshot-1",
            captured_at=captured_at,
            tabs=[tab],
            underlying_quote=QuoteSnapshot(label="SPY", kind="underlying", mark=99.0),
        )[0]

        self.assertTrue(row["bid_ask_timestamp_mismatch"])

    def test_row_builder_records_every_visible_moneyness_state_even_without_quotes(self) -> None:
        captured_at = datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc)
        contracts = [_option(float(strike)) for strike in range(100, 104)]
        plan = build_matrix_selection_plan(
            contracts,
            [100.0, 101.0, 102.0],
            [1.0],
            context_neighbors=0,
        )
        tab = SimpleNamespace(
            is_stale=False,
            matrix_input=SimpleNamespace(ticker_symbol="SPY", raw_expiration="20300117"),
            matrix_plan=plan,
            # Only the OTM 102/103 call spread is quoted. The fully ITM
            # 100/101 and crossing 101/102 selections must still be rows.
            leg_quotes={
                int(round(102.0 * 1000)): _leg_quote(102.0, 1.0),
                int(round(103.0 * 1000)): _leg_quote(103.0, 0.5),
            },
        )

        rows = SnapshotRowBuilder().build_rows(
            session_id="session-1",
            snapshot_id="snapshot-1",
            captured_at=captured_at,
            tabs=[tab],
            underlying_quote=QuoteSnapshot(label="SPY", kind="underlying", mark=101.5),
        )

        self.assertEqual(len(rows), 3)
        rows_by_lower = {row["lower_strike"]: row for row in rows}
        self.assertEqual(set(rows_by_lower), {100.0, 101.0, 102.0})
        self.assertIsNone(rows_by_lower[100.0]["synthetic_spread_mid"])
        self.assertIsNone(rows_by_lower[101.0]["synthetic_spread_mid"])
        self.assertAlmostEqual(rows_by_lower[102.0]["synthetic_spread_mid"], 0.5)

    def test_row_builder_excludes_manual_half_widths_but_keeps_supported_missing_quotes(self) -> None:
        captured_at = datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc)
        for right, excluded_width in (
            (right, width)
            for right in (RIGHT_CALL, RIGHT_PUT)
            for width in (0.5, 1.5, 2.5, 3.5, 4.5)
        ):
            with self.subTest(right=right, excluded_width=excluded_width):
                lower, half, upper = [
                    _option(strike, right) for strike in (100.0, 100.0 + excluded_width, 101.0)
                ]
                half_selection = resolve_vertical_selection(
                    VerticalPair(right=right, lower=lower, upper=half), DIRECTION_DEBIT
                )
                supported_selection = resolve_vertical_selection(
                    VerticalPair(right=right, lower=lower, upper=upper), DIRECTION_DEBIT
                )
                tab = SimpleNamespace(
                    is_stale=False,
                    matrix_input=SimpleNamespace(ticker_symbol="SPY", raw_expiration="20300117"),
                    matrix_plan=MatrixSelectionPlan(
                        visible={(100.0, excluded_width): half_selection, (100.0, 1.0): supported_selection},
                        context_by_width={},
                    ),
                    # The excluded pair has quotes, but the supported pair is missing a leg.
                    leg_quotes={
                        lower.con_id: _leg_quote(lower.strike, 1.0),
                        half.con_id: _leg_quote(half.strike, 0.8),
                    },
                )

                rows = SnapshotRowBuilder().build_rows(
                    session_id="session-1",
                    snapshot_id="snapshot-1",
                    captured_at=captured_at,
                    tabs=[tab],
                    underlying_quote=None,
                )

                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["option_type/right"], right)
                self.assertEqual((rows[0]["lower_strike"], rows[0]["higher_strike"]), (100.0, 101.0))
                self.assertIsNone(rows[0]["synthetic_spread_mid"])

    def test_row_builder_excludes_half_dollar_strikes_from_manual_plans(self) -> None:
        for right in (RIGHT_CALL, RIGHT_PUT):
            with self.subTest(right=right):
                strike_pairs = [
                    (157.5, 157.5 + width) for width in range(1, 6)
                ] + [(157.5, 158.75), (157.25, 158.5)]
                selections = [
                    resolve_vertical_selection(
                        VerticalPair(right=right, lower=_option(lower, right), upper=_option(upper, right)),
                        DIRECTION_DEBIT,
                    )
                    for lower, upper in strike_pairs
                ]
                tab = SimpleNamespace(
                    is_stale=False,
                    matrix_input=SimpleNamespace(ticker_symbol="SPY", raw_expiration="20300117"),
                    matrix_plan=MatrixSelectionPlan(
                        visible={
                            (selection.pair.lower.strike, selection.pair.width): selection
                            for selection in selections
                        },
                        context_by_width={},
                    ),
                    leg_quotes={
                        contract.con_id: _leg_quote(contract.strike, 1.0)
                        for selection in selections
                        for contract in (selection.pair.lower, selection.pair.upper)
                    },
                )

                rows = SnapshotRowBuilder().build_rows(
                    session_id="session-1",
                    snapshot_id="snapshot-1",
                    captured_at=datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc),
                    tabs=[tab],
                    underlying_quote=None,
                )

                self.assertEqual(rows, [])

    def test_row_builder_keeps_supported_strikes_in_mixed_manual_plans_without_quotes(self) -> None:
        for right in (RIGHT_CALL, RIGHT_PUT):
            with self.subTest(right=right):
                supported_strikes = [(157.0, 162.0), (157.25, 158.25), (157.75, 158.0)]
                selections = [
                    resolve_vertical_selection(
                        VerticalPair(right=right, lower=_option(lower, right), upper=_option(upper, right)),
                        DIRECTION_DEBIT,
                    )
                    for lower, upper in [
                        (157.5, 162.5), (157.5, 158.75), (157.25, 158.5), *supported_strikes
                    ]
                ]
                tab = SimpleNamespace(
                    is_stale=False,
                    matrix_input=SimpleNamespace(ticker_symbol="SPY", raw_expiration="20300117"),
                    matrix_plan=MatrixSelectionPlan(
                        visible={
                            (selection.pair.lower.strike, selection.pair.width): selection
                            for selection in selections
                        },
                        context_by_width={},
                    ),
                    leg_quotes={},
                )

                rows = SnapshotRowBuilder().build_rows(
                    session_id="session-1",
                    snapshot_id="snapshot-1",
                    captured_at=datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc),
                    tabs=[tab],
                    underlying_quote=None,
                )

                self.assertEqual(
                    [(row["lower_strike"], row["higher_strike"]) for row in rows],
                    supported_strikes,
                )
                for row in rows:
                    self.assertEqual(tuple(row), EXPECTED_COLUMNS)
                    self.assertEqual(row["option_type/right"], right)
                    self.assertIsNone(row["synthetic_spread_mid"])

    def test_row_builder_excludes_nonvisible_context(self) -> None:
        captured_at = datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc)
        call_contracts = [_option(float(strike)) for strike in range(100, 106)]
        plan = build_matrix_selection_plan(call_contracts, [102.0], [1.0])
        tab = SimpleNamespace(
            is_stale=False,
            matrix_input=SimpleNamespace(ticker_symbol="SPY", raw_expiration="20300117"),
            matrix_plan=plan,
            leg_quotes={},
        )

        rows = SnapshotRowBuilder().build_rows(
            session_id="session-1",
            snapshot_id="snapshot-1",
            captured_at=captured_at,
            tabs=[tab],
            underlying_quote=QuoteSnapshot(label="SPY", kind="underlying", mark=102.5),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["option_type/right"], RIGHT_CALL)
        self.assertEqual(rows[0]["lower_strike"], 102.0)
        self.assertEqual(rows[0]["higher_strike"], 103.0)

    def test_row_builder_deduplicates_overlapping_visible_tabs(self) -> None:
        captured_at = datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc)
        contracts = [_option(100.0), _option(101.0)]
        plan = build_matrix_selection_plan(contracts, [100.0], [1.0])
        tab = SimpleNamespace(
            is_stale=False,
            matrix_input=SimpleNamespace(ticker_symbol="SPY", raw_expiration="20300117"),
            matrix_plan=plan,
            leg_quotes={},
        )

        rows = SnapshotRowBuilder().build_rows(
            session_id="session-1",
            snapshot_id="snapshot-1",
            captured_at=captured_at,
            tabs=[tab, tab],
            underlying_quote=None,
        )

        self.assertEqual(len(rows), 1)

    def test_flattened_both_tab_matches_separate_call_and_put_recording_views(self) -> None:
        captured_at = datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc)
        expiration = "20300117"
        call_contracts = [_option(float(strike), RIGHT_CALL) for strike in range(99, 106)]
        put_contracts = [_option(float(strike), RIGHT_PUT) for strike in range(99, 106)]
        call_base = build_matrix_selection_plan(call_contracts, [101.0, 102.0], [1.0])
        put_base = build_matrix_selection_plan(put_contracts, [101.0, 102.0], [1.0])

        call_plan = call_base
        put_plan = put_base

        def matrix_input(side: str) -> MatrixInputState:
            return MatrixInputState(
                display_expiration="2030-01-17",
                raw_expiration=expiration,
                side=side,
                low_strike=101.0,
                high_strike=103.0,
                widths=(1.0,),
                ticker_symbol="SPY",
            )

        put_rows = (101.0, 102.0, 103.0)
        call_rows = (101.0, 102.0, 103.0)
        shared_quotes: dict[int, QuoteSnapshot] = {}
        both_tab = MatrixTabState(
            tab_id=1,
            label="2030-01-17 BOTH",
            matrix_input=matrix_input("BOTH"),
            row_strikes=(101.0, 102.0, 103.0),
            matrix_plan=MatrixSelectionPlan(visible={}, context_by_width={}),
            leg_quotes=shared_quotes,
            side_states={
                RIGHT_PUT: MatrixSideState(
                    right=RIGHT_PUT,
                    row_strikes=put_rows,
                    matrix_plan=put_plan,
                ),
                RIGHT_CALL: MatrixSideState(
                    right=RIGHT_CALL,
                    row_strikes=call_rows,
                    matrix_plan=call_plan,
                ),
            },
        )
        put_tab = MatrixTabState(
            tab_id=2,
            label="2030-01-17 P",
            matrix_input=matrix_input(RIGHT_PUT),
            row_strikes=put_rows,
            matrix_plan=put_plan,
            leg_quotes=shared_quotes,
        )
        call_tab = MatrixTabState(
            tab_id=3,
            label="2030-01-17 C",
            matrix_input=matrix_input(RIGHT_CALL),
            row_strikes=call_rows,
            matrix_plan=call_plan,
            leg_quotes=shared_quotes,
        )

        both_views = flatten_matrix_tabs_for_recording([both_tab])
        separate_views = flatten_matrix_tabs_for_recording([put_tab, call_tab])
        self.assertEqual(
            [view.matrix_input.side for view in both_views],
            [RIGHT_PUT, RIGHT_CALL],
        )

        builder = SnapshotRowBuilder()
        both_rows = builder.build_rows(
            session_id="session-1",
            snapshot_id="snapshot-1",
            captured_at=captured_at,
            tabs=both_views,
            underlying_quote=QuoteSnapshot(label="SPY", kind="underlying", mark=101.5),
        )
        separate_rows = builder.build_rows(
            session_id="session-1",
            snapshot_id="snapshot-1",
            captured_at=captured_at,
            tabs=separate_views,
            underlying_quote=QuoteSnapshot(label="SPY", kind="underlying", mark=101.5),
        )

        self.assertEqual(both_rows, separate_rows)
        self.assertEqual(len(both_rows), 4)
        self.assertTrue(all(tuple(row) == SNAPSHOT_COLUMNS for row in both_rows))
        self.assertEqual(
            {row["option_type/right"] for row in both_rows},
            {RIGHT_CALL, RIGHT_PUT},
        )
        self.assertNotIn("BOTH", {row["option_type/right"] for row in both_rows})
        self.assertEqual({row["snapshot_id"] for row in both_rows}, {"snapshot-1"})
        self.assertEqual({row["timestamp_utc"] for row in both_rows}, {captured_at})
        self.assertEqual(len({row["timestamp_local"] for row in both_rows}), 1)

        expected_visible_rows = {
            (
                selection.pair.right,
                float(selection.pair.lower.strike),
                float(selection.pair.upper.strike),
            )
            for plan in (put_plan, call_plan)
            for selection in plan.visible.values()
        }
        actual_rows = {
            (
                row["option_type/right"],
                row["lower_strike"],
                row["higher_strike"],
            )
            for row in both_rows
        }
        self.assertEqual(actual_rows, expected_visible_rows)
        self.assertEqual(len(actual_rows), len(both_rows))

    def test_flattened_recording_views_retain_underlying_price_hint(self) -> None:
        expiration = "20300117"
        call_plan = build_matrix_selection_plan(
            [_option(100.0, RIGHT_CALL), _option(101.0, RIGHT_CALL)],
            [100.0],
            [1.0],
        )
        put_plan = build_matrix_selection_plan(
            [_option(100.0, RIGHT_PUT), _option(101.0, RIGHT_PUT)],
            [100.0],
            [1.0],
        )
        both_input = MatrixInputState(
            display_expiration="2030-01-17",
            raw_expiration=expiration,
            side="BOTH",
            low_strike=100.0,
            high_strike=101.0,
            widths=(1.0,),
            ticker_symbol="SPY",
        )
        both_tab = MatrixTabState(
            tab_id=1,
            label="2030-01-17 BOTH",
            matrix_input=both_input,
            row_strikes=(100.0, 101.0),
            matrix_plan=MatrixSelectionPlan(visible={}, context_by_width={}),
            underlying_price_hint=100.75,
            side_states={
                RIGHT_PUT: MatrixSideState(
                    right=RIGHT_PUT,
                    row_strikes=(100.0, 101.0),
                    matrix_plan=put_plan,
                ),
                RIGHT_CALL: MatrixSideState(
                    right=RIGHT_CALL,
                    row_strikes=(100.0, 101.0),
                    matrix_plan=call_plan,
                ),
            },
        )

        views = flatten_matrix_tabs_for_recording([both_tab])

        self.assertEqual([view.matrix_input.side for view in views], [RIGHT_PUT, RIGHT_CALL])
        self.assertEqual([view.underlying_price_hint for view in views], [100.75, 100.75])

    def test_row_builder_prefers_positive_live_underlying_over_view_hint(self) -> None:
        captured_at = datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc)
        plan = build_matrix_selection_plan(
            [_option(100.0), _option(101.0)],
            [100.0],
            [1.0],
        )
        tab = SimpleNamespace(
            is_stale=False,
            matrix_input=SimpleNamespace(ticker_symbol="SPY", raw_expiration="20300117"),
            matrix_plan=plan,
            leg_quotes={},
            underlying_price_hint=100.25,
        )
        computation_prices: list[float | None] = []

        def capture_fair_value_price(*_args, **kwargs):
            computation_prices.append(kwargs["underlying_price"])
            return {}

        with patch(
            "options_matrix.recorder.build_matrix_snapshots",
            side_effect=capture_fair_value_price,
        ):
            rows = SnapshotRowBuilder().build_rows(
                session_id="session-1",
                snapshot_id="snapshot-1",
                captured_at=captured_at,
                tabs=[tab],
                underlying_quote=QuoteSnapshot(
                    label="SPY",
                    kind="underlying",
                    mark=101.75,
                ),
            )

        self.assertEqual(computation_prices, [101.75])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["underlying_price"], 101.75)

    def test_row_builder_uses_each_view_hint_when_live_underlying_is_unusable(self) -> None:
        captured_at = datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc)
        call_plan = build_matrix_selection_plan(
            [_option(100.0, RIGHT_CALL), _option(101.0, RIGHT_CALL)],
            [100.0],
            [1.0],
        )
        put_plan = build_matrix_selection_plan(
            [_option(100.0, RIGHT_PUT), _option(101.0, RIGHT_PUT)],
            [100.0],
            [1.0],
        )
        tabs = [
            SimpleNamespace(
                is_stale=False,
                matrix_input=SimpleNamespace(
                    ticker_symbol="SPY",
                    raw_expiration="20300117",
                ),
                matrix_plan=call_plan,
                leg_quotes={},
                underlying_price_hint=100.25,
            ),
            SimpleNamespace(
                is_stale=False,
                matrix_input=SimpleNamespace(
                    ticker_symbol="SPY",
                    raw_expiration="20300124",
                ),
                matrix_plan=put_plan,
                leg_quotes={},
                underlying_price_hint=100.75,
            ),
        ]
        computation_prices: list[float | None] = []

        def capture_fair_value_price(*_args, **kwargs):
            computation_prices.append(kwargs["underlying_price"])
            return {}

        with patch(
            "options_matrix.recorder.build_matrix_snapshots",
            side_effect=capture_fair_value_price,
        ):
            rows = SnapshotRowBuilder().build_rows(
                session_id="session-1",
                snapshot_id="snapshot-1",
                captured_at=captured_at,
                tabs=tabs,
                underlying_quote=QuoteSnapshot(
                    label="SPY",
                    kind="underlying",
                    mark=0.0,
                    bid=0.0,
                    ask=0.0,
                    last=0.0,
                ),
            )

        self.assertEqual(computation_prices, [100.25, 100.75])
        self.assertEqual(
            [row["underlying_price"] for row in rows],
            [100.25, 100.75],
        )

    def test_row_builder_skips_zero_mark_and_mid_for_positive_live_last(self) -> None:
        captured_at = datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc)
        plan = build_matrix_selection_plan(
            [_option(100.0), _option(101.0)],
            [100.0],
            [1.0],
        )
        tab = SimpleNamespace(
            is_stale=False,
            matrix_input=SimpleNamespace(ticker_symbol="SPY", raw_expiration="20300117"),
            matrix_plan=plan,
            leg_quotes={},
            underlying_price_hint=100.25,
        )
        computation_prices: list[float | None] = []

        def capture_fair_value_price(*_args, **kwargs):
            computation_prices.append(kwargs["underlying_price"])
            return {}

        with patch(
            "options_matrix.recorder.build_matrix_snapshots",
            side_effect=capture_fair_value_price,
        ):
            rows = SnapshotRowBuilder().build_rows(
                session_id="session-1",
                snapshot_id="snapshot-1",
                captured_at=captured_at,
                tabs=[tab],
                underlying_quote=QuoteSnapshot(
                    label="SPY",
                    kind="underlying",
                    mark=0.0,
                    bid=0.0,
                    ask=0.0,
                    last=101.5,
                ),
            )

        self.assertEqual(computation_prices, [101.5])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["underlying_price"], 101.5)

    def test_row_builder_records_strikes_for_maximum_width_without_width_column(self) -> None:
        captured_at = datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc)
        contracts = [_option(101.0), _option(106.0)]
        plan = build_matrix_selection_plan(contracts, [101.0], [5.0])
        tab = SimpleNamespace(
            is_stale=False,
            matrix_input=SimpleNamespace(ticker_symbol="SPY", raw_expiration="20300117"),
            matrix_plan=plan,
            leg_quotes={
                int(round(101.0 * 1000)): _leg_quote(101.0, 4.0),
                int(round(106.0 * 1000)): _leg_quote(106.0, 1.0),
            },
        )

        rows = SnapshotRowBuilder().build_rows(
            session_id="session-1",
            snapshot_id="snapshot-1",
            captured_at=captured_at,
            tabs=[tab],
            underlying_quote=QuoteSnapshot(label="SPY", kind="underlying", mark=100.0),
        )

        self.assertEqual(SNAPSHOT_COLUMNS, EXPECTED_COLUMNS)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lower_strike"], 101.0)
        self.assertEqual(rows[0]["higher_strike"], 106.0)
        self.assertNotIn("spread_width", rows[0])

    def test_row_builder_excludes_fair_value_trace_but_keeps_signal_method(self) -> None:
        captured_at = datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc)
        contracts = [_option(float(strike)) for strike in range(100, 106)]
        plan = build_matrix_selection_plan(contracts, [102.0], [1.0])
        # Leg mids telescope (3.00, 2.30, 1.60, 1.50, 0.80, 0.10) so each
        # adjacent spread has a deterministic synthetic mid.
        leg_mids = {100.0: 3.00, 101.0: 2.30, 102.0: 1.60, 103.0: 1.50, 104.0: 0.80, 105.0: 0.10}
        leg_quotes = {
            int(round(strike * 1000)): _leg_quote(strike, leg_mid)
            for strike, leg_mid in leg_mids.items()
        }
        tab = SimpleNamespace(
            is_stale=False,
            matrix_input=SimpleNamespace(ticker_symbol="SPY", raw_expiration="20300117"),
            matrix_plan=plan,
            leg_quotes=leg_quotes,
        )
        underlying_quote = QuoteSnapshot(
            label="SPY",
            kind="underlying",
            mark=102.5,
            volume=123456,
        )

        rows = SnapshotRowBuilder(
            formula_settings=FormulaSettings(fair_value_edge_curve_min_points=3),
            signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,
        ).build_rows(
            session_id="session-1",
            snapshot_id="snapshot-1",
            captured_at=captured_at,
            tabs=[tab],
            underlying_quote=underlying_quote,
        )

        row = next(candidate for candidate in rows if candidate["lower_strike"] == 102.0)
        self.assertEqual(row["signal_method"], SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE)
        self.assertAlmostEqual(row["synthetic_spread_mid"], 0.10)
        for column in REMOVED_RECORDING_COLUMNS:
            self.assertNotIn(column, row)
        for column in REMOVED_LEGACY_SIGNAL_COLUMNS:
            self.assertNotIn(column, row)

    def test_row_builder_excludes_populated_anomaly_and_opportunity_fields(self) -> None:
        captured_at = datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc)
        pair = build_exact_width_vertical_pairs([_option(100.0), _option(101.0)], 1.0)[0]
        selection = resolve_vertical_selection(pair, DIRECTION_DEBIT)
        snapshot = MonitoredSideSnapshot(
            title=pair.label,
            pair_key=pair_key_for_pair(pair),
            selection=selection,
            synthetic_quote=SyntheticSpreadQuote(bid=0.45, ask=0.55, bid_size=10, ask_size=10),
            fair_value_edge_curve_mid=0.50,
            fair_value_edge_curve_buy_endpoint=0.40,
            fair_value_edge_curve_sell_endpoint=0.60,
            anomaly_direction=SIGNAL_BUY,
            anomaly_price=0.60,
            opportunity_direction=SIGNAL_SELL,
            opportunity_price=0.40,
        )

        row = SnapshotRowBuilder()._build_row(
            session_id="session-1",
            snapshot_id="snapshot-1",
            captured_utc=captured_at,
            market_dt=captured_at,
            matrix_input=SimpleNamespace(ticker_symbol="SPY", raw_expiration="20300117"),
            snapshot=snapshot,
            leg_quotes={},
            underlying_price=100.0,
            underlying_volume=1000,
        )

        self.assertEqual(row["signal_method"], SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE)
        for column in REMOVED_RECORDING_COLUMNS:
            self.assertNotIn(column, row)
        for column in REMOVED_LEGACY_SIGNAL_COLUMNS:
            self.assertNotIn(column, row)

if __name__ == "__main__":
    unittest.main()
