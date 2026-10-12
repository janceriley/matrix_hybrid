from __future__ import annotations

import json
import queue
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from options_matrix.app import (
    DEFAULT_MATRIX_MODE,
    DEFAULT_WIDTHS,
    EDITABLE_FORMULA_SETTING_CONTROLS,
    FAIR_VALUE_COLOR,
    FAIR_VALUE_SYSTEM_OVERVIEW,
    FORMULA_SETTING_CONTROLS,
    GRID_BG,
    ITM_BG,
    ITM_SELECTED_BG,
    MAX_MATRIX_STRIKE_WIDTH,
    MAX_MATRIX_TABS,
    MATRIX_MODE_MODIFIED,
    MATRIX_MODE_REGULAR,
    MATRIX_WIDTH_BANDS,
    LIQUIDITY_GAUGE_MIN_PEERS,
    MISSING_BG,
    MATRIX_VIEW_BOTH,
    MATRIX_VIEWS,
    OptionsMatrixApp,
    RANK_SORT_ASC,
    RANK_SORT_DESC,
    RANK_SORT_NONE,
    SUBSCRIPTION_CONFIRMED_COLOR,
    SUBSCRIPTION_CONTEXT_COLOR,
    SUBSCRIPTION_FAILED_COLOR,
    SUBSCRIPTION_PENDING_COLOR,
    SUBSCRIPTION_RECONCILING_COLOR,
    SELECTED_BG,
    POSITION_BUY_BG,
    POSITION_SELL_BG,
    POSITION_ICON_TEXT,
    CachedMatrixTab,
    CachedMatrixWorkspace,
    MatrixLiquidityGaugeState,
    MatrixTabState,
    MatrixSideState,
    RankSortState,
    build_aggregate_ranked_signals,
    build_matrix_input_plan,
    build_matrix_view_plan,
    cached_matrix_workspace_from_tabs,
    clear_matrix_workspace_cache,
    compact_matrix_signal_text,
    compact_auth_status_text,
    compact_connection_status_text,
    connection_status_text,
    detail_display_values,
    discovered_matrix_view_widths,
    discovered_matrix_widths,
    fair_value_edge_curve_values,
    fair_value_unavailable_message,
    fair_value_settings_example_data,
    formula_settings_config_path,
    formula_settings_from_mapping,
    formula_settings_to_dict,
    format_compact_market_data_quote_status,
    format_market_data_quote_status,
    has_duplicate_matrix_tab,
    is_routine_reconnect_schedule_message,
    load_formula_settings,
    load_matrix_workspace_cache,
    leg_quote_ids,
    load_signal_method,
    market_data_quote_counts,
    market_data_quote_status_for_selections,
    matrix_quote_target_counts_for_plans,
    matrix_quote_status_for_plans,
    matrix_quote_status_for_plan_update,
    matrix_grid_layout,
    matrix_width_band,
    matrix_width_bands,
    split_matrix_grid_layout,
    matrix_tab_key,
    matrix_tab_identity,
    matrix_tab_label,
    matrix_tab_limit_reached,
    matrix_tab_underlying_reference_price,
    matrix_tab_views_conflict,
    matrix_quote_target_selections,
    matrix_cell_visual_legend_items,
    matrix_liquidity_gauge_states,
    next_rank_sort_state,
    parse_formula_settings_values,
    parse_matrix_input_state,
    flatten_matrix_tabs_for_recording,
    preview_market_data_quote_counts,
    resolve_recorder_indicator_state,
    resolve_matrix_cell_fills,
    resolve_matrix_cell_subscription_style,
    save_formula_settings,
    save_matrix_workspace_cache,
    matrix_workspace_cache_path,
    matrix_workspace_from_mapping,
    matrix_workspace_to_dict,
    selected_matrix_widths,
    seed_cached_matrix_quotes,
    selection_has_itm_strike,
    selection_request_key,
    sorted_ranked_signals,
    underlying_reference_price,
    union_matrix_quote_target_selections,
    union_matrix_tab_quote_target_selections,
    matrix_view_rights,
    normalize_matrix_view,
)
from options_matrix.models import (
    DEFAULT_FORMULA_SETTINGS,
    DEFAULT_SIGNAL_METHOD,
    DIRECTION_DEBIT,
    FormulaSettings,
    LIVE_MARKET_DATA_TYPE,
    MatrixSignal,
    MonitoredSideSnapshot,
    RIGHT_CALL,
    RIGHT_PUT,
    RankedMatrixSignal,
    SIGNAL_BUY,
    SIGNAL_KIND_ANOMALY,
    SIGNAL_KIND_OPPORTUNITY,
    SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,
    SIGNAL_METHODS,
    SIGNAL_SELL,
    OptionContractRecord,
    QuoteSnapshot,
    SyntheticSpreadQuote,
    _fit_fair_value_edge_curve,
    build_exact_width_vertical_pairs,
    build_matrix_context_snapshots,
    build_matrix_selection_plan,
    build_matrix_snapshots,
    build_ranked_matrix_signals,
    compute_matrix_signal,
    fair_value_edge_for_width,
    flatten_matrix_context,
    normalize_signal_method,
    pair_key_for_pair,
    pair_key_from_strikes,
    resolve_vertical_selection,
)
from options_matrix.recorder import SNAPSHOT_COLUMNS, RecorderStatus
from options_matrix.positions import POSITION_BOUGHT, POSITION_SOLD, PositionRecord, evaluate_position, position_key_for_selection


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


def _snapshot(lower_strike: float, spread_width: float, mid: float, quote_width: float) -> MonitoredSideSnapshot:
    pair = build_exact_width_vertical_pairs(
        [_option(lower_strike), _option(lower_strike + spread_width)],
        spread_width,
    )[0]
    selection = resolve_vertical_selection(pair, DIRECTION_DEBIT)
    return MonitoredSideSnapshot(
        title=pair.label,
        pair_key=pair_key_for_pair(pair),
        selection=selection,
        synthetic_quote=SyntheticSpreadQuote(
            bid=round(mid - (quote_width / 2.0), 4),
            ask=round(mid + (quote_width / 2.0), 4),
            bid_size=10,
            ask_size=10,
        ),
    )


def _liquidity_snapshot(
    lower_strike: float,
    spread_width: float,
    *,
    right: str = RIGHT_CALL,
    bid: float | None = 0.40,
    ask: float | None = 0.60,
    bid_size: float | None = 10,
    ask_size: float | None = 10,
) -> MonitoredSideSnapshot:
    pair = build_exact_width_vertical_pairs(
        [
            _option(lower_strike, right),
            _option(lower_strike + spread_width, right),
        ],
        spread_width,
    )[0]
    selection = resolve_vertical_selection(pair, DIRECTION_DEBIT)
    return MonitoredSideSnapshot(
        title=pair.label,
        pair_key=pair_key_for_pair(pair),
        selection=selection,
        synthetic_quote=SyntheticSpreadQuote(
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
        ),
    )


def _liquidity_side_state(
    snapshots: list[MonitoredSideSnapshot],
) -> SimpleNamespace:
    snapshots_by_cell = {
        (round(snapshot.selection.pair.lower.strike, 4), round(snapshot.width, 4)): snapshot
        for snapshot in snapshots
    }
    return SimpleNamespace(
        matrix_plan=SimpleNamespace(
            visible={
                cell_key: snapshot.selection
                for cell_key, snapshot in snapshots_by_cell.items()
            }
        ),
        matrix_snapshots=snapshots_by_cell,
    )


def _liquidity_tab(
    snapshots: list[MonitoredSideSnapshot],
    *,
    side: str = RIGHT_CALL,
) -> SimpleNamespace:
    side_state = _liquidity_side_state(snapshots)
    return SimpleNamespace(
        matrix_input=SimpleNamespace(side=side),
        matrix_plan=side_state.matrix_plan,
        matrix_snapshots=side_state.matrix_snapshots,
        side_states={},
    )


def _chain_leg_quotes(
    strikes: list[float],
    adjacent_mids: dict[str, float],
    *,
    top_leg_mid: float,
) -> dict[int, QuoteSnapshot]:
    """Leg quotes whose mids telescope so each adjacent spread's synthetic mid
    equals adjacent_mids[pair_key]; wider spreads get the implied sums."""
    ordered = sorted(strikes)
    leg_mids = {ordered[-1]: top_leg_mid}
    for index in range(len(ordered) - 2, -1, -1):
        lower, upper = ordered[index], ordered[index + 1]
        leg_mids[lower] = round(
            leg_mids[upper] + adjacent_mids[pair_key_from_strikes(lower, upper)], 4
        )
    quotes: dict[int, QuoteSnapshot] = {}
    for strike, leg_mid in leg_mids.items():
        con_id = int(round(strike * 1000))
        quotes[con_id] = QuoteSnapshot(
            label=str(con_id),
            kind="leg",
            contract_con_id=con_id,
            bid=round(leg_mid - 0.05, 4),
            ask=round(leg_mid + 0.05, 4),
            bid_size=10,
            ask_size=10,
            market_data_type=LIVE_MARKET_DATA_TYPE,
        )
    return quotes


def _anchor_call_option(strike: float) -> OptionContractRecord:
    """Call-right record in a distinct con_id space (no clash with _option)."""
    return OptionContractRecord(
        con_id=900000 + int(round(strike * 1000)),
        symbol="SPY",
        expiration="20300117",
        strike=strike,
        right=RIGHT_CALL,
        exchange="SMART",
        currency="USD",
        trading_class="SPY",
        multiplier="100",
        local_symbol=f"SPY ANCHOR CALL {strike}",
    )


def _put_chain_leg_quotes(
    strikes: list[float],
    adjacent_mids: dict[str, float],
    *,
    bottom_leg_mid: float,
) -> dict[int, QuoteSnapshot]:
    """Put-side leg quotes: mids grow with strike so each adjacent put spread's
    synthetic mid (upper leg minus lower leg) equals adjacent_mids[pair_key]."""
    ordered = sorted(strikes)
    leg_mids = {ordered[0]: bottom_leg_mid}
    for index in range(1, len(ordered)):
        lower, upper = ordered[index - 1], ordered[index]
        leg_mids[upper] = round(
            leg_mids[lower] + adjacent_mids[pair_key_from_strikes(lower, upper)], 4
        )
    quotes: dict[int, QuoteSnapshot] = {}
    for strike, leg_mid in leg_mids.items():
        con_id = int(round(strike * 1000))
        quotes[con_id] = QuoteSnapshot(
            label=str(con_id),
            kind="leg",
            contract_con_id=con_id,
            bid=round(leg_mid - 0.05, 4),
            ask=round(leg_mid + 0.05, 4),
            bid_size=10,
            ask_size=10,
            market_data_type=LIVE_MARKET_DATA_TYPE,
        )
    return quotes


def _ranked_signal(
    rank: int,
    pair_key: str,
    width: float,
    signal: str,
    price: float,
    edge: float,
) -> RankedMatrixSignal:
    lower_text, upper_text = pair_key.split("/")
    lower = float(lower_text)
    upper = float(upper_text)
    snapshot = _snapshot(lower, upper - lower, price, 0.10)
    return RankedMatrixSignal(
        rank=rank,
        lower_strike=lower,
        width=width,
        pair_key=pair_key,
        selection=snapshot.selection,
        snapshot=snapshot,
        signal=MatrixSignal(
            signal=signal,
            price=price,
            edge=edge,
            kind=SIGNAL_KIND_OPPORTUNITY,
        ),
    )


def _tab(tab_id: int, label: str, ranked_signals: list[RankedMatrixSignal]) -> MatrixTabState:
    matrix_input = parse_matrix_input_state(
        display_expiration="2030-01-17",
        raw_expiration="20300117",
        side=RIGHT_CALL,
        low_strike="100",
        high_strike="101",
        width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
    )
    return MatrixTabState(
        tab_id=tab_id,
        label=label,
        matrix_input=matrix_input,
        row_strikes=(),
        matrix_plan=build_matrix_selection_plan([], [], []),
        ranked_signals=ranked_signals,
    )


class MatrixModelsTestCase(unittest.TestCase):
    def test_strategy_settings_expose_editable_method_controls(self) -> None:
        editable_keys = {control.key for control in EDITABLE_FORMULA_SETTING_CONTROLS}

        edge_keys = {
            "fair_value_edge_width_rate",
            "fair_value_edge_width_2",
            "fair_value_edge_width_3",
            "fair_value_edge_width_4",
            "fair_value_edge_width_5",
        }
        self.assertTrue(edge_keys.issubset(editable_keys))
        self.assertTrue(edge_keys.issubset({control.key for control in FORMULA_SETTING_CONTROLS}))
        min_points_control = next(
            control
            for control in FORMULA_SETTING_CONTROLS
            if control.key == "fair_value_edge_curve_min_points"
        )
        min_points_purpose = min_points_control.purpose.lower()
        self.assertIn("required for each atomic decision fit after any direct-mid holdout", min_points_purpose)
        self.assertIn("fair value and signal are unavailable", min_points_purpose)
        self.assertNotIn("bounded flat fallback", min_points_purpose)

    def test_fair_value_settings_example_data_tracks_settings_and_composes_atomic_values(self) -> None:
        settings = FormulaSettings(
            fair_value_edge_width_rate=0.11,
            fair_value_edge_width_2=0.15,
            fair_value_edge_width_3=0.18,
            fair_value_edge_width_4=0.21,
            fair_value_edge_width_5=0.23,
        )

        example = fair_value_settings_example_data(settings)

        self.assertTrue(example.__dataclass_params__.frozen)
        self.assertEqual(
            example.edge_schedule,
            ((1.0, 0.11), (2.0, 0.15), (3.0, 0.18), (4.0, 0.21), (5.0, 0.23)),
        )
        self.assertGreaterEqual(len(example.observation_points), 5)
        self.assertEqual(
            {point.role for point in example.observation_points},
            {"market_mid", "wide_market_mid", "held_out_candidate"},
        )
        fitted_values = [value for _x, value in example.fitted_points]
        self.assertTrue(all(0.0 <= value <= 1.0 for value in fitted_values))
        self.assertEqual(fitted_values, sorted(fitted_values))
        self.assertGreaterEqual(len(example.atomic_values), 2)
        self.assertEqual(example.composed_width, float(len(example.atomic_values)))
        self.assertAlmostEqual(example.composed_fair_value, sum(example.atomic_values))
        self.assertEqual(
            example.composed_edge,
            fair_value_edge_for_width(example.composed_width, settings),
        )
        self.assertAlmostEqual(
            example.buy_endpoint,
            example.composed_fair_value - example.composed_edge,
        )
        self.assertAlmostEqual(
            example.sell_endpoint,
            example.composed_fair_value + example.composed_edge,
        )

    def test_fair_value_settings_overview_describes_leave_one_out_robust_regression(self) -> None:
        overview = " ".join(
            f"{title} {description}" for title, description in FAIR_VALUE_SYSTEM_OVERVIEW
        ).lower()

        self.assertIn("every structurally usable mid", overview)
        self.assertIn("displayed and nearby otm context", overview)
        self.assertIn("deep itm display is not blindly fitted", overview)
        self.assertIn("not a hard eligibility cutoff", overview)
        self.assertIn("each call curve's universe contains only eligible call midpoints", overview)
        self.assertIn("each put curve's universe contains only eligible put midpoints", overview)
        self.assertIn("robust, bounded, monotone sigmoid/probability-style regression", overview)
        self.assertIn("leave-one-out decision fit", overview)
        self.assertIn("that observation is held out", overview)
        self.assertIn("outside calibration has no direct observation to remove", overview)
        self.assertIn("remaining eligible observations from that same option side", overview)
        self.assertIn("min regression points is checked on each decision fit after the holdout", overview)
        self.assertIn("fair value and signal are unavailable", overview)
        self.assertIn("remain full-pool diagnostics", overview)
        self.assertIn("call decision fits use only eligible call vertical midpoints", overview)
        self.assertIn("put decision fits use only eligible put vertical midpoints", overview)
        self.assertIn("no opposite-side observation enters either curve", overview)
        self.assertIn("sum of the calibration intervals' leave-one-out fair values", overview)
        self.assertIn("strike widths ending in .50", overview)
        self.assertIn("their individual midpoints never enter the fit", overview)
        self.assertIn("fair minus edge", overview)
        self.assertNotIn("parity", overview)
        self.assertNotIn("including its own observation", overview)
        self.assertNotIn("quadratic", overview)

    def test_fair_value_settings_graph_explains_new_curve_and_unchanged_endpoints(self) -> None:
        class _Canvas:
            def __init__(self) -> None:
                self.texts: list[str] = []

            def delete(self, *_args) -> None:
                return

            def winfo_width(self) -> int:
                return 920

            def create_text(self, *_args, **kwargs) -> None:
                self.texts.append(str(kwargs.get("text", "")))

            def create_line(self, *_args, **_kwargs) -> None:
                return

            def create_oval(self, *_args, **_kwargs) -> None:
                return

            def create_rectangle(self, *_args, **_kwargs) -> None:
                return

        harness = SimpleNamespace(
            grid_font_bold="grid-bold",
            detail_font="detail",
        )
        canvas = _Canvas()

        OptionsMatrixApp._draw_fair_value_system_settings_examples(
            harness,
            canvas,
            DEFAULT_FORMULA_SETTINGS,
        )

        rendered_text = " ".join(canvas.texts).lower()
        self.assertIn("normalized atomic mids -> robust leave-one-out decision fits", rendered_text)
        self.assertIn("candidate's direct mid is in calibration, hold it out", rendered_text)
        self.assertIn("remaining eligible mids from the same option side", rendered_text)
        self.assertIn("same-side atomic mid / interval", rendered_text)
        self.assertIn("wide-market same-side mid (eligible)", rendered_text)
        self.assertIn("candidate held out from own fit", rendered_text)
        self.assertIn("sample loo bounded sigmoid", rendered_text)
        self.assertIn("wider fv = sum of atomic loo fvs", rendered_text)
        self.assertIn("min regression points applies after holdout", rendered_text)
        self.assertIn("insufficient support = no fv/signal", rendered_text)
        self.assertIn("buy <=", rendered_text)
        self.assertIn("sell >=", rendered_text)
        self.assertNotIn("parity", rendered_text)
        self.assertNotIn("usable calibration mids share one fit", rendered_text)

    def test_liquidity_gauges_use_shared_log_p90_for_bid_and_ask_support(self) -> None:
        snapshots = [
            _liquidity_snapshot(100.0, 1.0, bid_size=1, ask_size=3),
            _liquidity_snapshot(101.0, 1.0, bid_size=3, ask_size=9),
            _liquidity_snapshot(102.0, 1.0, bid_size=9, ask_size=9),
            _liquidity_snapshot(103.0, 1.0, bid_size=9, ask_size=100),
        ]

        states = matrix_liquidity_gauge_states(_liquidity_tab(snapshots))

        # The pooled bid-and-ask sizes have a linearly interpolated P90 of
        # 36.3. Log scaling and four display steps therefore map 1, 3, 9,
        # and 100 to .25, .50, .75, and 1.00 respectively.
        self.assertEqual(states[(100.0, 1.0)].bid_support, 0.25)
        self.assertEqual(states[(100.0, 1.0)].ask_support, 0.50)
        self.assertEqual(states[(101.0, 1.0)].bid_support, 0.50)
        self.assertEqual(states[(101.0, 1.0)].ask_support, 0.75)
        self.assertEqual(states[(102.0, 1.0)].bid_support, 0.75)
        self.assertEqual(states[(103.0, 1.0)].ask_support, 1.00)
        self.assertTrue(all(state.size_comparable for state in states.values()))
        self.assertEqual(
            {state.size_peer_count for state in states.values()},
            {LIQUIDITY_GAUGE_MIN_PEERS},
        )

    def test_liquidity_gauges_isolate_same_side_and_exact_fractional_width(self) -> None:
        call_snapshots = [
            *[
                _liquidity_snapshot(100.0 + index, 1.0, bid_size=1, ask_size=1)
                for index in range(LIQUIDITY_GAUGE_MIN_PEERS)
            ],
            *[
                _liquidity_snapshot(110.0 + index, 1.25, bid_size=100, ask_size=100)
                for index in range(LIQUIDITY_GAUGE_MIN_PEERS)
            ],
        ]
        put_snapshots = [
            _liquidity_snapshot(
                120.0 + index,
                1.0,
                right=RIGHT_PUT,
                bid_size=1000,
                ask_size=1000,
            )
            for index in range(LIQUIDITY_GAUGE_MIN_PEERS)
        ]
        tab = SimpleNamespace(
            matrix_input=SimpleNamespace(side=MATRIX_VIEW_BOTH),
            matrix_plan=SimpleNamespace(visible={}),
            matrix_snapshots={},
            side_states={
                RIGHT_CALL: _liquidity_side_state(call_snapshots),
                RIGHT_PUT: _liquidity_side_state(put_snapshots),
            },
        )

        states = matrix_liquidity_gauge_states(tab)

        call_width_one = states[(RIGHT_CALL, 100.0, 1.0)]
        call_fractional = states[(RIGHT_CALL, 110.0, 1.25)]
        put_width_one = states[(RIGHT_PUT, 120.0, 1.0)]
        for state in (call_width_one, call_fractional, put_width_one):
            self.assertTrue(state.size_comparable)
            self.assertEqual(state.size_peer_count, LIQUIDITY_GAUGE_MIN_PEERS)
            self.assertEqual((state.bid_support, state.ask_support), (1.0, 1.0))

    def test_liquidity_gauge_distinguishes_missing_size_from_zero(self) -> None:
        snapshots = [
            _liquidity_snapshot(100.0, 1.0, bid_size=None, ask_size=0),
            *[
                _liquidity_snapshot(101.0 + index, 1.0, bid_size=10, ask_size=10)
                for index in range(LIQUIDITY_GAUGE_MIN_PEERS - 1)
            ],
        ]

        state = matrix_liquidity_gauge_states(_liquidity_tab(snapshots))[(100.0, 1.0)]

        self.assertTrue(state.size_comparable)
        self.assertEqual(state.size_peer_count, LIQUIDITY_GAUGE_MIN_PEERS)
        self.assertIsNone(state.bid_support)
        self.assertEqual(state.ask_support, 0.0)

    def test_liquidity_gauge_leaves_insufficient_and_all_zero_curves_unranked(self) -> None:
        insufficient = [
            _liquidity_snapshot(100.0 + index, 1.0, bid_size=10, ask_size=10)
            for index in range(LIQUIDITY_GAUGE_MIN_PEERS - 1)
        ]
        insufficient_states = matrix_liquidity_gauge_states(
            _liquidity_tab(insufficient)
        )
        self.assertTrue(
            all(
                not state.size_comparable
                and state.size_peer_count == LIQUIDITY_GAUGE_MIN_PEERS - 1
                and state.bid_support is None
                and state.ask_support is None
                for state in insufficient_states.values()
            )
        )

        all_zero = [
            _liquidity_snapshot(110.0 + index, 1.0, bid_size=0, ask_size=0)
            for index in range(LIQUIDITY_GAUGE_MIN_PEERS)
        ]
        zero_states = matrix_liquidity_gauge_states(_liquidity_tab(all_zero))
        self.assertTrue(
            all(
                not state.size_comparable
                and state.size_peer_count == LIQUIDITY_GAUGE_MIN_PEERS
                and state.bid_support is None
                and state.ask_support is None
                for state in zero_states.values()
            )
        )

        mostly_zero = [
            _liquidity_snapshot(
                120.0 + index,
                1.0,
                bid_size=10 if index == 0 else 0,
                ask_size=0,
            )
            for index in range(6)
        ]
        mostly_zero_states = matrix_liquidity_gauge_states(
            _liquidity_tab(mostly_zero)
        )
        self.assertTrue(all(state.size_comparable for state in mostly_zero_states.values()))
        self.assertEqual(mostly_zero_states[(120.0, 1.0)].bid_support, 1.0)
        self.assertEqual(mostly_zero_states[(121.0, 1.0)].bid_support, 0.0)

    def test_liquidity_quote_width_normalizes_and_flags_overflow_and_crossed(self) -> None:
        snapshots = [
            _liquidity_snapshot(100.0, 1.0, bid=-0.10, ask=0.15),
            _liquidity_snapshot(101.0, 2.0, bid=-0.20, ask=0.30),
            _liquidity_snapshot(103.0, 1.0, bid=-0.10, ask=1.20),
            _liquidity_snapshot(104.0, 1.0, bid=0.60, ask=0.50),
        ]

        states = matrix_liquidity_gauge_states(_liquidity_tab(snapshots))

        normal = states[(100.0, 1.0)]
        normalized_wider = states[(101.0, 2.0)]
        overflow = states[(103.0, 1.0)]
        crossed = states[(104.0, 1.0)]
        self.assertEqual(normal.quote_width_ratio, 0.25)
        self.assertFalse(normal.quote_width_overflow)
        self.assertFalse(normal.quote_crossed)
        self.assertEqual(normalized_wider.quote_width_ratio, 0.25)
        self.assertEqual(overflow.quote_width_ratio, 1.0)
        self.assertTrue(overflow.quote_width_overflow)
        self.assertFalse(overflow.quote_crossed)
        self.assertIsNone(crossed.quote_width_ratio)
        self.assertFalse(crossed.quote_width_overflow)
        self.assertTrue(crossed.quote_crossed)
        self.assertFalse(crossed.size_comparable)


    def test_position_exit_fills_preserve_selection_and_itm_base(self) -> None:
        selection = _snapshot(100.0, 1.0, 0.50, 0.10).selection
        for selected, is_itm, base in (
            (False, False, GRID_BG), (True, False, SELECTED_BG),
            (False, True, ITM_BG), (True, True, ITM_SELECTED_BG),
        ):
            for signal, tint in ((SIGNAL_BUY, POSITION_BUY_BG), (SIGNAL_SELL, POSITION_SELL_BG)):
                with self.subTest(selected=selected, is_itm=is_itm, signal=signal):
                    self.assertEqual(
                        resolve_matrix_cell_fills(selection, selected=selected, is_itm=is_itm, exit_signal=signal),
                        (base, tint),
                    )

    def test_cell_exit_fills_require_a_live_existing_pair_and_valid_exit(self) -> None:
        selection = _snapshot(100.0, 1.0, 0.50, 0.10).selection
        self.assertEqual(
            resolve_matrix_cell_fills(None, selected=True, is_itm=False, exit_signal=SIGNAL_BUY),
            (MISSING_BG, MISSING_BG),
        )
        for signal in (None, SIGNAL_BUY, SIGNAL_SELL, "INVALID"):
            for live in (True, False):
                if live and signal in (SIGNAL_BUY, SIGNAL_SELL):
                    continue
                with self.subTest(signal=signal, live=live):
                    self.assertEqual(
                        resolve_matrix_cell_fills(selection, selected=True, is_itm=False, live=live, exit_signal=signal),
                        (SELECTED_BG, SELECTED_BG),
                    )


    def test_position_alerts_do_not_change_the_recorder_schema(self) -> None:
        self.assertEqual(len(SNAPSHOT_COLUMNS), 42)
        self.assertFalse(
            any(
                "watch" in column.lower() or "alert" in column.lower()
                for column in SNAPSHOT_COLUMNS
            )
        )

    def test_compact_matrix_signal_text_preserves_state_in_dense_cells(self) -> None:
        self.assertEqual(compact_matrix_signal_text("B 0.42 (0.12)"), "B.42")
        self.assertEqual(compact_matrix_signal_text("S 1.68 (0.12)"), "S1.68")
        self.assertEqual(compact_matrix_signal_text("B -0.05 (0.10)"), "B-.05")
        self.assertEqual(compact_matrix_signal_text("M 0.55 FV?"), "M.55?")
        self.assertEqual(compact_matrix_signal_text("M 0.55"), "M.55")
        self.assertEqual(compact_matrix_signal_text("M -0.05"), "M-.05")
        self.assertEqual(compact_matrix_signal_text("M -0.05 FV?"), "M-.05?")
        self.assertEqual(compact_matrix_signal_text("..."), "...")
        self.assertEqual(compact_matrix_signal_text("--"), "--")

    def test_liquidity_gauge_canvas_geometry_and_state_transitions(self) -> None:
        class _Canvas:
            def __init__(self) -> None:
                self.next_id = 0
                self.coordinates: dict[int, tuple[float, ...]] = {}
                self.options: dict[int, dict[str, object]] = {}

            def _create(self, coordinates: tuple[float, ...], options: dict[str, object]) -> int:
                self.next_id += 1
                self.coordinates[self.next_id] = coordinates
                self.options[self.next_id] = dict(options)
                return self.next_id

            def create_line(self, *coordinates: float, **options: object) -> int:
                return self._create(coordinates, options)

            def create_rectangle(self, *coordinates: float, **options: object) -> int:
                return self._create(coordinates, options)

            def coords(self, item_id: int, *coordinates: float) -> None:
                self.coordinates[item_id] = coordinates

            def itemconfigure(self, item_id: int, **options: object) -> None:
                self.options[item_id].update(options)

        canvas = _Canvas()
        state = MatrixLiquidityGaugeState(
            quote_width_ratio=0.5,
            bid_support=0.75,
            ask_support=0.25,
            size_comparable=True,
            size_peer_count=8,
        )
        items = OptionsMatrixApp._draw_matrix_liquidity_gauge(
            SimpleNamespace(),
            canvas,
            signal_x=50.0,
            signal_width=70.0,
            y=40.0,
            row_height=20.0,
            state=state,
            muted=False,
        )

        self.assertIsNotNone(items)
        assert items is not None
        self.assertLessEqual(items.right, 116.0)
        self.assertGreaterEqual(items.left, 50.0)
        self.assertEqual((items.quote_y, items.size_top, items.size_bottom), (46, 52, 56))
        quote_coordinates = canvas.coordinates[items.quote_id]
        self.assertAlmostEqual(
            quote_coordinates[6] - quote_coordinates[4],
            (items.right - items.left) * 0.5,
        )
        center = (items.left + items.right) / 2.0
        half_width = ((items.right - items.left) / 2.0) - 1.0
        self.assertAlmostEqual(canvas.coordinates[items.bid_id][0], center - (half_width * 0.75))
        self.assertAlmostEqual(canvas.coordinates[items.ask_id][2], center + (half_width * 0.25))

        OptionsMatrixApp._update_matrix_liquidity_gauge(
            SimpleNamespace(),
            canvas,
            items,
            MatrixLiquidityGaugeState(
                quote_width_ratio=0.25,
                bid_support=None,
                ask_support=0.0,
                size_comparable=True,
                size_peer_count=8,
            ),
            muted=False,
        )
        self.assertEqual(canvas.options[items.bid_id]["fill"], "")
        self.assertEqual(canvas.options[items.bid_id]["state"], "normal")
        self.assertEqual(canvas.options[items.ask_id]["state"], "hidden")

        OptionsMatrixApp._update_matrix_liquidity_gauge(
            SimpleNamespace(),
            canvas,
            items,
            MatrixLiquidityGaugeState(quote_crossed=True),
            muted=False,
        )
        self.assertEqual(canvas.options[items.quote_id]["width"], 2)
        for item_id in (
            items.size_track_id,
            items.bid_id,
            items.ask_id,
            items.center_id,
        ):
            self.assertEqual(canvas.options[item_id]["state"], "hidden")

        OptionsMatrixApp._update_matrix_liquidity_gauge(
            SimpleNamespace(),
            canvas,
            items,
            None,
            muted=False,
        )
        self.assertTrue(
            all(
                canvas.options[item_id]["state"] == "hidden"
                for item_id in (
                    items.quote_id,
                    items.auxiliary_id,
                    items.size_track_id,
                    items.bid_id,
                    items.ask_id,
                    items.center_id,
                )
            )
        )

    def test_matrix_cell_visual_legend_covers_every_runtime_cell_state(self) -> None:
        items = matrix_cell_visual_legend_items()
        by_key = {item.key: item for item in items}
        expected_keys = {
            "fill_otm",
            "fill_selected_otm",
            "fill_itm",
            "fill_selected_itm",
            "fill_missing",
            "position_saved",
            "position_exit",
            "border_confirmed",
            "border_pending",
            "border_reconciling",
            "border_context",
            "border_failed",
            "border_recurrent_failed",
            "text_buy",
            "text_sell",
            "text_anomaly",
            "text_neutral",
            "text_fv_waiting",
            "text_waiting",
            "text_no_mid",
            "text_itm_muted",
            "liquidity_quote_width",
            "liquidity_relative_size",
            "liquidity_missing_size",
            "liquidity_unranked",
            "liquidity_overflow",
            "liquidity_crossed",
            "state_stale",
        }

        self.assertEqual(set(by_key), expected_keys)
        self.assertTrue(all(item.__dataclass_params__.frozen for item in items))
        self.assertTrue(all(item.label.strip() and item.description.strip() for item in items))
        legend_copy = " ".join(
            f"{item.label} {item.description}" for item in items
        ).lower()
        self.assertNotIn("parity", legend_copy)
        self.assertIn("hidden same-side fair-value context", by_key["border_context"].description.lower())
        fv_waiting_description = by_key["text_fv_waiting"].description.lower()
        self.assertIn("enough remaining usable observations after the leave-one-out holdout", fv_waiting_description)
        self.assertIn("complete atomic leave-one-out coverage", fv_waiting_description)

        liquidity_keys = {
            "liquidity_quote_width",
            "liquidity_relative_size",
            "liquidity_missing_size",
            "liquidity_unranked",
            "liquidity_overflow",
            "liquidity_crossed",
        }
        self.assertEqual(
            {item.key for item in items if item.category == "liquidity"},
            liquidity_keys,
        )
        self.assertIn("ask minus bid", by_key["liquidity_quote_width"].description.lower())
        self.assertIn("strike distance", by_key["liquidity_quote_width"].description.lower())
        relative_size_copy = by_key["liquidity_relative_size"].description.lower()
        self.assertIn("share a log scale", relative_size_copy)
        self.assertIn("same-side, exact-width", relative_size_copy)
        self.assertIn("90th percentile", relative_size_copy)
        self.assertIn("not guaranteed execution", relative_size_copy)
        self.assertIn("missing and zero remain distinct", by_key["liquidity_missing_size"].description.lower())
        self.assertIn("not ranked", by_key["liquidity_unranked"].description.lower())
        self.assertIn("quote remains available", by_key["liquidity_overflow"].description.lower())
        self.assertIn("ask is below its bid", by_key["liquidity_crossed"].description.lower())

        confirmed = resolve_matrix_cell_subscription_style(["subscribed"], ["subscribed"])
        pending = resolve_matrix_cell_subscription_style(["pending_subscribe"], ["subscribed"])
        reconciling = resolve_matrix_cell_subscription_style(["desired"], ["subscribed"])
        context = resolve_matrix_cell_subscription_style(["subscribed"], ["failed"])
        failed = resolve_matrix_cell_subscription_style(["failed"], recurrent_failures=1)
        recurrent = resolve_matrix_cell_subscription_style(["failed"], recurrent_failures=2)
        expected_border_styles = {
            "border_confirmed": confirmed,
            "border_pending": pending,
            "border_reconciling": reconciling,
            "border_context": context,
            "border_failed": failed,
            "border_recurrent_failed": recurrent,
        }
        for key, expected in expected_border_styles.items():
            item = by_key[key]
            self.assertEqual((item.outline, item.border_width, item.dash), (expected.outline, expected.width, expected.dash))

    def test_signal_methods_only_include_fair_value_edge_curve(self) -> None:
        self.assertEqual(SIGNAL_METHODS, (SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,))
        self.assertEqual(DEFAULT_SIGNAL_METHOD, SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE)
        self.assertEqual(normalize_signal_method("fair value edge curve"), SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE)
        with self.assertRaises(ValueError):
            normalize_signal_method("Multi-Variable")

    def test_formula_settings_round_trip_to_dict(self) -> None:
        settings = FormulaSettings(
            fair_value_edge_width_rate=0.20,
            fair_value_edge_width_2=0.24,
            fair_value_edge_width_3=0.27,
            fair_value_edge_width_4=0.30,
            fair_value_edge_width_5=0.32,
        )

        payload = formula_settings_to_dict(settings)
        restored = formula_settings_from_mapping(payload)

        self.assertEqual(len(payload), len(FORMULA_SETTING_CONTROLS))
        self.assertEqual(restored, settings)

    def test_formula_settings_payload_can_include_signal_method(self) -> None:
        settings = FormulaSettings(fair_value_edge_width_rate=0.25)

        payload = formula_settings_to_dict(settings, signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE)

        self.assertEqual(len(payload), len(FORMULA_SETTING_CONTROLS) + 1)
        self.assertEqual(payload["signal_method"], SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE)
        self.assertEqual(formula_settings_from_mapping(payload).fair_value_edge_width_rate, 0.25)

    def test_formula_settings_mapping_uses_defaults_for_missing_or_invalid_values(self) -> None:
        legacy = formula_settings_from_mapping({"fair_value_edge_width_rate": "0.07"})
        self.assertEqual(legacy.fair_value_edge_width_rate, 0.07)
        self.assertEqual(
            (
                legacy.fair_value_edge_width_2,
                legacy.fair_value_edge_width_3,
                legacy.fair_value_edge_width_4,
                legacy.fair_value_edge_width_5,
            ),
            tuple(fair_value_edge_for_width(float(width), 0.07) for width in range(2, 6)),
        )
        self.assertEqual(
            formula_settings_from_mapping({"fair_value_edge_width_rate": "bad"}).fair_value_edge_width_rate,
            DEFAULT_FORMULA_SETTINGS.fair_value_edge_width_rate,
        )
        self.assertEqual(
            formula_settings_from_mapping({"fair_value_edge_width_rate": -1.0}).fair_value_edge_width_rate,
            DEFAULT_FORMULA_SETTINGS.fair_value_edge_width_rate,
        )

    def test_formula_settings_mapping_ignores_legacy_multi_variable_keys(self) -> None:
        settings = formula_settings_from_mapping(
            {
                "opportunity_threshold": 0.08,
                "reference_width_weight": 0.45,
                "fair_value_edge_width_rate": 0.20,
            }
        )

        self.assertEqual(settings.fair_value_edge_width_rate, 0.20)
        self.assertEqual(
            (
                settings.fair_value_edge_width_2,
                settings.fair_value_edge_width_3,
                settings.fair_value_edge_width_4,
                settings.fair_value_edge_width_5,
            ),
            tuple(fair_value_edge_for_width(float(width), 0.20) for width in range(2, 6)),
        )

    def test_formula_settings_mixed_explicit_schedule_defaults_missing_new_edges(self) -> None:
        settings = formula_settings_from_mapping(
            {
                "fair_value_edge_width_rate": 0.20,
                "fair_value_edge_width_2": 0.25,
            }
        )

        self.assertEqual(settings.fair_value_edge_width_rate, 0.20)
        self.assertEqual(settings.fair_value_edge_width_2, 0.25)
        self.assertEqual(settings.fair_value_edge_width_3, DEFAULT_FORMULA_SETTINGS.fair_value_edge_width_3)
        self.assertEqual(settings.fair_value_edge_width_4, DEFAULT_FORMULA_SETTINGS.fair_value_edge_width_4)
        self.assertEqual(settings.fair_value_edge_width_5, DEFAULT_FORMULA_SETTINGS.fair_value_edge_width_5)

    def test_explicit_edge_schedule_interpolates_fractional_widths(self) -> None:
        settings = FormulaSettings(
            fair_value_edge_width_rate=0.10,
            fair_value_edge_width_2=0.20,
            fair_value_edge_width_3=0.30,
            fair_value_edge_width_4=0.40,
            fair_value_edge_width_5=0.50,
        )

        self.assertEqual(fair_value_edge_for_width(2.25, settings), 0.225)
        self.assertEqual(fair_value_edge_for_width(4.75, settings), 0.475)

    def test_formula_settings_strict_parser_rejects_invalid_apply_values(self) -> None:
        values = {
            key: str(value)
            for key, value in formula_settings_to_dict(DEFAULT_FORMULA_SETTINGS).items()
        }
        values["fair_value_edge_width_rate"] = "-1"

        with self.assertRaises(ValueError):
            parse_formula_settings_values(values)

    def test_formula_settings_config_path_uses_appdata_then_home_fallback(self) -> None:
        self.assertEqual(
            formula_settings_config_path(environ={"APPDATA": "C:/Users/Test/AppData/Roaming"}),
            Path("C:/Users/Test/AppData/Roaming") / "OptionsMatrix" / "settings.json",
        )
        self.assertEqual(
            formula_settings_config_path(environ={}, home=Path("C:/Users/Test")),
            Path("C:/Users/Test") / ".options_matrix_settings.json",
        )

    def test_formula_settings_save_load_and_invalid_file_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "settings.json"
            settings = FormulaSettings(fair_value_edge_width_rate=0.25)

            save_formula_settings(
                settings,
                config_path,
                signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,
            )
            loaded = load_formula_settings(config_path)
            self.assertEqual(loaded.fair_value_edge_width_rate, 0.25)
            self.assertEqual(load_signal_method(config_path), SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE)
            saved_payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(
                {
                    "fair_value_edge_width_rate",
                    "fair_value_edge_width_2",
                    "fair_value_edge_width_3",
                    "fair_value_edge_width_4",
                    "fair_value_edge_width_5",
                }.issubset(saved_payload)
            )

            config_path.write_text("{not valid json", encoding="utf-8")
            self.assertEqual(load_formula_settings(config_path), DEFAULT_FORMULA_SETTINGS)
            self.assertEqual(load_signal_method(config_path), DEFAULT_SIGNAL_METHOD)

    def test_formula_settings_atomic_save_preserves_existing_file_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "settings.json"
            original_payload = {"signal_method": SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE}
            config_path.write_text(json.dumps(original_payload), encoding="utf-8")

            with patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    save_formula_settings(FormulaSettings(), config_path)

            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), original_payload)
            self.assertEqual(list(config_path.parent.glob(f".{config_path.name}.*.tmp")), [])

    def test_formula_settings_loads_partial_config_field_by_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "settings.json"
            config_path.write_text(
                json.dumps(
                    {
                        "opportunity_threshold": 0.08,
                        "fair_value_edge_width_rate": "bad",
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_formula_settings(config_path)

            self.assertEqual(
                loaded.fair_value_edge_width_rate,
                DEFAULT_FORMULA_SETTINGS.fair_value_edge_width_rate,
            )

    def test_signal_method_loads_default_for_missing_invalid_or_legacy_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "settings.json"

            config_path.write_text(json.dumps({"opportunity_threshold": 0.08}), encoding="utf-8")
            self.assertEqual(load_signal_method(config_path), DEFAULT_SIGNAL_METHOD)

            config_path.write_text(json.dumps({"signal_method": "Not Real"}), encoding="utf-8")
            self.assertEqual(load_signal_method(config_path), DEFAULT_SIGNAL_METHOD)

            config_path.write_text(json.dumps({"signal_method": "Multi-Variable"}), encoding="utf-8")
            self.assertEqual(load_signal_method(config_path), SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE)

    def test_matrix_workspace_cache_path_uses_appdata_then_home_fallback(self) -> None:
        self.assertEqual(
            matrix_workspace_cache_path(environ={"APPDATA": "C:/Users/Test/AppData/Roaming"}),
            Path("C:/Users/Test/AppData/Roaming") / "OptionsMatrix" / "matrix_cache.json",
        )
        self.assertEqual(
            matrix_workspace_cache_path(environ={}, home=Path("C:/Users/Test")),
            Path("C:/Users/Test") / ".options_matrix_cache.json",
        )

    def test_matrix_workspace_cache_round_trip_preserves_active_tab_and_selected_cell(self) -> None:
        workspace = CachedMatrixWorkspace(
            ticker_symbol="SPY",
            active_tab_identity=("SPY", "20300117", RIGHT_CALL),
            tabs=(
                CachedMatrixTab(
                    raw_expiration="20300117",
                    side=RIGHT_CALL,
                    low_strike=100.0,
                    high_strike=105.0,
                    widths=(1.0, 2.0),
                    selected_cell_key=(101.0, 1.0),
                    label="Jan17'30 C 100-105",
                    row_strikes=(100.0, 101.0, 102.0, 103.0, 104.0, 105.0),
                ),
            ),
        )

        payload = matrix_workspace_to_dict(workspace)
        restored = matrix_workspace_from_mapping(payload)

        self.assertEqual(payload["matrix_mode"], DEFAULT_MATRIX_MODE)
        self.assertEqual(restored, workspace)

    def test_both_matrix_cache_round_trip_uses_view_type_and_side_aware_selection(self) -> None:
        workspace = CachedMatrixWorkspace(
            ticker_symbol="SPY",
            active_tab_identity=("SPY", "20300117", MATRIX_VIEW_BOTH),
            tabs=(
                CachedMatrixTab(
                    raw_expiration="20300117",
                    side=MATRIX_VIEW_BOTH,
                    low_strike=100.0,
                    high_strike=105.0,
                    widths=(1.0, 2.0),
                    selected_cell_key=(RIGHT_PUT, 101.0, 1.0),
                    label="Jan17'30 C/P 100-105",
                    row_strikes=(100.0, 101.0, 102.0, 103.0, 104.0, 105.0),
                ),
            ),
        )

        payload = matrix_workspace_to_dict(workspace)
        restored = matrix_workspace_from_mapping(payload)

        self.assertEqual(payload["tabs"][0]["view_type"], MATRIX_VIEW_BOTH)
        self.assertNotIn("side", payload["tabs"][0])
        self.assertEqual(restored, workspace)

    def test_matrix_cache_migrates_legacy_side_key(self) -> None:
        restored = matrix_workspace_from_mapping(
            {
                "version": 1,
                "matrix_mode": MATRIX_MODE_REGULAR,
                "ticker_symbol": "SPY",
                "tabs": [
                    {
                        "raw_expiration": "20300117",
                        "side": RIGHT_CALL,
                        "low_strike": 100.0,
                        "high_strike": 105.0,
                        "widths": [1.0],
                    }
                ],
            }
        )

        self.assertIsNotNone(restored)
        self.assertEqual(restored.tabs[0].side, RIGHT_CALL)

    def test_matrix_cache_ignores_all_legacy_watch_data_without_losing_tabs(self) -> None:
        for side in (RIGHT_CALL, RIGHT_PUT, MATRIX_VIEW_BOTH):
            for legacy in ([["CALL", 100.0, 101.0]], [], "malformed", {"unexpected": True}, None):
                with self.subTest(side=side, legacy=legacy):
                    selected = [RIGHT_PUT, 100.0, 1.0] if side == MATRIX_VIEW_BOTH else [100.0, 1.0]
                    payload = {
                        "version": 1, "ticker_symbol": "SPY",
                        "active_tab_identity": ["SPY", "20300117", side],
                        "tabs": [{
                            "raw_expiration": "20300117", "view_type": side,
                            "low_strike": 100.0, "high_strike": 105.0,
                            "widths": [1.0, 2.0], "row_strikes": [100.0, 101.0, 102.0],
                            "selected_cell_key": selected, "watched_spreads": legacy,
                        }],
                    }
                    restored = matrix_workspace_from_mapping(payload)
                    self.assertIsNotNone(restored)
                    self.assertEqual(len(restored.tabs), 1)
                    self.assertEqual(restored.tabs[0].side, side)
                    self.assertEqual(restored.tabs[0].widths, (1.0, 2.0))
                    self.assertEqual(restored.tabs[0].selected_cell_key, tuple(selected))
                    self.assertEqual(restored.active_tab_identity, ("SPY", "20300117", side))
                    self.assertFalse(hasattr(restored.tabs[0], "watched_spreads"))
                    self.assertNotIn("watched_spreads", matrix_workspace_to_dict(restored)["tabs"][0])

    def test_matrix_workspace_cache_load_save_and_invalid_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "matrix_cache.json"
            workspace = CachedMatrixWorkspace(
                ticker_symbol="SPY",
                tabs=(
                    CachedMatrixTab(
                        raw_expiration="20300117",
                        side=RIGHT_CALL,
                        low_strike=100.0,
                        high_strike=105.0,
                        widths=(1.0,),
                        label="Jan17'30 C 100-105",
                        row_strikes=(100.0, 101.0, 102.0, 103.0, 104.0, 105.0),
                    ),
                ),
            )

            save_matrix_workspace_cache(workspace, cache_path)
            self.assertEqual(load_matrix_workspace_cache(cache_path), workspace)

            cache_path.write_text("{not valid json", encoding="utf-8")
            self.assertIsNone(load_matrix_workspace_cache(cache_path))

    def test_matrix_workspace_cache_rejects_missing_partial_or_wrong_version_payloads(self) -> None:
        # "opportunity_threshold" is a legacy key from removed Multi-Variable
        # support; caches that still contain it must keep loading.
        valid_tab = {
            "raw_expiration": "20300117",
            "side": RIGHT_CALL,
            "low_strike": 100.0,
            "high_strike": 105.0,
            "widths": [1.0],
            "opportunity_threshold": 0.10,
        }

        self.assertIsNone(matrix_workspace_from_mapping({}))
        self.assertIsNone(
            matrix_workspace_from_mapping(
                {"version": 999, "ticker_symbol": "SPY", "tabs": [valid_tab]}
            )
        )
        self.assertIsNone(
            matrix_workspace_from_mapping(
                {"version": 1, "ticker_symbol": "SPY", "tabs": [{**valid_tab, "raw_expiration": ""}]}
            )
        )
        self.assertEqual(
            matrix_workspace_from_mapping(
                {"version": 1, "ticker_symbol": "SPY", "tabs": [valid_tab]}
            ).matrix_mode,
            DEFAULT_MATRIX_MODE,
        )
        unified = matrix_workspace_from_mapping(
            {
                "version": 1,
                "matrix_mode": MATRIX_MODE_MODIFIED,
                "ticker_symbol": "SPY",
                "tabs": [{**valid_tab, "widths": [1.0, 6.0]}],
            }
        )
        self.assertIsNotNone(unified)
        self.assertEqual(unified.matrix_mode, DEFAULT_MATRIX_MODE)
        self.assertEqual(unified.tabs[0].widths, (1.0,))

    def test_legacy_regular_and_modified_cache_modes_load_identical_unified_workspace(self) -> None:
        def restore(legacy_mode: str) -> CachedMatrixWorkspace | None:
            return matrix_workspace_from_mapping(
                {
                    "version": 1,
                    "matrix_mode": legacy_mode,
                    "ticker_symbol": "SPY",
                    "tabs": [
                        {
                            "raw_expiration": "20300117",
                            "view_type": RIGHT_CALL,
                            "low_strike": 100.0,
                            "high_strike": 105.0,
                            "widths": [0.5, 1.25, 2.75, 5.0, 6.0],
                            "row_strikes": [100.0, 100.5, 101.75, 105.0],
                        }
                    ],
                }
            )

        regular = restore(MATRIX_MODE_REGULAR)
        modified = restore(MATRIX_MODE_MODIFIED)

        self.assertIsNotNone(regular)
        self.assertEqual(regular, modified)
        self.assertEqual(regular.matrix_mode, DEFAULT_MATRIX_MODE)
        self.assertEqual(regular.tabs[0].widths, (1.25, 2.75, 5.0))

    def test_cached_matrix_workspace_from_tabs_uses_current_tab_state(self) -> None:
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="105",
            width_states={2.0: True, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        tab = MatrixTabState(
            tab_id=7,
            label=matrix_tab_label(matrix_input),
            matrix_input=matrix_input,
            row_strikes=(),
            matrix_plan=build_matrix_selection_plan([], [], []),
            selected_cell_key=(101.0, 1.0),
        )

        workspace = cached_matrix_workspace_from_tabs([tab], active_tab_id=7)

        self.assertIsNotNone(workspace)
        self.assertEqual(workspace.ticker_symbol, "SPY")
        self.assertEqual(workspace.active_tab_identity, ("SPY", "20300117", RIGHT_CALL))
        self.assertEqual(workspace.tabs[0].widths, (1.0, 2.0))
        self.assertEqual(workspace.tabs[0].selected_cell_key, (101.0, 1.0))

    def test_clear_matrix_workspace_cache_removes_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "matrix_cache.json"
            cache_path.write_text("{}", encoding="utf-8")

            clear_matrix_workspace_cache(cache_path)

            self.assertFalse(cache_path.exists())

    def test_half_dollar_widths_and_cached_selections_are_removed(self) -> None:
        tab = CachedMatrixTab(
            raw_expiration="20300117",
            side=MATRIX_VIEW_BOTH,
            low_strike=100.0,
            high_strike=105.0,
            widths=(0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.5, 3.5, 4.5),
            row_strikes=(100.0, 100.25, 100.5, 101.0, 101.5, 102.0),
            selected_cell_key=(RIGHT_CALL, 100.0, 0.5),
        )
        encoded = matrix_workspace_to_dict(CachedMatrixWorkspace(ticker_symbol="SPY", tabs=(tab,)))
        self.assertEqual(encoded["tabs"][0]["widths"], [0.25, 0.75, 1.0, 1.25])
        self.assertIsNone(encoded["tabs"][0]["selected_cell_key"])

        # Old on-disk workspaces must be cleaned on restore as well as save.
        encoded["tabs"][0].update(
            widths=list(tab.widths),
            selected_cell_key=list(tab.selected_cell_key),
        )
        restored = matrix_workspace_from_mapping(encoded)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.tabs[0].widths, (0.25, 0.75, 1.0, 1.25))
        self.assertIsNone(restored.tabs[0].selected_cell_key)
        for width in (0.5, 1.5, 2.5, 3.5, 4.5):
            with self.subTest(excluded_width=width):
                encoded["tabs"][0]["selected_cell_key"] = [RIGHT_CALL, 100.0, width]
                self.assertIsNone(matrix_workspace_from_mapping(encoded).tabs[0].selected_cell_key)
                cached_tab = replace(tab, selected_cell_key=(RIGHT_CALL, 100.0, width))
                saved = matrix_workspace_to_dict(CachedMatrixWorkspace(ticker_symbol="SPY", tabs=(cached_tab,)))
                self.assertIsNone(saved["tabs"][0]["selected_cell_key"])

        matrix_input = replace(
            _tab(1, "Tab", []).matrix_input,
            ticker_symbol="SPY",
            widths=tab.widths,
            high_strike=102.0,
        )
        state = MatrixTabState(
            tab_id=1,
            label="Tab",
            matrix_input=matrix_input,
            row_strikes=tab.row_strikes,
            matrix_plan=build_matrix_selection_plan([], [], []),
            selected_cell_key=(100.0, 0.5),
        )
        current = cached_matrix_workspace_from_tabs([state], active_tab_id=1)
        self.assertIsNotNone(current)
        self.assertEqual(current.tabs[0].widths, (0.25, 0.75, 1.0, 1.25))
        self.assertIsNone(current.tabs[0].selected_cell_key)

    def test_half_dollar_only_cache_discovers_supported_wider_pairs(self) -> None:
        mapping = {
            "version": 1,
            "ticker_symbol": "SPY",
            "tabs": [{
                "raw_expiration": "20300117",
                "view_type": RIGHT_CALL,
                "low_strike": 100.0,
                "high_strike": 101.0,
                "widths": [0.5],
                "row_strikes": [100.0, 100.5, 101.0],
            }],
        }
        restored = matrix_workspace_from_mapping(mapping)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.tabs[0].widths, (1.0,))
        mapping["tabs"][0].update(high_strike=100.5, row_strikes=[100.0, 100.5])
        self.assertIsNone(matrix_workspace_from_mapping(mapping))

    def test_apply_formula_settings_updates_all_tabs_and_refreshes_views(self) -> None:
        class _Harness:
            def __init__(self) -> None:
                self.formula_settings = DEFAULT_FORMULA_SETTINGS
                self.matrix_tabs = [_tab(1, "Tab 1", []), _tab(2, "Tab 2", [])]
                self.rebuilt: list[int] = []
                self.synced = False
                self.redrawn = False
                self.quote_status_refreshed = False
                self.cache_saved = False

            def _rebuild_tab_from_quotes(self, tab: MatrixTabState) -> None:
                self.rebuilt.append(tab.tab_id)

            def _sync_view_from_active_tab(self) -> None:
                self.synced = True

            def _redraw_all(self) -> None:
                self.redrawn = True

            def _refresh_staged_market_data_quote_status(self) -> None:
                self.quote_status_refreshed = True

            def _save_current_matrix_cache(self) -> None:
                self.cache_saved = True

        harness = _Harness()
        settings = FormulaSettings(fair_value_edge_width_rate=0.06)

        result = OptionsMatrixApp._apply_formula_settings(
            harness,
            settings,
            save=False,
            status_message=None,
        )

        self.assertTrue(result)
        self.assertEqual(harness.formula_settings, settings)
        self.assertEqual(harness.rebuilt, [1, 2])
        self.assertTrue(harness.synced)
        self.assertTrue(harness.redrawn)
        self.assertTrue(harness.quote_status_refreshed)
        self.assertTrue(harness.cache_saved)

    def test_rank_selection_switches_to_matching_tab_and_cell(self) -> None:
        class _Tree:
            def selection(self) -> tuple[str, ...]:
                return ("row-1",)

        class _Harness:
            def __init__(self) -> None:
                self.rank_tree = _Tree()
                self.rank_item_cells = {"row-1": (2, (101.0, 1.0))}
                self.matrix_tabs = [_tab(1, "Tab 1", []), _tab(2, "Tab 2", [])]
                self.active_tab_id = 1
                self.synced = False
                self.tabs_redrawn = False
                self.redrawn = False

            def _sync_view_from_active_tab(self) -> None:
                self.synced = True

            def _redraw_matrix_tabs(self) -> None:
                self.tabs_redrawn = True

            def _redraw_all(self) -> None:
                self.redrawn = True

        harness = _Harness()

        OptionsMatrixApp._on_rank_selected(harness, None)

        self.assertEqual(harness.active_tab_id, 2)
        self.assertEqual(harness.matrix_tabs[1].selected_cell_key, (101.0, 1.0))
        self.assertTrue(harness.synced)
        self.assertTrue(harness.tabs_redrawn)
        self.assertTrue(harness.redrawn)

    def test_recorder_start_is_available_only_with_live_tabs(self) -> None:
        harness = SimpleNamespace(matrix_tabs=[SimpleNamespace(is_stale=True)])
        self.assertFalse(OptionsMatrixApp._has_live_matrix_tabs(harness))

        harness.matrix_tabs.append(SimpleNamespace(is_stale=False))
        self.assertTrue(OptionsMatrixApp._has_live_matrix_tabs(harness))

    def test_global_recorder_indicator_resolves_all_actual_states(self) -> None:
        off = resolve_recorder_indicator_state(running=False, start_inflight=False, armed=False)
        starting = resolve_recorder_indicator_state(running=False, start_inflight=True, armed=False)
        armed = resolve_recorder_indicator_state(running=False, start_inflight=False, armed=True)
        on = resolve_recorder_indicator_state(running=True, start_inflight=False, armed=False)
        error = resolve_recorder_indicator_state(
            running=True,
            start_inflight=False,
            armed=False,
            error="writer failed",
        )

        self.assertEqual((off.state, off.text), ("OFF", "\u25cb REC OFF"))
        self.assertEqual((starting.state, starting.text), ("STARTING", "\u25cf REC STARTING"))
        self.assertEqual((armed.state, armed.text), ("ARMED", "\u25cf REC ARMED"))
        self.assertEqual((on.state, on.text), ("ON", "\u25cf REC ON"))
        self.assertEqual((error.state, error.text), ("ERROR", "! REC ERROR"))

    def test_recorder_status_and_indicator_update_without_recording_panel(self) -> None:
        class _StringValue:
            def __init__(self, value: str = "") -> None:
                self.value = value

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        harness = SimpleNamespace(
            recorder=None,
            recorder_status_var=_StringValue("Recording."),
            recorder_indicator_var=_StringValue(),
        )
        OptionsMatrixApp._refresh_recorder_controls(harness)
        self.assertEqual(harness.recorder_status_var.get(), "Recorder idle.")
        self.assertEqual(harness.recorder_indicator_var.get(), "\u25cb REC OFF")

        status = RecorderStatus(
            running=True,
            session_id="remote-session",
            session_dir=Path("recorder-output/session"),
            current_part_path=None,
            rows_enqueued=20,
            rows_written=10,
            last_snapshot_rows=10,
            flush_complete=False,
        )
        harness.recorder = SimpleNamespace(running=True, status=lambda: status)
        OptionsMatrixApp._refresh_recorder_controls(harness)
        self.assertEqual(harness.recorder_status_var.get(), "Recording.")
        self.assertEqual(harness.recorder_indicator_var.get(), "\u25cf REC ON")

        status = replace(status, running=False, error="Recorder connection lost.")
        harness.recorder.running = False
        OptionsMatrixApp._refresh_recorder_controls(harness)
        self.assertEqual(harness.recorder_status_var.get(), "Recorder connection lost.")
        self.assertEqual(harness.recorder_indicator_var.get(), "! REC ERROR")

    def test_start_recorder_session_uses_local_writer_and_preserves_capture_time(self) -> None:
        started_at = datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc)
        market_close = datetime(2026, 6, 17, 20, 0, tzinfo=timezone.utc)
        session_status = SimpleNamespace(message="NYSE market is open.", market_close_utc=market_close)
        factory_calls = []
        start_calls = []
        status_messages = []
        timer_calls = []

        class _Recorder:
            def start(self, *, started_at: datetime) -> str:
                start_calls.append(started_at)
                return "companion-session"

        recorder = _Recorder()

        def create_recorder(output_root: Path, ticker_label: str):
            factory_calls.append((output_root, ticker_label))
            return recorder

        harness = SimpleNamespace(
            _create_recorder=create_recorder,
            recorder=None,
            recorder_status_var=SimpleNamespace(set=status_messages.append),
            _set_status=status_messages.append,
            _refresh_recorder_controls=lambda: None,
            _schedule_recorder_tick=lambda **kwargs: timer_calls.append(kwargs),
        )
        OptionsMatrixApp._start_recorder_session(harness, Path("."), "SPY", started_at, session_status)

        self.assertIs(harness.recorder, recorder)
        self.assertEqual(factory_calls, [(Path("."), "SPY")])
        self.assertEqual(start_calls, [started_at])
        self.assertEqual(timer_calls, [{"immediate": True}])
        self.assertEqual(harness._recorder_market_close_utc, market_close)
        self.assertFalse(harness._recorder_snapshot_inflight)
        self.assertTrue(any("companion-session" in message for message in status_messages))

    def test_finalize_close_disconnects_active_reconnecting_service(self) -> None:
        calls: list[str] = []

        class _Service:
            connected = False
            connection_active = True

            def disconnect(self) -> None:
                calls.append("disconnect")

        class _Root:
            def destroy(self) -> None:
                calls.append("destroy")

        harness = SimpleNamespace(
            _shutdown_finalized=False,
            _close_after_recorder_stop=True,
            service=_Service(),
            root=_Root(),
        )

        OptionsMatrixApp._finalize_close(harness)

        self.assertEqual(calls, ["disconnect", "destroy"])
        self.assertTrue(harness._shutdown_finalized)

    def test_start_recording_task_rejects_closed_market(self) -> None:
        class _Service:
            connected = True

            def request_current_time(self):
                return datetime(2026, 6, 17, 20, 0, tzinfo=timezone.utc)

        class _Guard:
            def status_at(self, _now: datetime):
                return SimpleNamespace(
                    is_open=False,
                    reason="after_close",
                    message="NYSE market closed at 2026-06-17 04:00 PM ET.",
                )

        class _Harness:
            service = _Service()

            def _build_market_session_guard(self):
                return _Guard()

        with self.assertRaisesRegex(RuntimeError, "market closed"):
            OptionsMatrixApp._start_recording_task(_Harness(), Path("recordings"))

    def test_start_recording_task_arms_before_open(self) -> None:
        market_open = datetime(2026, 6, 17, 13, 30, tzinfo=timezone.utc)

        class _Service:
            connected = True

            def request_current_time(self):
                return datetime(2026, 6, 17, 13, 0, tzinfo=timezone.utc)

        class _Guard:
            def status_at(self, _now: datetime):
                return SimpleNamespace(
                    is_open=False,
                    reason="pre_open",
                    message="NYSE market is not open yet.",
                    market_open_utc=market_open,
                    market_close_utc=datetime(2026, 6, 17, 20, 0, tzinfo=timezone.utc),
                )

        class _Harness:
            service = _Service()

            def _build_market_session_guard(self):
                return _Guard()

        result = OptionsMatrixApp._start_recording_task(_Harness(), Path("recordings"), "SPY")

        self.assertTrue(result.armed)
        self.assertEqual(result.session_status.market_open_utc, market_open)

    def test_armed_recorder_tick_waits_then_starts_at_open(self) -> None:
        class _StringValue:
            def __init__(self) -> None:
                self.value = ""

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        statuses = [
            SimpleNamespace(is_open=False, reason="pre_open", message="pre"),
            SimpleNamespace(is_open=True, reason="open", message="open"),
        ]

        class _Harness:
            _recorder_tick_armed = OptionsMatrixApp._recorder_tick_armed

            def __init__(self) -> None:
                self._recorder_armed = True
                self._recorder_armed_output_root = Path("recordings")
                self._recorder_armed_ticker_label = "SPY"
                self.recorder_status_var = _StringValue()
                self.scheduled = False
                self.refreshed = False
                self.started = None
                self.status = ""

            def _recorder_market_now(self) -> datetime:
                return datetime(2026, 6, 17, 13, 30, tzinfo=timezone.utc)

            def _recorder_session_status(self, _captured_at: datetime):
                return statuses.pop(0)

            def _schedule_recorder_tick(self, *, immediate: bool = False) -> None:
                self.scheduled = True

            def _refresh_recorder_controls(self) -> None:
                self.refreshed = True

            def _set_status(self, message: str) -> None:
                self.status = message

            def _start_recorder_session(self, output_root, ticker_label, started_at, session_status) -> None:
                self.started = (output_root, ticker_label, started_at, session_status.reason)

        harness = _Harness()

        harness._recorder_tick_armed()
        self.assertTrue(harness._recorder_armed)
        self.assertTrue(harness.scheduled)
        self.assertIsNone(harness.started)

        harness._recorder_tick_armed()
        self.assertFalse(harness._recorder_armed)
        self.assertEqual(harness.started[0], Path("recordings"))
        self.assertEqual(harness.started[1], "SPY")
        self.assertEqual(harness.started[3], "open")

    def test_stop_while_armed_disarms_recorder(self) -> None:
        class _StringValue:
            def __init__(self) -> None:
                self.value = "Armed - recording starts at open."

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        class _Harness:
            _stop_recording = OptionsMatrixApp._stop_recording
            _cancel_recorder_timer = OptionsMatrixApp._cancel_recorder_timer

            def __init__(self) -> None:
                self._recorder_after_id = None
                self._recorder_armed = True
                self.recorder = None
                self._recorder_start_inflight = False
                self._recorder_market_session_guard = object()
                self._recorder_market_time_offset = timedelta(seconds=1)
                self._recorder_market_status_message = "armed"
                self._recorder_market_close_utc = object()
                self.recorder_status_var = _StringValue()
                self.refreshed = False

            def _refresh_recorder_controls(self) -> None:
                self.refreshed = True

        harness = _Harness()

        harness._stop_recording()

        self.assertFalse(harness._recorder_armed)
        self.assertEqual(harness.recorder_status_var.get(), "Recorder idle.")
        self.assertTrue(harness.refreshed)

    def test_stop_recording_requests_nonblocking_flush_and_reports_accepted_rows(self) -> None:
        class _StringValue:
            def __init__(self) -> None:
                self.value = "Recording."

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        class _Root:
            def __init__(self) -> None:
                self.callbacks = []

            def after(self, _delay_ms: int, callback):
                self.callbacks.append(callback)
                return len(self.callbacks)

        class _Recorder:
            def __init__(self) -> None:
                self.stop_requested = 0
                self.wait_calls = 0
                self.flush_complete = False

            def request_stop(self) -> None:
                self.stop_requested += 1

            def wait_for_stop(self, *, timeout: float) -> bool:
                self.wait_calls += 1
                self.asserted_timeout = timeout
                return self.flush_complete

            def status(self) -> RecorderStatus:
                return RecorderStatus(
                    running=not self.flush_complete,
                    session_id="session-1",
                    session_dir=Path("recordings/session-1"),
                    current_part_path=Path("recordings/session-1/part-000001.parquet"),
                    rows_enqueued=7,
                    rows_written=7 if self.flush_complete else 3,
                    last_snapshot_rows=4,
                    stopping=True,
                    writer_alive=not self.flush_complete,
                    flush_complete=self.flush_complete,
                )

        class _Harness:
            _cancel_recorder_timer = OptionsMatrixApp._cancel_recorder_timer
            _poll_recorder_stop = OptionsMatrixApp._poll_recorder_stop

            def __init__(self) -> None:
                self.root = _Root()
                self.recorder = _Recorder()
                self._recorder_after_id = None
                self._recorder_next_deadline = 10.0
                self._recorder_armed = False
                self._recorder_start_inflight = False
                self._recorder_snapshot_inflight = False
                self._recorder_stop_inflight = False
                self._recorder_market_session_guard = object()
                self._recorder_market_time_offset = timedelta(0)
                self._recorder_market_status_message = "open"
                self._recorder_market_close_utc = None
                self._shutdown_in_progress = False
                self._close_after_recorder_stop = False
                self.recorder_status_var = _StringValue()
                self.status_message = ""
                self.status_overrides = []

            def _refresh_recorder_controls(self, *, status_override=None) -> None:
                self.status_overrides.append(status_override)

            def _set_status(self, message: str) -> None:
                self.status_message = message

        harness = _Harness()
        recorder = harness.recorder

        OptionsMatrixApp._stop_recording(harness)

        self.assertEqual(recorder.stop_requested, 1)
        self.assertEqual(recorder.wait_calls, 0)
        self.assertIs(harness.recorder, recorder)
        self.assertEqual(len(harness.root.callbacks), 1)
        self.assertIn("flushing accepted rows", harness.recorder_status_var.get())

        recorder.flush_complete = True
        harness.root.callbacks.pop(0)()

        self.assertEqual(recorder.wait_calls, 1)
        self.assertEqual(recorder.asserted_timeout, 0.0)
        self.assertIsNone(harness.recorder)
        self.assertEqual(harness.status_overrides[-1].rows_enqueued, 7)
        self.assertEqual(harness.status_overrides[-1].rows_written, 7)
        self.assertEqual(harness.status_message, "Recording stopped. Rows written: 7.")

    def test_recorder_tick_uses_fixed_deadline_and_hands_snapshot_build_to_worker(self) -> None:
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="101",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        plan = build_matrix_selection_plan(
            [_option(100.0), _option(101.0)],
            [100.0],
            [1.0],
            context_neighbors=0,
        )
        tab = MatrixTabState(
            tab_id=1,
            label=matrix_tab_label(matrix_input),
            matrix_input=matrix_input,
            row_strikes=(100.0,),
            matrix_plan=plan,
        )

        class _Recorder:
            running = True
            session_id = "session-1"

            def __init__(self) -> None:
                self.rows = None

            def record_rows(self, rows) -> None:
                self.rows = rows

        class _Builder:
            def __init__(self) -> None:
                self.calls = 0

            def build_rows(self, **_kwargs):
                self.calls += 1
                return [{"snapshot_id": "snapshot-1"}]

        class _Harness:
            def __init__(self) -> None:
                self._recorder_after_id = "old"
                self._recorder_next_deadline = 50.0
                self._recorder_armed = False
                self._recorder_snapshot_inflight = False
                self._recorder_stop_inflight = False
                self.recorder = _Recorder()
                self.snapshot_row_builder = _Builder()
                self.matrix_tabs = [tab]
                self.underlying_quote = None
                self.task = None
                self.scheduled = False
                self.refreshed = False

            def _recorder_market_now(self):
                return datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc)

            def _recorder_session_status(self, _captured_at):
                return None

            def _start_task(self, kind: str, task, *, announce: bool = True) -> None:
                self.task = (kind, task, announce)

            def _schedule_recorder_tick(self) -> None:
                self.scheduled = True

            def _refresh_recorder_controls(self) -> None:
                self.refreshed = True

            def _set_status(self, _message: str) -> None:
                return None

        harness = _Harness()

        OptionsMatrixApp._record_snapshot_tick(harness)

        self.assertEqual(harness._recorder_next_deadline, 51.0)
        self.assertTrue(harness._recorder_snapshot_inflight)
        self.assertEqual(harness.task[0], "record_snapshot")
        self.assertFalse(harness.task[2])
        self.assertEqual(harness.snapshot_row_builder.calls, 0)
        self.assertTrue(harness.scheduled)

        result = harness.task[1]()
        self.assertEqual(harness.snapshot_row_builder.calls, 1)
        OptionsMatrixApp._finish_record_snapshot(harness, result)

        self.assertEqual(harness.recorder.rows, [{"snapshot_id": "snapshot-1"}])
        self.assertFalse(harness._recorder_snapshot_inflight)
        self.assertTrue(harness.refreshed)

    def test_recenter_path_is_removed(self) -> None:
        self.assertFalse(hasattr(OptionsMatrixApp, "_recenter_active_matrix_tab"))

    def test_connection_status_text_warns_when_token_is_old(self) -> None:
        now = datetime(2026, 7, 7, tzinfo=timezone.utc)

        self.assertEqual(connection_status_text(False, None), "Not connected")
        self.assertEqual(connection_status_text(True, None), "Connected")
        self.assertEqual(
            connection_status_text(True, now - timedelta(days=2), now=now),
            "Connected",
        )
        self.assertEqual(
            connection_status_text(True, now - timedelta(days=5), now=now),
            "Connected - Schwab re-auth needed within 2 days",
        )
        self.assertEqual(
            connection_status_text(True, now - timedelta(days=6), now=now),
            "Connected - Schwab re-auth needed within 1 day",
        )
        self.assertEqual(
            connection_status_text(True, now - timedelta(days=8), now=now),
            "Connected - Schwab re-auth overdue (run schwab_auth/authorize_schwab.bat)",
        )

    def test_compact_connection_and_auth_badges_cover_each_lifecycle_state(self) -> None:
        now = datetime(2026, 7, 7, tzinfo=timezone.utc)

        self.assertEqual(compact_connection_status_text(False, False), "○ OFFLINE")
        self.assertEqual(
            compact_connection_status_text(False, True, connecting=True),
            "◐ CONNECTING",
        )
        self.assertEqual(
            compact_connection_status_text(True, True, disconnecting=True),
            "◐ DISCONNECTING",
        )
        self.assertEqual(compact_connection_status_text(True, True), "● LIVE")
        self.assertEqual(compact_connection_status_text(False, True), "↻ RETRY")
        retry_message = "Schwab streamer reconnecting in 2s (attempt 1/5) ..."
        self.assertEqual(
            compact_connection_status_text(False, True, message=retry_message),
            "↻ RETRY 1/5 · 2s",
        )
        self.assertTrue(is_routine_reconnect_schedule_message(retry_message))
        self.assertFalse(is_routine_reconnect_schedule_message("Socket closed (1006)."))
        self.assertEqual(
            compact_connection_status_text(
                False,
                True,
                message="Schwab streamer reconnect attempts exhausted. Use DISCONNECT/CONNECT to retry.",
            ),
            "! RETRY STOPPED",
        )

        self.assertEqual(compact_auth_status_text(None, now=now), "")
        self.assertEqual(
            compact_auth_status_text(now - timedelta(days=4), now=now),
            "",
        )
        self.assertEqual(
            compact_auth_status_text(now - timedelta(days=5), now=now),
            "⚠ AUTH 2d",
        )
        self.assertEqual(
            compact_auth_status_text(now - timedelta(days=6), now=now),
            "⚠ AUTH 1d",
        )
        self.assertEqual(
            compact_auth_status_text(now - timedelta(days=7), now=now),
            "⚠ AUTH DUE",
        )
        self.assertEqual(
            compact_auth_status_text(now + timedelta(days=1), now=now),
            "",
        )

    def test_status_display_is_single_line_and_ellipsized_without_losing_detail(self) -> None:
        class _StringValue:
            def __init__(self, value: str = "") -> None:
                self.value = value

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        harness = SimpleNamespace(
            status_var=_StringValue(),
            status_display_var=_StringValue(),
            _status_label_width=84,
        )

        full_message = "Socket closed on the first line.\nAutomatic recovery remains active."
        OptionsMatrixApp._set_status(harness, full_message)

        self.assertEqual(harness.status_var.get(), full_message)
        self.assertNotIn("\n", harness.status_display_var.get())
        self.assertLessEqual(len(harness.status_display_var.get()), 12)
        self.assertTrue(harness.status_display_var.get().endswith("…"))

        harness._status_label_width = 2000
        OptionsMatrixApp._update_status_display(harness)
        self.assertEqual(
            harness.status_display_var.get(),
            "Socket closed on the first line. Automatic recovery remains active.",
        )

        harness.grid_font = SimpleNamespace(measure=lambda text: len(text) * 10)
        harness._status_label_width = 58
        OptionsMatrixApp._set_status(harness, "abcdefghij")
        self.assertEqual(harness.status_display_var.get(), "abcd…")

    def test_record_snapshot_tick_auto_stops_when_market_closes(self) -> None:
        class _StringValue:
            def __init__(self) -> None:
                self.value = ""

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        class _Recorder:
            running = True
            session_id = "session-1"

            def __init__(self) -> None:
                self.stopped = False

            def stop(self) -> None:
                self.stopped = True
                self.running = False

            def status(self):
                return SimpleNamespace(
                    session_id="session-1",
                    current_part_path=None,
                    rows_enqueued=0,
                    rows_written=0,
                    last_snapshot_rows=0,
                )

        class _Guard:
            def status_at(self, _now: datetime):
                return SimpleNamespace(is_open=False, message="NYSE market closed at 2026-06-17 04:00 PM ET.")

        class _Harness:
            def __init__(self) -> None:
                self._recorder_after_id = None
                self._recorder_start_inflight = False
                self._recorder_market_session_guard = _Guard()
                self._recorder_market_time_offset = timedelta(0)
                self.recorder = _Recorder()
                self.recorder_status_var = _StringValue()
                self.recorder_session_var = _StringValue()
                self.recorder_part_var = _StringValue()
                self.recorder_rows_var = _StringValue()
                self.recorder_last_snapshot_var = _StringValue()
                self.status = ""

            def _set_status(self, message: str) -> None:
                self.status = message

            def _recorder_market_now(self) -> datetime:
                return OptionsMatrixApp._recorder_market_now(self)

            def _recorder_session_status(self, captured_at: datetime):
                return OptionsMatrixApp._recorder_session_status(self, captured_at)

            def _stop_recording(self, *, silent: bool = False) -> None:
                OptionsMatrixApp._stop_recording(self, silent=silent)

            def _cancel_recorder_timer(self) -> None:
                OptionsMatrixApp._cancel_recorder_timer(self)

            def _refresh_recorder_controls(self) -> None:
                OptionsMatrixApp._refresh_recorder_controls(self)

        harness = _Harness()
        recorder = harness.recorder

        OptionsMatrixApp._record_snapshot_tick(harness)

        self.assertTrue(recorder.stopped)
        self.assertIsNone(harness.recorder)
        self.assertIn("Auto-stopped recording", harness.status)
        self.assertIn("Auto-stopped recording", harness.recorder_status_var.get())

    def test_record_snapshot_tick_flattens_both_into_call_and_put_views(self) -> None:
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=MATRIX_VIEW_BOTH,
            low_strike="100",
            high_strike="101",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        empty_plan = build_matrix_selection_plan([], [], [])
        both_tab = MatrixTabState(
            tab_id=1,
            label=matrix_tab_label(matrix_input),
            matrix_input=matrix_input,
            row_strikes=(100.0, 101.0),
            matrix_plan=empty_plan,
            side_states={
                RIGHT_PUT: MatrixSideState(RIGHT_PUT, (100.0, 101.0), empty_plan),
                RIGHT_CALL: MatrixSideState(RIGHT_CALL, (100.0, 101.0), empty_plan),
            },
        )

        class _Recorder:
            running = True
            session_id = "session-1"

            def __init__(self) -> None:
                self.rows = None

            def record_rows(self, rows) -> None:
                self.rows = rows

        class _Builder:
            def __init__(self) -> None:
                self.tabs = None

            def build_rows(self, **kwargs):
                self.tabs = kwargs["tabs"]
                return []

        class _Harness:
            def __init__(self) -> None:
                self._recorder_after_id = None
                self._recorder_armed = False
                self.recorder = _Recorder()
                self.snapshot_row_builder = _Builder()
                self.matrix_tabs = [both_tab]
                self.underlying_quote = None
                self.refreshed = False
                self.scheduled = False

            def _recorder_market_now(self):
                return datetime(2026, 6, 17, 14, 45, tzinfo=timezone.utc)

            def _recorder_session_status(self, _captured_at):
                return None

            def _refresh_recorder_controls(self) -> None:
                self.refreshed = True

            def _schedule_recorder_tick(self) -> None:
                self.scheduled = True

        harness = _Harness()

        OptionsMatrixApp._record_snapshot_tick(harness)

        self.assertEqual(
            [tab.matrix_input.side for tab in harness.snapshot_row_builder.tabs],
            [RIGHT_PUT, RIGHT_CALL],
        )
        self.assertTrue(harness.refreshed)
        self.assertTrue(harness.scheduled)

    def test_all_five_width_bands_are_selectable_and_legacy_mode_is_behavior_free(self) -> None:
        self.assertEqual(MATRIX_WIDTH_BANDS, (1.0, 2.0, 3.0, 4.0, 5.0))
        self.assertEqual(DEFAULT_WIDTHS, MATRIX_WIDTH_BANDS)

        all_selected = {band: True for band in MATRIX_WIDTH_BANDS}
        for legacy_mode in (MATRIX_MODE_REGULAR, MATRIX_MODE_MODIFIED):
            with self.subTest(legacy_mode=legacy_mode):
                self.assertEqual(
                    selected_matrix_widths(all_selected, matrix_mode=legacy_mode),
                    list(MATRIX_WIDTH_BANDS),
                )

        selected = {band: band in {1.0, 3.0, 5.0} for band in MATRIX_WIDTH_BANDS}
        self.assertEqual(selected_matrix_widths(selected), [1.0, 3.0, 5.0])

    def test_fractional_actual_widths_use_ceil_bands_and_stop_at_five(self) -> None:
        band_examples = {
            0.0: None,
            0.25: 1.0,
            0.5: None,
            0.75: 1.0,
            1.0: 1.0,
            1.0001: 2.0,
            1.25: 2.0,
            1.5: None,
            2.0: 2.0,
            2.5: None,
            2.75: 3.0,
            3.5: None,
            4.0: 4.0,
            4.0001: 5.0,
            4.5: None,
            5.0: 5.0,
            5.0001: None,
        }
        self.assertEqual(
            {width: matrix_width_band(width) for width in band_examples},
            band_examples,
        )
        self.assertEqual(
            matrix_width_bands((0.5, 1.25, 2.75, 4.0, 4.25, 5.0, 5.5)),
            (2.0, 3.0, 4.0, 5.0),
        )

        row_strikes = (100.0, 100.5, 101.75, 104.5, 105.0, 106.0)
        discovered = discovered_matrix_widths(row_strikes)
        self.assertEqual(
            discovered,
            (1.0, 1.75, 3.25, 4.25, 5.0),
        )
        self.assertIn(5.0, discovered)
        self.assertNotIn(5.5, discovered)
        self.assertNotIn(6.0, discovered)
        self.assertEqual(
            discovered_matrix_widths(row_strikes, enabled_bands=(1.0, 3.0, 5.0)),
            (1.0, 4.25, 5.0),
        )

        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="106",
            width_states={band: band in {1.0, 3.0, 5.0} for band in MATRIX_WIDTH_BANDS},
            ticker_symbol="SPY",
        )
        input_plan = build_matrix_input_plan(
            [_option(strike) for strike in row_strikes],
            matrix_input,
        )

        self.assertIsNotNone(input_plan)
        self.assertEqual(input_plan.widths, (1.0, 4.25, 5.0))

    def test_matrix_input_state_snapshots_staged_values_until_submitted(self) -> None:
        applied = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="110",
            width_states={2.0: True, 3.0: False, 4.0: False, 5.0: True},
        )
        later_staged = parse_matrix_input_state(
            display_expiration="2030-02-17",
            raw_expiration="20300217",
            side=RIGHT_PUT,
            low_strike="101",
            high_strike="106",
            width_states={2.0: False, 3.0: True, 4.0: False, 5.0: False},
        )

        self.assertEqual(applied.raw_expiration, "20300117")
        self.assertEqual(applied.side, RIGHT_CALL)
        self.assertEqual(applied.low_strike, 100.0)
        self.assertEqual(applied.high_strike, 110.0)
        self.assertEqual(applied.widths, (1.0, 2.0, 5.0))
        self.assertEqual(later_staged.widths, (1.0, 3.0))
        self.assertEqual(applied.widths, (1.0, 2.0, 5.0))

        legacy_modified = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="110",
            width_states={2.0: True, 3.0: True, 4.0: True, 5.0: True},
            matrix_mode=MATRIX_MODE_MODIFIED,
        )
        self.assertEqual(legacy_modified.matrix_mode, DEFAULT_MATRIX_MODE)
        self.assertEqual(legacy_modified.widths, MATRIX_WIDTH_BANDS)

    def test_both_is_a_matrix_view_and_expands_to_real_option_rights(self) -> None:
        self.assertEqual(MATRIX_VIEWS, (RIGHT_CALL, RIGHT_PUT, MATRIX_VIEW_BOTH))
        self.assertEqual(normalize_matrix_view("both"), MATRIX_VIEW_BOTH)
        self.assertEqual(matrix_view_rights(MATRIX_VIEW_BOTH), (RIGHT_PUT, RIGHT_CALL))
        self.assertEqual(matrix_view_rights(RIGHT_CALL), (RIGHT_CALL,))

    def test_build_both_matrix_view_plan_keeps_call_and_put_plans_independent(self) -> None:
        strikes = [float(strike) for strike in range(99, 107)]
        records = [
            *[_anchor_call_option(strike) for strike in strikes],
            *[_option(strike, RIGHT_PUT) for strike in strikes],
        ]
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=MATRIX_VIEW_BOTH,
            low_strike="100",
            high_strike="105",
            width_states={2.0: True, 3.0: True, 4.0: True, 5.0: True},
            ticker_symbol="SPY",
            matrix_mode=MATRIX_MODE_MODIFIED,
        )

        view_plan = build_matrix_view_plan(records, matrix_input)

        self.assertIsNotNone(view_plan)
        self.assertEqual(set(view_plan.side_plans), {RIGHT_PUT, RIGHT_CALL})
        self.assertEqual(view_plan.row_strikes, tuple(float(strike) for strike in range(100, 106)))
        self.assertEqual(view_plan.widths, (1.0, 2.0, 3.0, 4.0, 5.0))
        for right, side_plan in view_plan.side_plans.items():
            self.assertTrue(side_plan.plan.visible)
            self.assertEqual({selection.pair.right for selection in side_plan.plan.visible.values()}, {right})
        self.assertIsNot(
            view_plan.side_plans[RIGHT_PUT].plan,
            view_plan.side_plans[RIGHT_CALL].plan,
        )

        call_only = build_matrix_view_plan(
            [record for record in records if record.right == RIGHT_CALL],
            matrix_input,
        )
        self.assertIsNone(call_only)

    def test_asymmetric_both_view_unions_each_right_without_fabricating_cross_right_widths(self) -> None:
        call_records = [_anchor_call_option(strike) for strike in (100.25, 101.25)]
        put_records = [_option(strike, RIGHT_PUT) for strike in (102.0, 104.75)]
        records = [*call_records, *put_records]

        self.assertEqual(
            discovered_matrix_view_widths(
                records,
                MATRIX_VIEW_BOTH,
                100.0,
                104.75,
            ),
            (1.0, 2.75),
        )

        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=MATRIX_VIEW_BOTH,
            low_strike="100",
            high_strike="104.75",
            width_states={band: True for band in MATRIX_WIDTH_BANDS},
            ticker_symbol="SPY",
        )
        view_plan = build_matrix_view_plan(records, matrix_input)

        self.assertIsNotNone(view_plan)
        self.assertEqual(view_plan.widths, (1.0, 2.75))
        self.assertEqual(view_plan.side_plans[RIGHT_CALL].widths, (1.0,))
        self.assertEqual(view_plan.side_plans[RIGHT_PUT].widths, (2.75,))
        self.assertNotIn(1.75, view_plan.widths)
        self.assertNotIn(2.25, view_plan.widths)

    def test_apply_both_matrix_creates_one_graphical_tab_with_two_live_sides(self) -> None:
        class _Harness:
            _find_matrix_tab = OptionsMatrixApp._find_matrix_tab
            _rebuild_tab_from_quotes = OptionsMatrixApp._rebuild_tab_from_quotes
            _ensure_tab_selected_cell = OptionsMatrixApp._ensure_tab_selected_cell

            def __init__(self) -> None:
                self.matrix_tabs = []
                self.active_tab_id = None
                self._next_tab_id = 1
                self.leg_quote_cache = {}
                self.underlying_quote = None
                self.search_result = None
                self.formula_settings = DEFAULT_FORMULA_SETTINGS
                self.signal_method = DEFAULT_SIGNAL_METHOD
                self.status = ""

            def _reject_matrix_tab_request(self, _matrix_input) -> bool:
                return False

            def _set_status(self, message: str) -> None:
                self.status = message

            def _sync_view_from_active_tab(self) -> None:
                return

            def _redraw_matrix_tabs(self) -> None:
                return

            def _redraw_all(self) -> None:
                return

            def _save_current_matrix_cache(self) -> None:
                return

            def _sync_quote_subscriptions(self, *, force: bool = False) -> None:
                return

        strikes = [float(strike) for strike in range(100, 107)]
        records = [
            *[_anchor_call_option(strike) for strike in strikes],
            *[_option(strike, RIGHT_PUT) for strike in strikes],
        ]
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=MATRIX_VIEW_BOTH,
            low_strike="100",
            high_strike="105",
            width_states={2.0: True, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
            matrix_mode=MATRIX_MODE_REGULAR,
        )
        harness = _Harness()

        OptionsMatrixApp._apply_matrix_input(harness, matrix_input, records)

        self.assertEqual(len(harness.matrix_tabs), 1)
        tab = harness.matrix_tabs[0]
        self.assertEqual(tab.matrix_input.side, MATRIX_VIEW_BOTH)
        self.assertEqual(set(tab.side_states), {RIGHT_PUT, RIGHT_CALL})
        self.assertFalse(tab.matrix_plan.visible)
        self.assertTrue(tab.side_states[RIGHT_PUT].matrix_plan.visible)
        self.assertTrue(tab.side_states[RIGHT_CALL].matrix_plan.visible)
        self.assertEqual(tab.matrix_input.widths, (1.0, 2.0))
        self.assertEqual(len(flatten_matrix_tabs_for_recording(harness.matrix_tabs)), 2)
        self.assertEqual(len(tab.selected_cell_key), 3)
        self.assertIn(tab.selected_cell_key[0], {RIGHT_PUT, RIGHT_CALL})
        self.assertEqual(
            set(tab.side_states[RIGHT_PUT].matrix_snapshots),
            set(tab.side_states[RIGHT_PUT].matrix_plan.visible),
        )
        self.assertEqual(
            set(tab.side_states[RIGHT_CALL].matrix_snapshots),
            set(tab.side_states[RIGHT_CALL].matrix_plan.visible),
        )

    def test_both_tab_chain_hint_builds_call_and_put_fair_values_before_live_spot(self) -> None:
        # Two same-side context intervals beyond each selected edge give both
        # independent curves enough post-LOO support around spot 103.5.
        strikes = [float(strike) for strike in range(98, 112)]
        row_strikes = tuple(float(strike) for strike in range(100, 109))
        records = [
            *[_anchor_call_option(strike) for strike in strikes],
            *[_option(strike, RIGHT_PUT) for strike in strikes],
        ]
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=MATRIX_VIEW_BOTH,
            low_strike="100",
            high_strike="108",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
            matrix_mode=MATRIX_MODE_REGULAR,
        )
        call_plan = build_matrix_input_plan(records, replace(matrix_input, side=RIGHT_CALL))
        put_plan = build_matrix_input_plan(records, replace(matrix_input, side=RIGHT_PUT))
        self.assertIsNotNone(call_plan)
        self.assertIsNotNone(put_plan)

        adjacent_mids = {
            pair_key_from_strikes(lower, lower + 1.0): 0.40
            for lower in strikes[:-1]
        }
        base_call_quotes = _chain_leg_quotes(
            strikes,
            adjacent_mids,
            top_leg_mid=0.10,
        )
        call_quotes = {
            900000 + con_id: replace(
                quote,
                label=str(900000 + con_id),
                contract_con_id=900000 + con_id,
            )
            for con_id, quote in base_call_quotes.items()
        }
        put_quotes = _put_chain_leg_quotes(
            strikes,
            adjacent_mids,
            bottom_leg_mid=0.10,
        )
        tab = MatrixTabState(
            tab_id=1,
            label="Hinted BOTH",
            matrix_input=matrix_input,
            row_strikes=row_strikes,
            matrix_plan=build_matrix_selection_plan([], [], []),
            leg_quotes={**call_quotes, **put_quotes},
            side_states={
                RIGHT_CALL: MatrixSideState(RIGHT_CALL, call_plan.row_strikes, call_plan.plan),
                RIGHT_PUT: MatrixSideState(RIGHT_PUT, put_plan.row_strikes, put_plan.plan),
            },
            underlying_price_hint=103.5,
        )

        class _Harness:
            underlying_quote = None
            formula_settings = DEFAULT_FORMULA_SETTINGS
            signal_method = DEFAULT_SIGNAL_METHOD
            _ensure_tab_selected_cell = OptionsMatrixApp._ensure_tab_selected_cell

        OptionsMatrixApp._rebuild_tab_from_quotes(_Harness(), tab)

        for right in (RIGHT_CALL, RIGHT_PUT):
            snapshots = tab.side_states[right].matrix_snapshots
            self.assertTrue(snapshots)
            self.assertTrue(
                all(
                    snapshot.fair_value_edge_curve_fit_points >= 5
                    and snapshot.fair_value_edge_curve_mid is not None
                    for snapshot in snapshots.values()
                )
            )

    def test_both_tab_flattens_to_side_specific_recorder_views(self) -> None:
        put_records = [_option(float(strike), RIGHT_PUT) for strike in range(100, 104)]
        call_records = [_anchor_call_option(float(strike)) for strike in range(100, 104)]
        both_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=MATRIX_VIEW_BOTH,
            low_strike="100",
            high_strike="103",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
            matrix_mode=MATRIX_MODE_REGULAR,
        )
        put_plan = build_matrix_input_plan(put_records, replace(both_input, side=RIGHT_PUT))
        call_plan = build_matrix_input_plan(call_records, replace(both_input, side=RIGHT_CALL))
        shared_quotes: dict[int, QuoteSnapshot] = {}
        tab = MatrixTabState(
            tab_id=7,
            label=matrix_tab_label(both_input),
            matrix_input=both_input,
            row_strikes=(100.0, 101.0, 102.0, 103.0),
            matrix_plan=build_matrix_selection_plan([], [], []),
            leg_quotes=shared_quotes,
            side_states={
                RIGHT_PUT: MatrixSideState(RIGHT_PUT, put_plan.row_strikes, put_plan.plan),
                RIGHT_CALL: MatrixSideState(RIGHT_CALL, call_plan.row_strikes, call_plan.plan),
            },
        )

        views = flatten_matrix_tabs_for_recording([tab])

        self.assertEqual([view.matrix_input.side for view in views], [RIGHT_PUT, RIGHT_CALL])
        self.assertNotIn(MATRIX_VIEW_BOTH, [view.matrix_input.side for view in views])
        self.assertIs(views[0].matrix_plan, put_plan.plan)
        self.assertIs(views[1].matrix_plan, call_plan.plan)
        self.assertIs(views[0].leg_quotes, shared_quotes)
        self.assertIs(views[1].leg_quotes, shared_quotes)

    def test_matrix_tab_label_and_key_use_expiry_side_and_strike_range(self) -> None:
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="110",
            width_states={2.0: True, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="spy",
        )
        put_input = parse_matrix_input_state(
            display_expiration="2030-02-05",
            raw_expiration="20300205",
            side=RIGHT_PUT,
            low_strike="101.5",
            high_strike="106",
            width_states={2.0: False, 3.0: True, 4.0: False, 5.0: False},
        )

        self.assertEqual(matrix_tab_label(matrix_input), "Jan17'30 C 100-110")
        self.assertEqual(matrix_tab_label(put_input), "Feb05'30 P 101.5-106")
        self.assertEqual(matrix_tab_key(matrix_input), ("20300117", RIGHT_CALL, 100.0, 110.0))
        self.assertEqual(matrix_tab_identity(matrix_input), ("SPY", "20300117", RIGHT_CALL))

    def test_matrix_tab_identity_ignores_strike_range_widths_and_threshold(self) -> None:
        existing = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="110",
            width_states={2.0: True, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        same_tab_with_different_filters = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="101",
            high_strike="109",
            width_states={2.0: False, 3.0: True, 4.0: True, 5.0: False},
            ticker_symbol="spy",
        )
        separate_put_tab = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_PUT,
            low_strike="101",
            high_strike="110",
            width_states={2.0: False, 3.0: True, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )

        self.assertTrue(has_duplicate_matrix_tab([existing], same_tab_with_different_filters))
        self.assertFalse(has_duplicate_matrix_tab([existing], separate_put_tab))

    def test_both_view_conflicts_with_overlapping_single_right_tabs(self) -> None:
        def matrix_input(side: str, *, expiration: str = "20300117"):
            return parse_matrix_input_state(
                display_expiration="2030-01-17",
                raw_expiration=expiration,
                side=side,
                low_strike="100",
                high_strike="105",
                width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
                ticker_symbol="SPY",
            )

        call = matrix_input(RIGHT_CALL)
        put = matrix_input(RIGHT_PUT)
        both = matrix_input(MATRIX_VIEW_BOTH)

        self.assertFalse(matrix_tab_views_conflict(call, put))
        self.assertTrue(matrix_tab_views_conflict(call, both))
        self.assertTrue(matrix_tab_views_conflict(put, both))
        self.assertFalse(matrix_tab_views_conflict(both, both))
        self.assertFalse(matrix_tab_views_conflict(call, matrix_input(MATRIX_VIEW_BOTH, expiration="20300217")))

    def test_tab_request_rejects_overlapping_both_and_single_view(self) -> None:
        call_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="105",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        both_input = replace(call_input, side=MATRIX_VIEW_BOTH)
        call_tab = MatrixTabState(
            tab_id=1,
            label=matrix_tab_label(call_input),
            matrix_input=call_input,
            row_strikes=(),
            matrix_plan=build_matrix_selection_plan([], [], []),
        )

        class _Harness:
            def __init__(self) -> None:
                self.matrix_tabs = [call_tab]
                self.error = ""

            def _find_matrix_tab(self, matrix_input):
                return OptionsMatrixApp._find_matrix_tab(self, matrix_input)

            def _show_matrix_tab_error(self, message: str) -> None:
                self.error = message

        harness = _Harness()

        rejected = OptionsMatrixApp._reject_matrix_tab_request(harness, both_input)

        self.assertTrue(rejected)
        self.assertIn("overlaps", harness.error)

    def test_cache_rejects_both_and_overlapping_single_view_for_same_expiration(self) -> None:
        def cached_tab(side: str) -> dict[str, object]:
            return {
                "raw_expiration": "20300117",
                "view_type": side,
                "low_strike": 100.0,
                "high_strike": 105.0,
                "widths": [1.0],
            }

        workspace = matrix_workspace_from_mapping(
            {
                "version": 1,
                "matrix_mode": MATRIX_MODE_REGULAR,
                "ticker_symbol": "SPY",
                "tabs": [cached_tab(MATRIX_VIEW_BOTH), cached_tab(RIGHT_CALL)],
            }
        )

        self.assertIsNone(workspace)
        for duplicated_view in (RIGHT_CALL, RIGHT_PUT, MATRIX_VIEW_BOTH):
            with self.subTest(duplicated_view=duplicated_view):
                self.assertIsNone(
                    matrix_workspace_from_mapping(
                        {
                            "version": 1,
                            "matrix_mode": MATRIX_MODE_REGULAR,
                            "ticker_symbol": "SPY",
                            "tabs": [cached_tab(duplicated_view), cached_tab(duplicated_view)],
                        }
                    )
                )

    def test_unified_call_matrix_includes_all_moneyness_states_through_width_five(self) -> None:
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="107",
            width_states={band: True for band in MATRIX_WIDTH_BANDS},
            ticker_symbol="SPY",
        )
        contracts = [_option(float(strike), RIGHT_CALL) for strike in range(100, 108)]

        input_plan = build_matrix_input_plan(contracts, matrix_input, underlying_price=101.0)

        self.assertIsNotNone(input_plan)
        self.assertEqual(MAX_MATRIX_STRIKE_WIDTH, 5.0)
        self.assertEqual(input_plan.widths, (1.0, 2.0, 3.0, 4.0, 5.0))
        self.assertIn((100.0, 1.0), input_plan.plan.visible)  # ITM at the supplied spot
        self.assertIn((101.0, 1.0), input_plan.plan.visible)  # ATM boundary
        self.assertIn((106.0, 1.0), input_plan.plan.visible)  # OTM
        self.assertIn((102.0, 5.0), input_plan.plan.visible)
        self.assertNotIn((103.0, 5.0), input_plan.plan.visible)  # upper leg would leave the range
        self.assertNotIn((101.0, 6.0), input_plan.plan.visible)

    def test_unified_put_matrix_includes_all_moneyness_states_through_width_five(self) -> None:
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_PUT,
            low_strike="100",
            high_strike="107",
            width_states={band: True for band in MATRIX_WIDTH_BANDS},
            ticker_symbol="SPY",
        )
        contracts = [_option(float(strike), RIGHT_PUT) for strike in range(100, 108)]

        input_plan = build_matrix_input_plan(contracts, matrix_input, underlying_price=106.0)

        self.assertIsNotNone(input_plan)
        self.assertEqual(input_plan.widths, (1.0, 2.0, 3.0, 4.0, 5.0))
        self.assertIn((100.0, 1.0), input_plan.plan.visible)  # OTM
        self.assertIn((105.0, 1.0), input_plan.plan.visible)  # ATM boundary
        self.assertIn((106.0, 1.0), input_plan.plan.visible)  # ITM at the supplied spot
        self.assertIn((102.0, 5.0), input_plan.plan.visible)
        self.assertNotIn((103.0, 5.0), input_plan.plan.visible)
        self.assertNotIn((100.0, 6.0), input_plan.plan.visible)

    def test_unified_matrix_plan_does_not_require_underlying_price(self) -> None:
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="101",
            high_strike="107",
            width_states={band: True for band in MATRIX_WIDTH_BANDS},
            ticker_symbol="SPY",
        )
        contracts = [_option(float(strike), RIGHT_CALL) for strike in range(101, 108)]

        input_plan = build_matrix_input_plan(contracts, matrix_input, underlying_price=None)

        self.assertIsNotNone(input_plan)
        self.assertEqual(input_plan.widths, (1.0, 2.0, 3.0, 4.0, 5.0))
        self.assertEqual(discovered_matrix_widths([101.0, 102.0, 103.0, 107.0]), (1.0, 2.0, 4.0, 5.0))

    def test_unified_matrix_requires_both_legs_inside_selected_range(self) -> None:
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="101",
            high_strike="103",
            width_states={1.0: True, 2.0: True, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        contracts = [_option(float(strike), RIGHT_CALL) for strike in range(99, 106)]

        input_plan = build_matrix_input_plan(contracts, matrix_input)

        self.assertIsNotNone(input_plan)
        self.assertEqual(
            set(input_plan.plan.visible),
            {(101.0, 1.0), (102.0, 1.0), (101.0, 2.0)},
        )
        self.assertNotIn((103.0, 1.0), input_plan.plan.visible)

    def test_matrix_load_rejection_clearly_reports_no_complete_spread(self) -> None:
        class _Harness:
            def __init__(self) -> None:
                self.matrix_tabs = []
                self.underlying_quote = None
                self.search_result = None
                self.status = ""

            def _find_matrix_tab(self, _matrix_input):
                return None

            def _reject_matrix_tab_request(self, _matrix_input) -> bool:
                return False

            def _set_status(self, message: str) -> None:
                self.status = message

        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="100",
            width_states={band: True for band in MATRIX_WIDTH_BANDS},
            ticker_symbol="SPY",
        )
        harness = _Harness()

        OptionsMatrixApp._apply_matrix_input(
            harness,
            matrix_input,
            [_option(100.0)],
        )

        self.assertEqual(
            harness.status,
            "No complete spreads are available in the enabled width bands and selected range.",
        )

    def test_call_and_put_plans_subscribe_only_their_own_right(self) -> None:
        strikes = [float(strike) for strike in range(90, 116)]
        records = [
            *[_anchor_call_option(strike) for strike in strikes],
            *[_option(strike, RIGHT_PUT) for strike in strikes],
        ]

        def matrix_input(side: str) -> object:
            return parse_matrix_input_state(
                display_expiration="2030-01-17",
                raw_expiration="20300117",
                side=side,
                low_strike="100",
                high_strike="105",
                width_states={band: True for band in MATRIX_WIDTH_BANDS},
                ticker_symbol="SPY",
            )

        call_plan = build_matrix_input_plan(records, matrix_input(RIGHT_CALL), underlying_price=None)
        put_plan = build_matrix_input_plan(records, matrix_input(RIGHT_PUT), underlying_price=None)

        self.assertIsNotNone(call_plan)
        self.assertIsNotNone(put_plan)
        call_targets = matrix_quote_target_selections(call_plan.plan)
        put_targets = matrix_quote_target_selections(put_plan.plan)
        self.assertTrue(call_targets)
        self.assertTrue(put_targets)
        self.assertEqual({selection.pair.right for selection in call_targets}, {RIGHT_CALL})
        self.assertEqual({selection.pair.right for selection in put_targets}, {RIGHT_PUT})

        call_record_ids = {record.con_id for record in records if record.right == RIGHT_CALL}
        put_record_ids = {record.con_id for record in records if record.right == RIGHT_PUT}
        call_target_ids = leg_quote_ids(call_targets)
        put_target_ids = leg_quote_ids(put_targets)
        self.assertTrue(call_target_ids <= call_record_ids)
        self.assertTrue(put_target_ids <= put_record_ids)
        self.assertTrue(call_target_ids.isdisjoint(put_record_ids))
        self.assertTrue(put_target_ids.isdisjoint(call_record_ids))

    def test_apply_unified_matrix_builds_immediately_without_underlying_price(self) -> None:
        class _Harness:
            _find_matrix_tab = OptionsMatrixApp._find_matrix_tab

            def __init__(self) -> None:
                self.matrix_tabs = []
                self.active_tab_id = None
                self.leg_quote_cache = {}
                self.underlying_quote = None
                self.search_result = object()
                self.service = SimpleNamespace(
                    chain_underlying_price=lambda _search_result, _expiration: None,
                )
                self._next_tab_id = 1
                self.status = ""

            def _reject_matrix_tab_request(self, matrix_input) -> bool:
                return False

            def _set_status(self, message: str) -> None:
                self.status = message

            def _rebuild_tab_from_quotes(self, tab) -> None:
                return

            def _sync_view_from_active_tab(self) -> None:
                return

            def _redraw_matrix_tabs(self) -> None:
                return

            def _redraw_all(self) -> None:
                return

            def _save_current_matrix_cache(self) -> None:
                return

            def _sync_quote_subscriptions(self, *, force: bool = False) -> None:
                return

        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="101",
            high_strike="107",
            width_states={band: True for band in MATRIX_WIDTH_BANDS},
            ticker_symbol="SPY",
        )
        contracts = [_option(float(strike), RIGHT_CALL) for strike in range(101, 108)]
        harness = _Harness()

        OptionsMatrixApp._apply_matrix_input(harness, matrix_input, contracts)

        self.assertEqual(len(harness.matrix_tabs), 1)
        self.assertTrue(harness.matrix_tabs[0].matrix_plan.visible)
        self.assertNotIn("Waiting", harness.status)

    def test_chain_price_does_not_change_unified_visible_membership(self) -> None:
        class _Harness:
            _find_matrix_tab = OptionsMatrixApp._find_matrix_tab

            def __init__(self) -> None:
                self.matrix_tabs = []
                self.active_tab_id = None
                self.leg_quote_cache = {}
                self.underlying_quote = None
                self.search_result = object()
                self.service = SimpleNamespace(
                    chain_underlying_price=lambda _search_result, _expiration: 104.0,
                )
                self._next_tab_id = 1
                self.status = ""

            def _reject_matrix_tab_request(self, matrix_input) -> bool:
                return False

            def _set_status(self, message: str) -> None:
                self.status = message

            def _rebuild_tab_from_quotes(self, tab) -> None:
                return

            def _sync_view_from_active_tab(self) -> None:
                return

            def _redraw_matrix_tabs(self) -> None:
                return

            def _redraw_all(self) -> None:
                return

            def _save_current_matrix_cache(self) -> None:
                return

            def _sync_quote_subscriptions(self, *, force: bool = False) -> None:
                return

        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="101",
            high_strike="107",
            width_states={band: True for band in MATRIX_WIDTH_BANDS},
            ticker_symbol="SPY",
        )
        contracts = [_option(float(strike), RIGHT_CALL) for strike in range(101, 108)]
        harness = _Harness()

        OptionsMatrixApp._apply_matrix_input(harness, matrix_input, contracts)

        self.assertEqual(len(harness.matrix_tabs), 1)
        self.assertEqual(
            set(harness.matrix_tabs[0].matrix_plan.visible),
            set(build_matrix_input_plan(contracts, matrix_input, underlying_price=None).plan.visible),
        )

    def test_unified_staged_quote_status_is_available_without_underlying(self) -> None:
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="107",
            width_states={band: True for band in MATRIX_WIDTH_BANDS},
            ticker_symbol="SPY",
        )
        contracts = [_option(float(strike), RIGHT_CALL) for strike in range(100, 108)]

        class _Harness:
            _refresh_staged_market_data_quote_status = OptionsMatrixApp._refresh_staged_market_data_quote_status

            def __init__(self) -> None:
                self.matrix_tabs = []
                self.option_records_by_expiration = {"20300117": contracts}
                self.underlying_quote = None
                self.search_result = object()
                self.service = SimpleNamespace(chain_underlying_price=lambda *_args: None)
                self.market_status = ""

            def _read_matrix_input(self):
                return matrix_input

            def _find_matrix_tab(self, _matrix_input):
                return None

            def _set_market_data_quote_status(self, status) -> None:
                self.market_status = format_market_data_quote_status(status)

        harness = _Harness()

        OptionsMatrixApp._refresh_staged_market_data_quote_status(harness)

        self.assertIn(
            "If loaded: 8 option streams, 25 synthetic spreads (+8 streams +25 spreads)",
            harness.market_status,
        )

    def test_matrix_tab_limit_blocks_fourth_tab(self) -> None:
        self.assertEqual(MAX_MATRIX_TABS, 3)
        self.assertFalse(matrix_tab_limit_reached(MAX_MATRIX_TABS - 1))
        self.assertTrue(matrix_tab_limit_reached(MAX_MATRIX_TABS))
        self.assertTrue(matrix_tab_limit_reached(MAX_MATRIX_TABS + 1))

    def test_load_matrix_updates_existing_identity_tab_even_at_tab_limit(self) -> None:
        def tab_from_input(tab_id: int, matrix_input) -> MatrixTabState:
            return MatrixTabState(
                tab_id=tab_id,
                label=matrix_tab_label(matrix_input),
                matrix_input=matrix_input,
                row_strikes=(),
                matrix_plan=build_matrix_selection_plan([], [], []),
            )

        class _Harness:
            def __init__(self, tabs: list[MatrixTabState]) -> None:
                self.matrix_tabs = tabs
                self.active_tab_id = tabs[0].tab_id
                self.leg_quote_cache = {}
                self.underlying_quote = None
                self.search_result = None
                self._next_tab_id = 4
                self.rebuilt: list[int] = []
                self.synced = False
                self.tabs_redrawn = False
                self.redrawn = False
                self.subscriptions_synced = False
                self.cache_saved = False
                self.status = ""

            def _find_matrix_tab(self, matrix_input):
                return OptionsMatrixApp._find_matrix_tab(self, matrix_input)

            def _reject_matrix_tab_request(self, matrix_input):
                return OptionsMatrixApp._reject_matrix_tab_request(self, matrix_input)

            def _show_matrix_tab_error(self, message: str) -> None:
                self.status = message

            def _rebuild_tab_from_quotes(self, tab: MatrixTabState) -> None:
                self.rebuilt.append(tab.tab_id)

            def _sync_view_from_active_tab(self) -> None:
                self.synced = True

            def _redraw_matrix_tabs(self) -> None:
                self.tabs_redrawn = True

            def _redraw_all(self) -> None:
                self.redrawn = True

            def _set_status(self, message: str) -> None:
                self.status = message

            def _sync_quote_subscriptions(self) -> None:
                self.subscriptions_synced = True

            def _save_current_matrix_cache(self) -> None:
                self.cache_saved = True

        existing_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="104",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        replacement_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="101",
            high_strike="106",
            width_states={2.0: True, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        separate_put_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_PUT,
            low_strike="100",
            high_strike="104",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        separate_expiry_input = parse_matrix_input_state(
            display_expiration="2030-02-17",
            raw_expiration="20300217",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="104",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        harness = _Harness(
            [
                tab_from_input(1, existing_input),
                tab_from_input(2, separate_put_input),
                tab_from_input(3, separate_expiry_input),
            ]
        )
        option_records = [_option(strike) for strike in range(100, 108)]

        OptionsMatrixApp._apply_matrix_input(harness, replacement_input, option_records)

        self.assertEqual(len(harness.matrix_tabs), MAX_MATRIX_TABS)
        self.assertEqual(harness.active_tab_id, 1)
        self.assertEqual(harness.matrix_tabs[0].matrix_input, replacement_input)
        self.assertEqual(harness.matrix_tabs[0].row_strikes, (101.0, 102.0, 103.0, 104.0, 105.0, 106.0))
        self.assertEqual(harness.matrix_tabs[0].label, "Jan17'30 C 101-106")
        self.assertEqual(harness.rebuilt, [1])
        self.assertTrue(harness.synced)
        self.assertTrue(harness.tabs_redrawn)
        self.assertTrue(harness.redrawn)
        self.assertTrue(harness.subscriptions_synced)
        self.assertTrue(harness.cache_saved)

    def test_manual_ticker_load_clears_cached_matrix_workspace(self) -> None:
        class _StringValue:
            def __init__(self, value: str) -> None:
                self.value = value

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        class _Harness:
            def __init__(self) -> None:
                self.ticker_var = _StringValue("aapl")
                self.matrix_tabs = []
                self.service = SimpleNamespace(connected=True)
                self._connect_inflight = False
                self._disconnect_inflight = False
                self.cache_cleared = False
                self.loaded_chain_cleared = False
                self.started_kind = ""

            def _clear_cached_matrix_workspace(self) -> None:
                self.cache_cleared = True

            def _clear_loaded_chain(self, *, clear_matrix: bool = True) -> None:
                self.loaded_chain_cleared = True

            def _start_task(self, kind: str, func) -> None:
                self.started_kind = kind

            def _set_status(self, message: str) -> None:
                self.status = message

        harness = _Harness()

        OptionsMatrixApp._load_ticker(harness)

        self.assertEqual(harness.ticker_var.get(), "AAPL")
        self.assertTrue(harness.cache_cleared)
        self.assertTrue(harness.loaded_chain_cleared)
        self.assertEqual(harness.started_kind, "load_ticker")

    def test_ticker_load_during_reconnect_preserves_current_workspace(self) -> None:
        class _StringValue:
            def __init__(self, value: str) -> None:
                self.value = value

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        class _Harness:
            def __init__(self) -> None:
                self.ticker_var = _StringValue("aapl")
                self.service = SimpleNamespace(connected=False)
                self._connect_inflight = False
                self._disconnect_inflight = False
                self._connection_active = True
                self.cache_cleared = False
                self.chain_cleared = False
                self.started = False
                self.connected_states: list[bool] = []
                self.status = ""

            def _clear_cached_matrix_workspace(self) -> None:
                self.cache_cleared = True

            def _clear_loaded_chain(self, *, clear_matrix: bool = True) -> None:
                self.chain_cleared = True

            def _start_task(self, _kind: str, _func) -> None:
                self.started = True

            def _set_connected_state(self, connected: bool) -> None:
                self.connected_states.append(connected)

            def _set_status(self, message: str) -> None:
                self.status = message

        harness = _Harness()

        OptionsMatrixApp._load_ticker(harness)

        self.assertFalse(harness.cache_cleared)
        self.assertFalse(harness.chain_cleared)
        self.assertFalse(harness.started)
        self.assertEqual(harness.connected_states, [False])
        self.assertIn("reconnecting", harness.status.lower())

    def test_load_ticker_task_does_not_start_a_second_connection(self) -> None:
        class _Service:
            connected = False

            def __init__(self) -> None:
                self.connect_calls = 0

            def connect(self) -> None:
                self.connect_calls += 1

        service = _Service()
        harness = SimpleNamespace(service=service)

        with self.assertRaisesRegex(RuntimeError, "connection was lost"):
            OptionsMatrixApp._load_ticker_task(harness, "AAPL")

        self.assertEqual(service.connect_calls, 0)

    def test_connection_action_guard_prevents_overlapping_clicks(self) -> None:
        class _Harness:
            def __init__(self) -> None:
                self.service = SimpleNamespace(connected=False)
                self._connect_inflight = False
                self._disconnect_inflight = False
                self._connection_active = False
                self.started: list[str] = []
                self.status = ""

            def _start_task(self, kind: str, _func) -> None:
                self.started.append(kind)

            def _connect_task(self) -> None:
                return None

            def _set_connected_state(self, _connected: bool) -> None:
                return None

            def _set_status(self, message: str) -> None:
                self.status = message

        harness = _Harness()

        OptionsMatrixApp._connect(harness)
        OptionsMatrixApp._connect(harness)
        OptionsMatrixApp._disconnect(harness)

        self.assertEqual(harness.started, ["connect"])
        self.assertTrue(harness._connect_inflight)
        self.assertIn("wait", harness.status.lower())

    def test_width_band_controls_preserve_choices_and_disable_empty_bands(self) -> None:
        class _BooleanValue:
            def __init__(self, value: bool) -> None:
                self.value = value

            def get(self) -> bool:
                return self.value

            def set(self, value: bool) -> None:
                self.value = bool(value)

        class _Button:
            def __init__(self) -> None:
                self.state = ""

            def configure(self, *, state: str) -> None:
                self.state = state

        class _Harness:
            _remember_width_band_preferences = OptionsMatrixApp._remember_width_band_preferences

            def __init__(self) -> None:
                self.availability: tuple[float, ...] | None = None
                self.width_vars = {
                    band: _BooleanValue(True)
                    for band in MATRIX_WIDTH_BANDS
                }
                self.width_checkbuttons = {
                    band: _Button()
                    for band in MATRIX_WIDTH_BANDS
                }
                self.available_width_bands: tuple[float, ...] = ()
                self._width_band_preferences = {
                    band: True
                    for band in MATRIX_WIDTH_BANDS
                }

            def _staged_width_band_availability(self) -> tuple[float, ...] | None:
                return self.availability

        harness = _Harness()

        OptionsMatrixApp._refresh_width_band_controls(harness)

        self.assertTrue(all(variable.get() for variable in harness.width_vars.values()))
        self.assertTrue(
            all(button.state == "disabled" for button in harness.width_checkbuttons.values())
        )

        harness.availability = (2.0, 4.0)
        OptionsMatrixApp._refresh_width_band_controls(harness)

        self.assertEqual(harness.available_width_bands, (2.0, 4.0))
        self.assertEqual(
            {band: variable.get() for band, variable in harness.width_vars.items()},
            {1.0: False, 2.0: True, 3.0: False, 4.0: True, 5.0: False},
        )
        self.assertEqual(
            {band: button.state for band, button in harness.width_checkbuttons.items()},
            {
                1.0: "disabled",
                2.0: "normal",
                3.0: "disabled",
                4.0: "normal",
                5.0: "disabled",
            },
        )

        harness.width_vars[4.0].set(False)
        harness.width_vars[2.0].set(False)
        OptionsMatrixApp._remember_width_band_preferences(harness)
        OptionsMatrixApp._refresh_width_band_controls(harness)
        self.assertFalse(harness.width_vars[4.0].get())
        self.assertFalse(harness.width_vars[2.0].get())

    def test_disconnect_result_saves_cache_and_marks_tabs_stale(self) -> None:
        class _Harness:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def _save_current_matrix_cache(self) -> None:
                self.calls.append("save")

            def _set_connected_state(self, connected: bool) -> None:
                self.calls.append(f"connected:{connected}")

            def _clear_loaded_chain(self, *, clear_matrix: bool = True) -> None:
                self.calls.append(f"clear:{clear_matrix}")

            def _mark_all_matrix_tabs_stale(self) -> None:
                self.calls.append("stale")

            def _active_tab(self) -> None:
                return None

            def _set_market_data_quote_status(self, status) -> None:
                self.calls.append("quote_status")

            def _redraw_matrix_tabs(self) -> None:
                self.calls.append("tabs")

            def _redraw_all(self) -> None:
                self.calls.append("redraw")

            def _set_status(self, message: str) -> None:
                self.calls.append(message)

        harness = _Harness()

        OptionsMatrixApp._handle_worker_result(harness, SimpleNamespace(kind="disconnect", value=None))

        self.assertEqual(
            harness.calls,
            ["save", "connected:False", "clear:False", "stale", "quote_status", "tabs", "redraw", "Disconnected."],
        )

    def test_poll_worker_events_discards_stale_tokens_and_workspace_generations(self) -> None:
        applied: list[str] = []

        class _Harness:
            def __init__(self) -> None:
                self.worker_events: queue.Queue[SimpleNamespace] = queue.Queue()
                self._task_tokens = {"load_ticker": 7, "subscribe_quotes": 2, "record_snapshot": 4}
                self._workspace_generation = 3
                self._combo_subscription_inflight = True
                self._combo_target_key = ("old",)
                self._recorder_snapshot_inflight = True
                self.status = ""

            def _set_status(self, message: str) -> None:
                self.status = message

            def _discard_stale_worker_result(self, kind: str) -> None:
                OptionsMatrixApp._discard_stale_worker_result(self, kind)

            def _handle_worker_result(self, result) -> None:
                applied.append(f"{result.kind}:{result.token}")

        harness = _Harness()
        harness.worker_events.put(
            SimpleNamespace(kind="load_ticker", token=5, generation=3, value="stale-token", error=None)
        )
        harness.worker_events.put(
            SimpleNamespace(kind="load_ticker", token=7, generation=3, value="fresh", error=None)
        )
        harness.worker_events.put(
            SimpleNamespace(kind="subscribe_quotes", token=2, generation=2, value=None, error=None)
        )
        harness.worker_events.put(
            SimpleNamespace(kind="record_snapshot", token=4, generation=2, value=None, error=None)
        )
        harness.worker_events.put(
            SimpleNamespace(kind="load_ticker", token=6, generation=3, value=None, error="boom")
        )

        OptionsMatrixApp._poll_worker_events(harness)

        self.assertEqual(applied, ["load_ticker:7"])
        self.assertFalse(harness._combo_subscription_inflight)
        self.assertIsNone(harness._combo_target_key)
        self.assertFalse(harness._recorder_snapshot_inflight)
        self.assertEqual(harness.status, "")

    def test_connect_result_survives_workspace_generation_change(self) -> None:
        class _Harness:
            def __init__(self) -> None:
                self.worker_events: queue.Queue[SimpleNamespace] = queue.Queue()
                self._task_tokens = {"connect": 4}
                self._workspace_generation = 9
                self._connect_inflight = True
                self._disconnect_inflight = False
                self._connection_active = True
                self.service = SimpleNamespace(connected=True, connection_active=True)
                self.connected_states: list[bool] = []
                self.status = ""

            def _set_connected_state(self, connected: bool) -> None:
                self.connected_states.append(connected)

            def _set_status(self, message: str) -> None:
                self.status = message

            def _discard_stale_worker_result(self, kind: str) -> None:
                OptionsMatrixApp._discard_stale_worker_result(self, kind)

            def _handle_worker_result(self, result) -> None:
                OptionsMatrixApp._handle_worker_result(self, result)

        harness = _Harness()
        harness.worker_events.put(
            SimpleNamespace(kind="connect", token=4, generation=8, value=None, error=None)
        )

        OptionsMatrixApp._poll_worker_events(harness)

        self.assertFalse(harness._connect_inflight)
        self.assertEqual(harness.connected_states, [True])
        self.assertEqual(harness.status, "Connected.")

    def test_service_connection_status_updates_ui_and_resyncs_after_reconnect(self) -> None:
        class _Harness:
            def __init__(self) -> None:
                self._quote_event_generation = 3
                self._service_connected = True
                self._connection_active = True
                self.service = SimpleNamespace(events=queue.Queue())
                self.connected_states: list[tuple[bool, bool]] = []
                self.statuses: list[str] = []
                self.subscription_syncs = 0

            def _set_connected_state(self, connected: bool) -> None:
                self.connected_states.append((connected, self._connection_active))

            def _set_status(self, message: str) -> None:
                self.statuses.append(message)

            def _sync_quote_subscriptions(self) -> None:
                self.subscription_syncs += 1

            def _handle_quote(self, _quote: QuoteSnapshot) -> None:
                return None

            def _handle_subscription_status(self, _subscriptions) -> None:
                return None

        harness = _Harness()
        harness.service.events.put(
            {
                "type": "status",
                "message": "Connection interrupted.",
                "connected": False,
                "connection_active": True,
            }
        )
        harness.service.events.put(
            {
                "type": "status",
                "message": "Reconnected.",
                "connected": True,
                "connection_active": True,
            }
        )

        OptionsMatrixApp._poll_service_events(harness)

        self.assertEqual(harness.connected_states, [(False, True), (True, True)])
        self.assertEqual(harness.statuses, ["Connection interrupted.", "Reconnected."])
        self.assertEqual(harness.subscription_syncs, 1)

    def test_retry_countdown_updates_connection_detail_without_overwriting_failure_status(self) -> None:
        class _Harness:
            def __init__(self) -> None:
                self._quote_event_generation = 0
                self._service_connected = True
                self._connection_active = True
                self.service = SimpleNamespace(events=queue.Queue())
                self.statuses: list[str] = []
                self.connection_messages: list[str] = []

            def _set_connected_state(self, _connected: bool) -> None:
                self.connection_messages.append(self._connection_status_message)

            def _set_status(self, message: str) -> None:
                self.statuses.append(message)

            def _sync_quote_subscriptions(self) -> None:
                return None

            def _handle_quote(self, _quote: QuoteSnapshot) -> None:
                return None

            def _handle_subscription_status(self, _subscriptions) -> None:
                return None

        harness = _Harness()
        for message in (
            "Schwab streamer reconnect failed: socket closed (1006).",
            "Schwab streamer reconnecting in 4s (attempt 2/5) ...",
            "",
        ):
            harness.service.events.put(
                {
                    "type": "status",
                    "message": message,
                    "connected": False,
                    "connection_active": True,
                }
            )

        OptionsMatrixApp._poll_service_events(harness)

        self.assertEqual(
            harness.statuses,
            ["Schwab streamer reconnect failed: socket closed (1006)."],
        )
        self.assertEqual(
            harness.connection_messages[-1],
            "Schwab streamer reconnecting in 4s (attempt 2/5) ...",
        )

    def test_reconnecting_connection_state_disables_connect_and_allows_disconnect(self) -> None:
        class _Value:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value: str) -> None:
                self.value = value

        class _Button:
            def __init__(self) -> None:
                self.state = ""

            def configure(self, *, state: str) -> None:
                self.state = state

        harness = SimpleNamespace(
            _connect_inflight=False,
            _disconnect_inflight=False,
            _connection_active=True,
            connection_var=_Value(),
            auth_status_var=_Value(),
            connect_button=_Button(),
            disconnect_button=_Button(),
        )

        with patch(
            "options_matrix.app.refresh_token_issued_at",
            return_value=datetime.now(timezone.utc) - timedelta(days=8),
        ):
            OptionsMatrixApp._set_connected_state(harness, False)

        self.assertEqual(harness.connection_var.value, "↻ RETRY")
        self.assertEqual(harness.auth_status_var.value, "⚠ AUTH DUE")
        self.assertEqual(harness.connect_button.state, "disabled")
        self.assertEqual(harness.disconnect_button.state, "normal")

    def test_discarded_stale_subscription_immediately_resyncs_latest_target(self) -> None:
        class _Harness:
            def __init__(self) -> None:
                self._combo_subscription_inflight = True
                self._combo_target_key = ("old",)
                self.syncs = 0

            def _sync_quote_subscriptions(self) -> None:
                self.syncs += 1

        harness = _Harness()

        OptionsMatrixApp._discard_stale_worker_result(harness, "subscribe_quotes")

        self.assertFalse(harness._combo_subscription_inflight)
        self.assertIsNone(harness._combo_target_key)
        self.assertEqual(harness.syncs, 1)

    def test_subscription_status_counts_distinct_failure_episodes_and_filters_generation(self) -> None:
        class _Harness:
            def __init__(self) -> None:
                self._quote_event_generation = 7
                self._subscription_diagnostics_by_con_id = {}
                self._subscription_last_states = {}
                self._subscription_failure_counts = {}
                self.active_tab_id = None

        harness = _Harness()
        failed = SimpleNamespace(contract_con_id=101, generation=7, state="failed")
        subscribed = SimpleNamespace(contract_con_id=101, generation=7, state="subscribed")
        old_generation = SimpleNamespace(contract_con_id=202, generation=6, state="failed")

        OptionsMatrixApp._handle_subscription_status(harness, [failed, old_generation])
        self.assertEqual(set(harness._subscription_diagnostics_by_con_id), {101})
        self.assertEqual(harness._subscription_failure_counts[101], 1)
        self.assertNotIn(202, harness._subscription_failure_counts)

        OptionsMatrixApp._handle_subscription_status(harness, [failed])
        self.assertEqual(harness._subscription_failure_counts[101], 1)

        OptionsMatrixApp._handle_subscription_status(harness, [subscribed])
        OptionsMatrixApp._handle_subscription_status(harness, [failed])
        self.assertEqual(harness._subscription_failure_counts[101], 2)
        self.assertEqual(harness._subscription_last_states[101], "failed")

    def test_poll_service_events_rejects_old_generation_quote_and_accepts_current_quote(self) -> None:
        old_quote = QuoteSnapshot(label="old")
        current_quote = QuoteSnapshot(label="current")

        class _Harness:
            def __init__(self) -> None:
                self._quote_event_generation = 12
                self.service = SimpleNamespace(events=queue.Queue())
                self.handled_quotes = []
                self.statuses = []

            def _handle_quote(self, quote: QuoteSnapshot) -> None:
                self.handled_quotes.append(quote.label)

            def _set_status(self, message: str) -> None:
                self.statuses.append(message)

            def _handle_subscription_status(self, _subscriptions) -> None:
                return None

        harness = _Harness()
        harness.service.events.put({"type": "quote", "generation": 11, "quote": old_quote})
        harness.service.events.put({"type": "status", "generation": 11, "message": "old status"})
        harness.service.events.put({"type": "quote", "generation": 12, "quote": current_quote})
        harness.service.events.put({"type": "status", "generation": 12, "message": "current status"})

        OptionsMatrixApp._poll_service_events(harness)

        self.assertEqual(harness.handled_quotes, ["current"])
        self.assertEqual(harness.statuses, ["current status"])

    def test_mark_all_matrix_tabs_stale_keeps_tabs_and_clears_quote_data(self) -> None:
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="101",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        tab = MatrixTabState(
            tab_id=1,
            label=matrix_tab_label(matrix_input),
            matrix_input=matrix_input,
            row_strikes=(100.0, 101.0),
            matrix_plan=build_matrix_selection_plan([], [], []),
            leg_quotes={1: QuoteSnapshot(label="leg")},
            leg_quote_ids=frozenset({1}),
            matrix_snapshots={(100.0, 1.0): _snapshot(100.0, 1.0, 0.50, 0.10)},
            ranked_signals=[_ranked_signal(1, "100/101", 1.0, SIGNAL_BUY, 0.40, 0.10)],
        )

        class _Harness:
            _mark_matrix_tab_stale = staticmethod(OptionsMatrixApp._mark_matrix_tab_stale)

            def __init__(self) -> None:
                self.matrix_tabs = [tab]
                self.active_tab_id = 1
                self.leg_quote_cache = {1: QuoteSnapshot(label="leg")}
                self.underlying_quote = QuoteSnapshot(label="SPY")
                self.active_market_data_quote_counts = object()
                self._combo_target_key = object()
                self._combo_subscription_inflight = True
                self.synced = False

            def _sync_view_from_active_tab(self) -> None:
                self.synced = True

        harness = _Harness()

        OptionsMatrixApp._mark_all_matrix_tabs_stale(harness)

        self.assertEqual(len(harness.matrix_tabs), 1)
        self.assertTrue(harness.matrix_tabs[0].is_stale)
        self.assertEqual(harness.matrix_tabs[0].leg_quotes, {})
        self.assertEqual(harness.matrix_tabs[0].leg_quote_ids, frozenset())
        self.assertEqual(harness.matrix_tabs[0].matrix_snapshots, {})
        self.assertEqual(harness.matrix_tabs[0].ranked_signals, [])
        self.assertEqual(harness.leg_quote_cache, {})
        self.assertIsNone(harness.underlying_quote)
        self.assertIsNone(harness.active_market_data_quote_counts)
        self.assertIsNone(harness._combo_target_key)
        self.assertFalse(harness._combo_subscription_inflight)
        self.assertTrue(harness.synced)

    def test_cached_workspace_loads_as_stale_tabs_on_startup(self) -> None:
        class _StringValue:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value: str) -> None:
                self.value = value

            def get(self) -> str:
                return self.value

        class _Harness:
            def __init__(self) -> None:
                self.cached_matrix_workspace = CachedMatrixWorkspace(
                    ticker_symbol="SPY",
                    active_tab_identity=("SPY", "20300117", RIGHT_PUT),
                    matrix_mode=MATRIX_MODE_REGULAR,
                    tabs=(
                        CachedMatrixTab(
                            "20300117",
                            RIGHT_CALL,
                            100.0,
                            103.0,
                            (1.0,),
                            (101.0, 1.0),
                            "Jan17'30 C 100-103",
                            (100.0, 101.0, 102.0, 103.0),
                        ),
                        CachedMatrixTab(
                            "20300117",
                            RIGHT_PUT,
                            100.0,
                            103.0,
                            (1.0, 2.0),
                            (101.0, 1.0),
                            "Jan17'30 P 100-103",
                            (100.0, 101.0, 102.0, 103.0),
                        ),
                    ),
                )
                self.matrix_tabs: list[MatrixTabState] = []
                self.active_tab_id = None
                self._next_tab_id = 1
                self.ticker_var = _StringValue()
                self.expiration_display_to_raw = {}
                self.expiration_combo = {}
                self.controls_synced = None
                self.view_synced = False
                self.tabs_redrawn = False
                self.redrawn = False
                self.status = ""

            def _active_tab(self):
                return OptionsMatrixApp._active_tab(self)

            def _sync_controls_from_stale_matrix_input(self, matrix_input) -> None:
                self.controls_synced = matrix_input

            def _sync_view_from_active_tab(self) -> None:
                self.view_synced = True

            def _redraw_matrix_tabs(self) -> None:
                self.tabs_redrawn = True

            def _redraw_all(self) -> None:
                self.redrawn = True

            def _set_status(self, message: str) -> None:
                self.status = message

        harness = _Harness()

        OptionsMatrixApp._load_cached_workspace_as_stale_tabs(harness)

        self.assertEqual(len(harness.matrix_tabs), 2)
        self.assertEqual(harness.active_tab_id, 2)
        self.assertTrue(all(tab.is_stale for tab in harness.matrix_tabs))
        self.assertEqual(harness.matrix_tabs[0].matrix_snapshots, {})
        self.assertEqual(harness.matrix_tabs[0].ranked_signals, [])
        self.assertEqual(harness.matrix_tabs[0].leg_quotes, {})
        self.assertEqual(harness.matrix_tabs[1].row_strikes, (100.0, 101.0, 102.0, 103.0))
        self.assertEqual(harness.matrix_mode, DEFAULT_MATRIX_MODE)
        self.assertEqual(harness.matrix_tabs[0].matrix_input.matrix_mode, DEFAULT_MATRIX_MODE)
        self.assertEqual(harness.matrix_tabs[0].matrix_input.width_bands, (1.0,))
        self.assertEqual(harness.matrix_tabs[1].matrix_input.width_bands, (1.0, 2.0))
        self.assertEqual(harness.controls_synced.side, RIGHT_PUT)
        self.assertTrue(harness.view_synced)
        self.assertTrue(harness.tabs_redrawn)
        self.assertTrue(harness.redrawn)
        self.assertEqual(harness.status, "Cached matrix tabs loaded. Click RELOAD MATRIX to request live quotes.")

    def test_reloaded_matrix_tab_rebuilds_only_active_stale_tab(self) -> None:
        class _StringValue:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value: str) -> None:
                self.value = value

            def get(self) -> str:
                return self.value

        class _Harness:
            def __init__(self) -> None:
                active_input = parse_matrix_input_state(
                    display_expiration="2030-01-17",
                    raw_expiration="20300117",
                    side=RIGHT_CALL,
                    low_strike="100",
                    high_strike="103",
                    width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
                    ticker_symbol="SPY",
                )
                other_input = parse_matrix_input_state(
                    display_expiration="2030-01-17",
                    raw_expiration="20300117",
                    side=RIGHT_PUT,
                    low_strike="100",
                    high_strike="103",
                    width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
                    ticker_symbol="SPY",
                )
                self.matrix_tabs = [
                    MatrixTabState(
                        tab_id=1,
                        label=matrix_tab_label(active_input),
                        matrix_input=active_input,
                        row_strikes=(100.0, 101.0, 102.0, 103.0),
                        matrix_plan=build_matrix_selection_plan([], [], []),
                        selected_cell_key=(101.0, 1.0),
                        is_stale=True,
                    ),
                    MatrixTabState(
                        tab_id=2,
                        label=matrix_tab_label(other_input),
                        matrix_input=other_input,
                        row_strikes=(100.0, 101.0, 102.0, 103.0),
                        matrix_plan=build_matrix_selection_plan([], [], []),
                        is_stale=True,
                    ),
                ]
                self.active_tab_id = 1
                self.leg_quote_cache = {}
                self.underlying_quote = None
                self.service = SimpleNamespace(
                    chain_underlying_price=lambda _search_result, _expiration: None,
                )
                self.formula_settings = DEFAULT_FORMULA_SETTINGS
                self.ticker_var = _StringValue()
                self.option_records_by_expiration = {}
                self.option_records = []
                self.controls_synced = None
                self.view_synced = False
                self.tabs_redrawn = False
                self.redrawn = False
                self.subscriptions_synced = False
                self.cache_saved = False
                self.status = ""

            def _populate_expiration_values(self) -> list[str]:
                return ["2030-01-17"]

            def _active_tab(self):
                return OptionsMatrixApp._active_tab(self)

            def _rebuild_tab_from_quotes(self, tab: MatrixTabState) -> None:
                OptionsMatrixApp._ensure_tab_selected_cell(self, tab)

            def _sync_controls_from_matrix_input(self, matrix_input) -> None:
                self.controls_synced = matrix_input

            def _sync_view_from_active_tab(self) -> None:
                self.view_synced = True

            def _redraw_matrix_tabs(self) -> None:
                self.tabs_redrawn = True

            def _redraw_all(self) -> None:
                self.redrawn = True

            def _sync_quote_subscriptions(self, *, force: bool = False) -> None:
                self.subscriptions_synced = force

            def _save_current_matrix_cache(self) -> None:
                self.cache_saved = True

            def _set_status(self, message: str) -> None:
                self.status = message

        records = [_option(strike, RIGHT_CALL) for strike in range(100, 105)]
        result = SimpleNamespace(
            search_result=SimpleNamespace(chain=SimpleNamespace(expirations=("20300117",))),
            records=records,
            tab_id=1,
        )
        harness = _Harness()

        OptionsMatrixApp._apply_reloaded_matrix_tab(harness, result)

        self.assertFalse(harness.matrix_tabs[0].is_stale)
        self.assertTrue(harness.matrix_tabs[1].is_stale)
        self.assertEqual(harness.matrix_tabs[0].row_strikes, (100.0, 101.0, 102.0, 103.0))
        self.assertEqual(harness.matrix_tabs[0].selected_cell_key, (101.0, 1.0))
        self.assertEqual(harness.controls_synced.side, RIGHT_CALL)
        self.assertTrue(harness.view_synced)
        self.assertTrue(harness.tabs_redrawn)
        self.assertTrue(harness.redrawn)
        self.assertTrue(harness.subscriptions_synced)
        self.assertTrue(harness.cache_saved)
        self.assertEqual(harness.status, "Reloaded matrix Jan17'30 C 100-103.")

    def test_reload_cached_both_tab_restores_two_live_side_states(self) -> None:
        class _StringValue:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value: str) -> None:
                self.value = value

        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=MATRIX_VIEW_BOTH,
            low_strike="100",
            high_strike="103",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        stale_tab = MatrixTabState(
            tab_id=1,
            label=matrix_tab_label(matrix_input),
            matrix_input=matrix_input,
            row_strikes=(100.0, 101.0, 102.0, 103.0),
            matrix_plan=build_matrix_selection_plan([], [], []),
            selected_cell_key=(RIGHT_PUT, 101.0, 1.0),
            is_stale=True,
        )

        class _Harness:
            def __init__(self) -> None:
                self.matrix_tabs = [stale_tab]
                self.active_tab_id = 1
                self.search_result = None
                self.ticker_var = _StringValue()
                self.option_records_by_expiration = {}
                self.option_records = []
                self.leg_quote_cache = {}
                self.underlying_quote = None
                self.service = SimpleNamespace(chain_underlying_price=lambda *_args: None)
                self.controls_synced = None
                self.status = ""

            def _populate_expiration_values(self):
                return ["2030-01-17"]

            def _rebuild_tab_from_quotes(self, tab) -> None:
                OptionsMatrixApp._ensure_tab_selected_cell(self, tab)

            def _sync_controls_from_matrix_input(self, matrix_input) -> None:
                self.controls_synced = matrix_input

            def _sync_view_from_active_tab(self) -> None:
                return

            def _redraw_matrix_tabs(self) -> None:
                return

            def _redraw_all(self) -> None:
                return

            def _sync_quote_subscriptions(self, *, force: bool = False) -> None:
                return

            def _save_current_matrix_cache(self) -> None:
                return

            def _set_status(self, message: str) -> None:
                self.status = message

        strikes = [float(strike) for strike in range(99, 105)]
        records = [
            *[_anchor_call_option(strike) for strike in strikes],
            *[_option(strike, RIGHT_PUT) for strike in strikes],
        ]
        result = SimpleNamespace(
            tab_id=1,
            search_result=SimpleNamespace(chain=SimpleNamespace(expirations=("20300117",))),
            records=records,
        )
        harness = _Harness()

        OptionsMatrixApp._apply_reloaded_matrix_tab(harness, result)

        self.assertFalse(stale_tab.is_stale)
        self.assertEqual(set(stale_tab.side_states), {RIGHT_PUT, RIGHT_CALL})
        self.assertEqual(stale_tab.selected_cell_key, (RIGHT_PUT, 101.0, 1.0))
        self.assertEqual(len(flatten_matrix_tabs_for_recording([stale_tab])), 2)
        self.assertEqual(harness.controls_synced.side, MATRIX_VIEW_BOTH)
        self.assertEqual(harness.status, "Reloaded matrix Jan17'30 C/P 100-103.")

    def test_workspace_ui_uses_simple_matrix_and_bottom_notebook_layout(self) -> None:
        class _FakeWidget:
            registry: list["_FakeWidget"] = []

            def __init__(self, kind: str, parent=None, **kwargs) -> None:
                self.kind = kind
                self.parent = parent
                self.kwargs = dict(kwargs)
                self.children: list[_FakeWidget] = []
                self.grid_info: dict[str, object] = {}
                self.pack_info: dict[str, object] = {}
                self.row_configs: dict[int, dict[str, object]] = {}
                self.column_configs: dict[int, dict[str, object]] = {}
                self.added: list[tuple[_FakeWidget, dict[str, object]]] = []
                self.bindings: list[tuple[str, dict[str, object]]] = []
                self.configured: dict[str, object] = {}
                _FakeWidget.registry.append(self)
                if isinstance(parent, _FakeWidget):
                    parent.children.append(self)

            def grid(self, **kwargs) -> None:
                self.grid_info = dict(kwargs)

            def pack(self, **kwargs) -> None:
                self.pack_info = dict(kwargs)

            def grid_remove(self) -> None:
                self.grid_info["removed"] = True

            def rowconfigure(self, index: int, **kwargs) -> None:
                self.row_configs[index] = dict(kwargs)

            def columnconfigure(self, index: int, **kwargs) -> None:
                self.column_configs[index] = dict(kwargs)

            def add(self, child, **kwargs) -> None:
                self.added.append((child, dict(kwargs)))

            def bind(self, event: str, _callback, **kwargs) -> None:
                self.bindings.append((event, dict(kwargs)))

            def configure(self, **kwargs) -> None:
                self.configured.update(kwargs)

            def heading(self, *_args, **_kwargs) -> None:
                return

            def column(self, *_args, **_kwargs) -> None:
                return

            def state(self, *_args, **_kwargs) -> None:
                return

            def xview(self, *_args, **_kwargs) -> None:
                return

            def yview(self, *_args, **_kwargs) -> None:
                return

            def set(self, *_args, **_kwargs) -> None:
                return

        def widget_factory(kind: str):
            def build(parent=None, *_args, **kwargs):
                return _FakeWidget(kind, parent, **kwargs)

            return build

        class _BooleanValue:
            def __init__(self, value: bool) -> None:
                self.value = value

            def get(self) -> bool:
                return self.value

            def set(self, value: bool) -> None:
                self.value = bool(value)

        class _Harness:
            def __init__(self) -> None:
                self.root = _FakeWidget("Root")
                for name in (
                    "connection_var",
                    "auth_status_var",
                    "market_data_quote_status_var",
                    "recorder_indicator_var",
                    "status_var",
                    "status_display_var",
                    "ticker_var",
                    "expiration_var",
                    "side_var",
                    "low_strike_var",
                    "high_strike_var",
                    "selected_header_var",
                ):
                    setattr(self, name, object())
                self.width_vars = {
                    band: _BooleanValue(True)
                    for band in MATRIX_WIDTH_BANDS
                }
                self.width_checkbuttons = {}
                self.grid_font = "grid"
                self.grid_font_bold = "grid-bold"
                self.detail_font = "detail"
                self.detail_font_bold = "detail-bold"
                self.tooltip_specs: list[tuple[_FakeWidget, object]] = []

            def _install_tooltip(self, widget, text_source) -> None:
                self.tooltip_specs.append((widget, text_source))

            _build_connection_bar = OptionsMatrixApp._build_connection_bar
            _build_matrix_controls = OptionsMatrixApp._build_matrix_controls

            def __getattr__(self, name: str):
                if name.startswith("_"):
                    return lambda *_args, **_kwargs: None
                raise AttributeError(name)

        app_globals = OptionsMatrixApp._build_ui.__globals__
        tk_module = app_globals["tk"]
        ttk_module = app_globals["ttk"]
        _FakeWidget.registry.clear()
        harness = _Harness()
        ttk_replacements = {
            name: widget_factory(name)
            for name in (
                "Frame",
                "LabelFrame",
                "Label",
                "Button",
                "Entry",
                "Combobox",
                "Checkbutton",
                "Notebook",
                "Treeview",
                "Scrollbar",
                "Radiobutton",
            )
        }
        tk_replacements = {
            name: widget_factory(name)
            for name in ("Frame", "Label", "Canvas", "PanedWindow")
        }

        with patch.multiple(ttk_module, **ttk_replacements), patch.multiple(
            tk_module,
            **tk_replacements,
        ):
            OptionsMatrixApp._build_ui(harness)

        main = harness.root.children[0]
        self.assertEqual(main.kind, "Frame")
        self.assertEqual(main.grid_info, {"row": 0, "column": 0, "sticky": "nsew"})
        self.assertEqual(main.column_configs[0]["weight"], 1)
        self.assertEqual(main.row_configs[2]["weight"], 1)
        self.assertEqual(main.row_configs[3]["weight"], 0)

        def widget_with_text(text: str) -> _FakeWidget:
            return next(
                widget
                for widget in _FakeWidget.registry
                if widget.kwargs.get("text") == text
            )

        connection_title = widget_with_text("SCHWAB MARKET DATA")
        connection = connection_title.parent
        controls = widget_with_text("TICKER").parent
        matrix_title = widget_with_text("SPREAD WIDTH MATRIX").parent
        self.assertIs(connection.parent, main)
        self.assertEqual(connection.grid_info["row"], 0)
        self.assertEqual(connection.grid_info["sticky"], "ew")
        self.assertEqual(connection_title.grid_info["row"], 0)
        self.assertEqual(harness.connection_status_label.grid_info["row"], 0)
        self.assertEqual(harness.auth_status_label.grid_info["row"], 0)
        self.assertEqual(widget_with_text("CONNECT").grid_info["row"], 0)
        self.assertEqual(widget_with_text("DISCONNECT").grid_info["row"], 0)
        self.assertEqual(harness.recorder_indicator_label.grid_info["row"], 0)
        self.assertEqual(harness.recorder_indicator_label.grid_info["sticky"], "e")
        self.assertEqual(harness.market_data_quote_status_label.grid_info["row"], 1)
        self.assertEqual(harness.market_data_quote_status_label.grid_info["sticky"], "ew")
        self.assertEqual(widget_with_text("STATUS").grid_info["row"], 2)
        self.assertEqual(harness.status_label.grid_info["row"], 2)
        self.assertEqual(harness.status_label.grid_info["sticky"], "ew")
        self.assertEqual(harness.status_label.kwargs["width"], 1)
        self.assertIn("<Configure>", [event for event, _kwargs in harness.status_label.bindings])
        self.assertEqual(connection.row_configs, {})
        persistent_text = {
            str(widget.kwargs.get("text"))
            for widget in _FakeWidget.registry
            if widget.kind == "Label"
        }
        self.assertFalse(any("schwab_token.json" in text for text in persistent_text))
        title_tooltips = [
            source
            for widget, source in harness.tooltip_specs
            if widget is connection_title
        ]
        self.assertEqual(len(title_tooltips), 1)
        self.assertIn("schwab_token.json", str(title_tooltips[0]))
        self.assertIn("schwab_auth", str(title_tooltips[0]))
        self.assertIs(controls.parent, main)
        self.assertEqual(controls.grid_info["row"], 1)
        self.assertEqual(controls.grid_info["sticky"], "ew")
        control_labels = {
            str(widget.kwargs.get("text"))
            for widget in _FakeWidget.registry
            if widget.kind == "Label" and widget.parent is controls
        }
        self.assertNotIn("MODE", control_labels)
        self.assertIn("WIDTH BANDS", control_labels)
        self.assertNotIn("matrix_mode_combo", harness.__dict__)
        width_host = widget_with_text("W1").parent
        width_checks = [
            widget
            for widget in _FakeWidget.registry
            if widget.kind == "Checkbutton" and widget.parent is width_host
        ]
        self.assertEqual(
            [widget.kwargs.get("text") for widget in width_checks],
            ["W1", "W2", "W3", "W4", "W5"],
        )
        self.assertTrue(
            all(widget.kwargs["variable"].get() for widget in width_checks)
        )
        self.assertIs(matrix_title.parent, main)
        self.assertEqual(matrix_title.grid_info["row"], 2)
        self.assertEqual(matrix_title.grid_info["sticky"], "new")
        self.assertIs(harness.matrix_canvas.parent.parent, main)
        self.assertEqual(harness.matrix_canvas.parent.grid_info["row"], 2)
        self.assertEqual(harness.matrix_canvas.parent.grid_info["sticky"], "nsew")
        self.assertEqual(harness.matrix_canvas.grid_info["sticky"], "nsew")

        notebooks = [widget for widget in _FakeWidget.registry if widget.kind == "Notebook"]
        self.assertEqual(len(notebooks), 1)
        bottom = notebooks[0]
        self.assertIs(bottom.parent, main)
        self.assertEqual(bottom.grid_info["row"], 3)
        self.assertEqual(bottom.grid_info["sticky"], "ew")
        self.assertEqual(
            [options["text"] for _child, options in bottom.added],
            ["Signals"],
        )
        signals_tab = bottom.added[0][0]
        self.assertNotIn("recorder_tab", harness.__dict__)
        self.assertNotIn("recorder_start_button", harness.__dict__)
        self.assertEqual(signals_tab.column_configs[0]["weight"], 0)
        self.assertEqual(signals_tab.column_configs[1]["weight"], 1)
        self.assertIs(harness.rank_tree.parent, signals_tab)
        self.assertEqual(
            (harness.rank_tree.grid_info["row"], harness.rank_tree.grid_info["column"]),
            (0, 0),
        )
        detail = harness.detail_canvas.parent
        self.assertIs(detail.parent, signals_tab)
        self.assertEqual((detail.grid_info["row"], detail.grid_info["column"]), (0, 1))
        self.assertEqual(detail.grid_info["sticky"], "nsew")
        self.assertEqual(harness.detail_canvas.grid_info["sticky"], "ew")

        self.assertFalse(any(widget.kind == "PanedWindow" for widget in _FakeWidget.registry))
        self.assertFalse(
            any(widget.kwargs.get("text") == "LIVE SPREAD CURVES" for widget in _FakeWidget.registry)
        )
        self.assertNotIn("workspace_paned", harness.__dict__)
        self.assertNotIn("live_curve_canvas", harness.__dict__)

    def test_matrix_layout_always_uses_dynamic_actual_width_slots(self) -> None:
        actual_widths = (0.5, 1.25, 1.5, 2.5, 2.75, 3.5, 4.5, 5.0)
        parsed = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="105",
            width_states={band: True for band in MATRIX_WIDTH_BANDS},
            ticker_symbol="SPY",
        )

        for legacy_mode in (MATRIX_MODE_REGULAR, MATRIX_MODE_MODIFIED):
            with self.subTest(legacy_mode=legacy_mode):
                matrix_input = replace(
                    parsed,
                    widths=actual_widths,
                    matrix_mode=legacy_mode,
                )
                harness = SimpleNamespace(applied_matrix_input=matrix_input)
                slot_widths = OptionsMatrixApp._matrix_layout_slot_widths(harness)
                layout = matrix_grid_layout(640, actual_widths, slot_widths=slot_widths)

                self.assertEqual(slot_widths, (1.25, 2.75, 5.0))
                self.assertEqual(OptionsMatrixApp._applied_widths(harness), slot_widths)
                self.assertEqual(
                    [slot.width for slot in layout.slots],
                    list(slot_widths),
                )

    def test_matrix_grid_layout_supports_dynamic_fractional_width_slots(self) -> None:
        widths = (0.25, 0.75, 1.25, 2.75, 4.75, 5.0)
        layout = matrix_grid_layout(640, widths, slot_widths=widths)

        self.assertEqual([slot.width for slot in layout.slots], list(widths))
        self.assertEqual([slot.width for slot in layout.slots if slot.selected], list(widths))
        self.assertGreater(layout.total_width, 640)
        self.assertGreaterEqual(layout.group_width, 112.0)

    def test_split_matrix_grid_layout_mirrors_widths_around_center_strikes(self) -> None:
        layout = split_matrix_grid_layout(1260, DEFAULT_WIDTHS)
        put_x = {slot.width: slot.x for slot in layout.put_slots}
        call_x = {slot.width: slot.x for slot in layout.call_slots}

        self.assertAlmostEqual(
            layout.strike_x + (layout.strike_width / 2),
            layout.total_width / 2,
        )
        self.assertAlmostEqual(call_x[1.0] + layout.group_width, layout.strike_x)
        self.assertAlmostEqual(put_x[1.0], layout.strike_x + layout.strike_width)
        self.assertEqual(call_x[5.0], 0.0)
        self.assertGreater(put_x[5.0], put_x[1.0])
        self.assertEqual(
            [slot.width for slot in layout.call_slots if slot.selected],
            list(DEFAULT_WIDTHS),
        )

        actual_widths = tuple(width / 4 for width in range(1, 21) if width % 4 != 2)
        dynamic = split_matrix_grid_layout(
            640,
            actual_widths,
            slot_widths=actual_widths,
        )
        self.assertGreater(dynamic.total_width, 640)
        self.assertGreaterEqual(dynamic.group_width, 112.0)

    def test_split_matrix_renderer_draws_calls_left_and_puts_right(self) -> None:
        class _Canvas:
            def __init__(self) -> None:
                self.texts: list[str] = []
                self.text_positions: list[tuple[str, float]] = []
                self.scrollregion = None
                self.rectangles = []

            def winfo_width(self) -> int:
                return 1260

            def configure(self, **kwargs) -> None:
                self.scrollregion = kwargs.get("scrollregion")

            def create_rectangle(self, *args, **kwargs) -> None:
                self.rectangles.append((args, kwargs))

            def create_text(self, *args, **kwargs) -> None:
                label = str(kwargs.get("text", ""))
                self.texts.append(label)
                self.text_positions.append((label, float(args[0])))

        strikes = [100.0, 101.0, 102.0]
        call_plan = build_matrix_selection_plan(
            [_anchor_call_option(strike) for strike in strikes],
            [100.0, 101.0],
            [1.0],
            context_neighbors=0,
        )
        put_plan = build_matrix_selection_plan(
            [_option(strike, RIGHT_PUT) for strike in strikes],
            [100.0, 101.0],
            [1.0],
            context_neighbors=0,
        )
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=MATRIX_VIEW_BOTH,
            low_strike="100",
            high_strike="102",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
            matrix_mode=MATRIX_MODE_REGULAR,
        )
        tab = MatrixTabState(
            tab_id=1,
            label=matrix_tab_label(matrix_input),
            matrix_input=matrix_input,
            row_strikes=tuple(strikes),
            matrix_plan=build_matrix_selection_plan([], [], []),
            side_states={
                RIGHT_PUT: MatrixSideState(RIGHT_PUT, tuple(strikes), put_plan),
                RIGHT_CALL: MatrixSideState(RIGHT_CALL, tuple(strikes), call_plan),
            },
        )

        class _Harness:
            def __init__(self) -> None:
                self.row_strikes = strikes
                self.selected_cell_key = None
                self.matrix_hitboxes = []
                self.grid_font = "font"
                self.grid_font_bold = "bold"
                self.signal_method = DEFAULT_SIGNAL_METHOD
                self.positions = {}
                self._position_results = {}

            def _applied_widths(self):
                return (1.0,)

            def _matrix_layout_slot_widths(self):
                return (1.0,)

            def _selection_is_itm(self, _selection) -> bool:
                return False

            def _cell_signal_text(self, snapshot, *, muted: bool = False):
                return OptionsMatrixApp._cell_signal_text(self, snapshot, muted=muted)

        canvas = _Canvas()
        harness = _Harness()
        for right, side, average in ((RIGHT_CALL, POSITION_SOLD, 0.60), (RIGHT_PUT, POSITION_BOUGHT, 0.40)):
            selection = tab.side_states[right].matrix_plan.visible[(100.0, 1.0)]
            snapshot = _liquidity_snapshot(100.0, 1.0, right=right)
            tab.side_states[right].matrix_snapshots[(100.0, 1.0)] = snapshot
            key = position_key_for_selection(selection)
            record = PositionRecord(key, side, average)
            harness.positions[key] = record
            harness._position_results[key] = evaluate_position(record, snapshot, DEFAULT_FORMULA_SETTINGS, live=True)
        OptionsMatrixApp._redraw_split_matrix(harness, canvas, tab)

        self.assertIsNotNone(canvas.scrollregion)
        self.assertIn("PUTS", canvas.texts)
        self.assertIn("CALLS", canvas.texts)
        self.assertIn("STRIKE", canvas.texts)
        self.assertIn("EXIT BUY 0.50", canvas.texts)
        self.assertIn("EXIT SELL 0.50", canvas.texts)
        fills = [options.get("fill") for _bounds, options in canvas.rectangles]
        self.assertEqual(fills.count(POSITION_BUY_BG), 1)
        self.assertEqual(fills.count(POSITION_SELL_BG), 1)
        self.assertEqual(canvas.texts.count(POSITION_ICON_TEXT), 2)
        self.assertFalse(any(
            abs(bounds[2] - bounds[0]) <= 10 and abs(bounds[3] - bounds[1]) <= 10
            for bounds, _options in canvas.rectangles
        ))
        combo_positions = [x for label, x in canvas.text_positions if label == "COMBO"]
        signal_positions = [x for label, x in canvas.text_positions if label == "SIGNAL/PRICE"]
        self.assertGreater(combo_positions[0], signal_positions[0])
        self.assertLess(combo_positions[-1], signal_positions[-1])
        keys = [hitbox[-1] for hitbox in harness.matrix_hitboxes]
        self.assertEqual({key[0] for key in keys}, {RIGHT_PUT, RIGHT_CALL})
        self.assertFalse(hasattr(harness, "matrix_watch_hitboxes"))
        self.assertNotIn("✓", canvas.texts)
        strike_x = split_matrix_grid_layout(
            1260,
            (1.0,),
            slot_widths=(1.0,),
        ).strike_x
        for x1, _y1, x2, _y2, key in harness.matrix_hitboxes:
            if key[0] == RIGHT_CALL:
                self.assertLessEqual(x2, strike_x)
            else:
                self.assertGreaterEqual(x1, strike_x + 64.0)


    def test_clicking_left_edge_selects_cell_without_toggling_a_position(self) -> None:
        plan = build_matrix_selection_plan(
            [_option(100.0), _option(101.0), _option(102.0)],
            [100.0, 101.0],
            [1.0],
            context_neighbors=0,
        )
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="102",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        tab = MatrixTabState(
            tab_id=1,
            label=matrix_tab_label(matrix_input),
            matrix_input=matrix_input,
            row_strikes=(100.0, 101.0),
            matrix_plan=plan,
            selected_cell_key=(101.0, 1.0),
        )

        class _Harness:
            def __init__(self) -> None:
                self.matrix_canvas = None
                self.matrix_reload_hitbox = None
                self.matrix_watch_hitboxes = [(0.0, 0.0, 16.0, 20.0, (100.0, 1.0))]
                self.positions = {"saved": SimpleNamespace(armed=True)}
                self.matrix_hitboxes = [(0.0, 0.0, 120.0, 20.0, (100.0, 1.0))]
                self.saved = 0
                self.matrix_redraws = 0
                self.full_redraws = 0
                self.synced = 0

            def _active_tab(self):
                return tab

            def _save_current_matrix_cache(self) -> None:
                self.saved += 1

            def _redraw_matrix(self) -> None:
                self.matrix_redraws += 1

            def _sync_view_from_active_tab(self) -> None:
                self.synced += 1

            def _redraw_all(self) -> None:
                self.full_redraws += 1

        harness = _Harness()

        OptionsMatrixApp._on_matrix_click(harness, SimpleNamespace(x=8.0, y=10.0))

        self.assertEqual(tab.selected_cell_key, (100.0, 1.0))
        self.assertEqual((harness.saved, harness.matrix_redraws), (0, 0))
        self.assertEqual((harness.synced, harness.full_redraws), (1, 1))
        self.assertTrue(harness.positions["saved"].armed)

    def test_active_matrix_quote_refresh_updates_cached_canvas_items_without_redraw(self) -> None:
        plan = build_matrix_selection_plan(
            [_option(100.0), _option(101.0)],
            [100.0],
            [1.0],
            context_neighbors=0,
        )
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="101",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        tab = MatrixTabState(
            tab_id=1,
            label=matrix_tab_label(matrix_input),
            matrix_input=matrix_input,
            row_strikes=(100.0,),
            matrix_plan=plan,
        )

        class _Canvas:
            def __init__(self) -> None:
                self.updates = []

            def itemconfigure(self, item_id: int, **kwargs) -> None:
                self.updates.append((item_id, kwargs))

        class _Harness:
            def __init__(self) -> None:
                self.matrix_canvas = _Canvas()
                self._matrix_render_signature = ("stable",)
                self._matrix_cell_items = {
                    (100.0, 1.0): SimpleNamespace(
                        rectangle_ids=(1, 2),
                        combo_text_id=3,
                        signal_text_id=4,
                    )
                }
                self.selected_cell_key = None
                self.redraws = 0

            def _matrix_render_state_signature(self):
                return ("stable",)

            def _active_tab(self):
                return tab

            def _selection_is_itm(self, _selection) -> bool:
                return False

            def _cell_signal_text(self, _snapshot, *, muted: bool = False):
                return "BUY 0.10", "#008000"

            def _redraw_matrix(self) -> None:
                self.redraws += 1

        harness = _Harness()

        OptionsMatrixApp._refresh_active_matrix_render(harness)

        self.assertEqual(harness.redraws, 0)
        self.assertEqual(
            [item_id for item_id, _kwargs in harness.matrix_canvas.updates[:4]],
            [1, 2, 3, 4],
        )
        self.assertEqual(
            [item_id for item_id, _kwargs in harness.matrix_canvas.updates[4:]],
            [1, 2],
        )
        self.assertEqual(harness.matrix_canvas.updates[2][1]["text"], "100/101")
        self.assertEqual(harness.matrix_canvas.updates[3][1]["text"], "BUY 0.10")

    def test_active_quote_refresh_applies_and_clears_saved_position_exit_tint(self) -> None:
        plan = build_matrix_selection_plan(
            [_option(100.0), _option(101.0)],
            [100.0],
            [1.0],
            context_neighbors=0,
        )
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="101",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        snapshot = _snapshot(100.0, 1.0, 0.50, 0.10)
        snapshot.opportunity_direction = SIGNAL_BUY
        snapshot.opportunity_price = 0.60
        tab = MatrixTabState(
            tab_id=1,
            label=matrix_tab_label(matrix_input),
            matrix_input=matrix_input,
            row_strikes=(100.0,),
            matrix_plan=plan,
            matrix_snapshots={(100.0, 1.0): snapshot},
            selected_cell_key=(100.0, 1.0),
        )

        class _Canvas:
            def __init__(self) -> None:
                self.updates: list[tuple[int, dict[str, object]]] = []

            def itemconfigure(self, item_id: int, **kwargs: object) -> None:
                self.updates.append((item_id, kwargs))

        class _Harness:
            def __init__(self) -> None:
                self.matrix_canvas = _Canvas()
                self._matrix_render_signature = ("stable",)
                self._matrix_cell_items = {
                    (100.0, 1.0): SimpleNamespace(
                        rectangle_ids=(1, 2),
                        combo_text_id=3,
                        signal_text_id=4,
                    )
                }
                self.selected_cell_key = (100.0, 1.0)
                self.signal_method = DEFAULT_SIGNAL_METHOD
                self.redraws = 0

            def _matrix_render_state_signature(self):
                return ("stable",)

            def _active_tab(self):
                return tab

            def _selection_is_itm(self, _selection) -> bool:
                return False

            def _cell_signal_text(self, snapshot, *, muted: bool = False):
                return OptionsMatrixApp._cell_signal_text(self, snapshot, muted=muted)

            def _redraw_matrix(self) -> None:
                self.redraws += 1

        harness = _Harness()
        selection = plan.visible[(100.0, 1.0)]
        key = position_key_for_selection(selection)
        harness.positions = {key: PositionRecord(key, POSITION_SOLD, 0.60)}
        harness._position_results = {key: evaluate_position(harness.positions[key], snapshot, DEFAULT_FORMULA_SETTINGS, live=True)}

        OptionsMatrixApp._refresh_active_matrix_render(harness)

        rectangle_fills = {
            item_id: options["fill"]
            for item_id, options in harness.matrix_canvas.updates
            if item_id in {1, 2} and "fill" in options
        }
        self.assertEqual(rectangle_fills, {1: SELECTED_BG, 2: POSITION_BUY_BG})
        self.assertEqual(next(options["text"] for item, options in harness.matrix_canvas.updates if item == 4), "EXIT BUY 0.50")
        self.assertEqual(harness.redraws, 0)

        # The fair-value BUY remains, but pausing the saved position clears its tint.
        harness.positions[key] = replace(harness.positions[key], armed=False)
        harness.matrix_canvas.updates.clear()
        OptionsMatrixApp._refresh_active_matrix_render(harness)

        cleared_fills = {
            item_id: options["fill"]
            for item_id, options in harness.matrix_canvas.updates
            if item_id in {1, 2} and "fill" in options
        }
        self.assertEqual(cleared_fills, {1: SELECTED_BG, 2: SELECTED_BG})
        self.assertTrue(next(options["text"] for item, options in harness.matrix_canvas.updates if item == 4).startswith("B "))
        self.assertEqual(harness.redraws, 0)

    def test_matrix_cell_subscription_style_priority_and_recurrent_failure_width(self) -> None:
        subscribed = resolve_matrix_cell_subscription_style(["subscribed"], ["subscribed"])
        self.assertEqual(subscribed.outline, SUBSCRIPTION_CONFIRMED_COLOR)
        self.assertEqual(subscribed.width, 1)
        self.assertEqual(subscribed.dash, ())

        pending = resolve_matrix_cell_subscription_style(
            ["pending_subscribe", "subscribed"],
            ["failed"],
        )
        self.assertEqual(pending.outline, SUBSCRIPTION_PENDING_COLOR)
        self.assertEqual(pending.width, 2)

        reconciling = resolve_matrix_cell_subscription_style(["desired"], ["subscribed"])
        self.assertEqual(reconciling.outline, SUBSCRIPTION_RECONCILING_COLOR)
        self.assertEqual(reconciling.dash, (3, 2))

        context_failed = resolve_matrix_cell_subscription_style(
            ["subscribed", "subscribed"],
            ["failed"],
        )
        self.assertEqual(context_failed.outline, SUBSCRIPTION_CONTEXT_COLOR)
        self.assertEqual(context_failed.width, 2)
        self.assertEqual(context_failed.dash, (3, 2))

        direct_failed = resolve_matrix_cell_subscription_style(
            ["failed", "subscribed"],
            ["pending_subscribe"],
            recurrent_failures=1,
        )
        self.assertEqual(direct_failed.outline, SUBSCRIPTION_FAILED_COLOR)
        self.assertEqual(direct_failed.width, 2)
        self.assertEqual(direct_failed.dash, ())

        recurrent_direct_failure = resolve_matrix_cell_subscription_style(
            ["failed", "subscribed"],
            recurrent_failures=2,
        )
        self.assertEqual(recurrent_direct_failure.outline, SUBSCRIPTION_FAILED_COLOR)
        self.assertEqual(recurrent_direct_failure.width, 3)

    def test_hidden_context_failure_is_amber_while_direct_leg_failure_is_red(self) -> None:
        contracts = [_option(float(strike)) for strike in range(98, 105)]
        plan = build_matrix_selection_plan(contracts, [100.0], [1.0], context_neighbors=2)
        selection = plan.visible[(100.0, 1.0)]
        direct_ids = {leg.contract.con_id for leg in selection.legs}
        all_ids = {
            leg.contract.con_id
            for context_selection in [*flatten_matrix_context(plan), *plan.visible.values()]
            for leg in context_selection.legs
        }
        hidden_id = next(con_id for con_id in all_ids if con_id not in direct_ids)
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="101",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        tab = MatrixTabState(
            tab_id=1,
            label=matrix_tab_label(matrix_input),
            matrix_input=matrix_input,
            row_strikes=(100.0,),
            matrix_plan=plan,
        )

        class _Harness:
            def __init__(self) -> None:
                self._matrix_cell_items = {(100.0, 1.0): object()}
                self._subscription_failure_counts = {}
                self._subscription_diagnostics_by_con_id = {
                    con_id: SimpleNamespace(state="subscribed", quote_kind="leg")
                    for con_id in all_ids
                }
                self._subscription_diagnostics_by_con_id[-1] = SimpleNamespace(
                    state="subscribed",
                    quote_kind="underlying",
                )

        harness = _Harness()
        harness._subscription_diagnostics_by_con_id[hidden_id] = SimpleNamespace(
            state="failed",
            quote_kind="leg",
        )
        context_style = OptionsMatrixApp._active_subscription_styles(harness, tab)[(100.0, 1.0)]
        self.assertEqual(context_style.outline, SUBSCRIPTION_CONTEXT_COLOR)

        failed_direct_id = next(iter(direct_ids))
        harness._subscription_diagnostics_by_con_id[hidden_id] = SimpleNamespace(
            state="subscribed",
            quote_kind="leg",
        )
        harness._subscription_diagnostics_by_con_id[failed_direct_id] = SimpleNamespace(
            state="failed",
            quote_kind="leg",
        )
        direct_style = OptionsMatrixApp._active_subscription_styles(harness, tab)[(100.0, 1.0)]
        self.assertEqual(direct_style.outline, SUBSCRIPTION_FAILED_COLOR)

    def test_rank_table_redraw_is_skipped_until_rank_signature_changes(self) -> None:
        class _Harness:
            def __init__(self) -> None:
                self.rank_sort_state = RankSortState()
                self.ranked_signals = []
                self._rank_render_signature = None
                self.redraws = 0

            def _rank_table_state_signature(self):
                return OptionsMatrixApp._rank_table_state_signature(self)

            def _redraw_rank_table(self) -> None:
                self.redraws += 1
                self._rank_render_signature = self._rank_table_state_signature()

        harness = _Harness()

        OptionsMatrixApp._redraw_rank_table_if_changed(harness)
        OptionsMatrixApp._redraw_rank_table_if_changed(harness)
        self.assertEqual(harness.redraws, 1)

        harness.rank_sort_state = RankSortState(column="width", direction=RANK_SORT_ASC)
        OptionsMatrixApp._redraw_rank_table_if_changed(harness)
        self.assertEqual(harness.redraws, 2)

    def test_reload_matrix_overlay_hitbox_only_appears_for_stale_active_tab(self) -> None:
        class _Canvas:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple]] = []

            def delete(self, *args) -> None:
                self.calls.append(("delete", args))

            def winfo_width(self) -> int:
                return 640

            def winfo_height(self) -> int:
                return 240

            def create_rectangle(self, *args, **kwargs) -> None:
                self.calls.append(("rectangle", args))

            def create_text(self, *args, **kwargs) -> None:
                self.calls.append(("text", args))

        class _Harness:
            _draw_stale_matrix_overlay = OptionsMatrixApp._draw_stale_matrix_overlay

            def __init__(self, stale: bool) -> None:
                self.matrix_canvas = _Canvas()
                self.matrix_hitboxes = []
                self.matrix_reload_hitbox = None
                self.applied_matrix_input = parse_matrix_input_state(
                    display_expiration="2030-01-17",
                    raw_expiration="20300117",
                    side=RIGHT_CALL,
                    low_strike="100",
                    high_strike="101",
                    width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
                    ticker_symbol="SPY",
                )
                self.row_strikes = [100.0]
                self.matrix_plan = build_matrix_selection_plan([], [], [])
                self.matrix_snapshots = {}
                self.selected_cell_key = None
                self.grid_font = "font"
                self.grid_font_bold = "bold"
                self.stale = stale

            def _applied_widths(self):
                return self.applied_matrix_input.widths

            def _matrix_layout_slot_widths(self):
                return None

            def _active_tab(self):
                return SimpleNamespace(is_stale=self.stale)

            def _selection_is_itm(self, selection) -> bool:
                return False

            def _cell_signal_text(self, snapshot, *, muted: bool = False):
                return "--", "#111111"

        stale_harness = _Harness(stale=True)
        live_harness = _Harness(stale=False)

        OptionsMatrixApp._redraw_matrix(stale_harness)
        OptionsMatrixApp._redraw_matrix(live_harness)

        self.assertIsNotNone(stale_harness.matrix_reload_hitbox)
        self.assertIsNone(live_harness.matrix_reload_hitbox)

    def test_clicking_reload_overlay_reloads_active_stale_tab(self) -> None:
        class _Harness:
            def __init__(self) -> None:
                self.matrix_reload_hitbox = (10, 10, 100, 50)
                self.matrix_hitboxes = [(10, 10, 100, 50, (100.0, 1.0))]
                self.reloaded = False

            def _reload_active_matrix_tab(self) -> None:
                self.reloaded = True

        harness = _Harness()

        OptionsMatrixApp._on_matrix_click(harness, SimpleNamespace(x=20, y=20))

        self.assertTrue(harness.reloaded)

    def test_rank_sort_cycles_and_returns_to_default_edge_rank_order(self) -> None:
        ranked = [
            _ranked_signal(1, "103/104", 1.0, SIGNAL_BUY, 0.40, 0.30),
            _ranked_signal(2, "100/102", 2.0, SIGNAL_SELL, 1.20, 0.10),
            _ranked_signal(3, "101/104", 3.0, SIGNAL_BUY, 0.80, 0.20),
        ]

        default_state = RankSortState()
        self.assertEqual(default_state.direction, RANK_SORT_NONE)
        self.assertEqual([item.rank for item in sorted_ranked_signals(ranked, default_state)], [1, 2, 3])

        edge_ascending = next_rank_sort_state(default_state, "edge")
        self.assertEqual(edge_ascending.direction, RANK_SORT_ASC)
        self.assertEqual(
            [item.edge for item in (item.signal for item in sorted_ranked_signals(ranked, edge_ascending))],
            [0.10, 0.20, 0.30],
        )

        edge_descending = next_rank_sort_state(edge_ascending, "edge")
        self.assertEqual(edge_descending.direction, RANK_SORT_DESC)
        self.assertEqual(
            [item.edge for item in (item.signal for item in sorted_ranked_signals(ranked, edge_descending))],
            [0.30, 0.20, 0.10],
        )

        reset_state = next_rank_sort_state(edge_descending, "edge")
        self.assertEqual(reset_state.direction, RANK_SORT_NONE)
        self.assertEqual([item.rank for item in sorted_ranked_signals(ranked, reset_state)], [1, 2, 3])

        combo_ascending = next_rank_sort_state(reset_state, "combo")
        self.assertEqual(
            [item.pair_key for item in sorted_ranked_signals(ranked, combo_ascending)],
            ["100/102", "101/104", "103/104"],
        )

    def test_detail_display_values_include_fair_value_edge_curve_values(self) -> None:
        snapshot = _snapshot(100.0, 1.0, 0.50, 0.10)
        snapshot.fair_value_edge_curve_mid = 0.55
        snapshot.fair_value_edge_curve_edge = 0.10
        snapshot.fair_value_edge_curve_buy_endpoint = 0.45
        snapshot.fair_value_edge_curve_sell_endpoint = 0.65

        values = fair_value_edge_curve_values(snapshot)

        self.assertIsNotNone(values)
        self.assertEqual(values.buy_endpoint, 0.45)
        self.assertEqual(values.sell_endpoint, 0.65)
        self.assertEqual(
            set(detail_display_values(snapshot)),
            {0.45, 0.50, 0.55, 0.65},
        )

    def test_fair_value_unavailable_message_explains_each_waiting_state(self) -> None:
        snapshot = _snapshot(100.0, 1.0, 0.50, 0.10)

        self.assertEqual(
            fair_value_unavailable_message(
                snapshot,
                underlying_price=None,
                min_points=5,
            ),
            "FV waiting: no usable underlying price",
        )

        snapshot.fair_value_edge_curve_fit_points = 0
        self.assertEqual(
            fair_value_unavailable_message(
                snapshot,
                underlying_price=101.25,
                min_points=5,
            ),
            "FV waiting: no usable atomic observations",
        )

        snapshot.fair_value_edge_curve_fit_points = 3
        self.assertEqual(
            fair_value_unavailable_message(
                snapshot,
                underlying_price=101.25,
                min_points=5,
            ),
            "FV unavailable: incomplete atomic fair-value coverage",
        )

        snapshot.fair_value_edge_curve_mid = 0.55
        snapshot.fair_value_edge_curve_edge = 0.10
        snapshot.fair_value_edge_curve_buy_endpoint = 0.45
        snapshot.fair_value_edge_curve_sell_endpoint = 0.65
        self.assertIsNone(
            fair_value_unavailable_message(
                snapshot,
                underlying_price=101.25,
                min_points=5,
            )
        )

    def test_selected_detail_fair_value_marker_is_explicit_and_visible(self) -> None:
        class _Canvas:
            def __init__(self) -> None:
                self.lines: list[dict[str, object]] = []
                self.texts: list[tuple[str, dict[str, object]]] = []

            def create_line(self, *_args, **kwargs):
                self.lines.append(kwargs)

            def create_oval(self, *_args, **_kwargs):
                return None

            def create_text(self, *_args, **kwargs):
                self.texts.append((kwargs.get("text", ""), kwargs))

        class _Harness:
            detail_font = "detail"
            _draw_detail_notch = staticmethod(OptionsMatrixApp._draw_detail_notch)
            _draw_detail_dot = staticmethod(OptionsMatrixApp._draw_detail_dot)

        snapshot = _snapshot(100.0, 1.0, 0.50, 0.10)
        snapshot.fair_value_edge_curve_mid = 0.55
        snapshot.fair_value_edge_curve_edge = 0.10
        snapshot.fair_value_edge_curve_buy_endpoint = 0.45
        snapshot.fair_value_edge_curve_sell_endpoint = 0.65
        canvas = _Canvas()

        OptionsMatrixApp._draw_fair_value_edge_curve(
            _Harness(),
            canvas,
            snapshot,
            lambda value: value * 100.0,
            48,
        )

        self.assertIn("FV 0.55", [text for text, _kwargs in canvas.texts])
        self.assertTrue(
            any(
                line.get("fill") == FAIR_VALUE_COLOR and line.get("width") == 2
                for line in canvas.lines
            )
        )

    def test_cell_signal_text_marks_unavailable_fair_value_in_amber(self) -> None:
        harness = SimpleNamespace(signal_method=DEFAULT_SIGNAL_METHOD)
        snapshot = _snapshot(100.0, 1.0, 0.50, 0.10)

        waiting_text, waiting_color = OptionsMatrixApp._cell_signal_text(harness, snapshot)

        self.assertEqual(waiting_text, "M 0.50 FV?")
        self.assertEqual(waiting_color, SUBSCRIPTION_CONTEXT_COLOR)

        muted_text, muted_color = OptionsMatrixApp._cell_signal_text(
            harness,
            snapshot,
            muted=True,
        )
        self.assertEqual(muted_text, "M 0.50 FV?")
        self.assertEqual(muted_color, SUBSCRIPTION_CONTEXT_COLOR)

        snapshot.fair_value_edge_curve_mid = 0.55
        snapshot.fair_value_edge_curve_edge = 0.10
        snapshot.fair_value_edge_curve_buy_endpoint = 0.45
        snapshot.fair_value_edge_curve_sell_endpoint = 0.65

        ready_text, ready_color = OptionsMatrixApp._cell_signal_text(harness, snapshot)

        self.assertEqual(ready_text, "M 0.50")
        self.assertEqual(ready_color, "#111111")

    def test_exact_width_generation_keeps_mixed_half_point_strikes_separate(self) -> None:
        contracts = [_option(strike) for strike in (100.0, 100.5, 101.0, 102.0)]

        one_point_pairs = build_exact_width_vertical_pairs(contracts, 1.0)
        half_point_pairs = build_exact_width_vertical_pairs(contracts, 0.5)

        self.assertEqual([pair_key_for_pair(pair) for pair in one_point_pairs], ["100/101", "101/102"])
        self.assertEqual(half_point_pairs, [])

    def test_half_dollar_chain_excludes_dot_fifty_widths_and_visible_strike_legs(self) -> None:
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="100",
            high_strike="105",
            width_states={band: True for band in MATRIX_WIDTH_BANDS},
            ticker_symbol="SPY",
        )
        records = [_option(100.0 + index * 0.5) for index in range(11)]
        input_plan = build_matrix_input_plan(records, matrix_input)
        self.assertIsNotNone(input_plan)
        self.assertEqual(input_plan.widths, (1.0, 2.0, 3.0, 4.0, 5.0))
        self.assertEqual(input_plan.row_strikes, tuple(float(strike) for strike in range(100, 106)))
        self.assertNotIn((100.5, 1.0), input_plan.plan.visible)
        self.assertNotIn((100.5, 4.0), input_plan.plan.visible)
        self.assertIn((100.0, 5.0), input_plan.plan.visible)
        for width in (0.5, 1.5, 2.5, 3.5, 4.5):
            with self.subTest(excluded_width=width):
                self.assertNotIn(width, input_plan.plan.context_by_width)
                self.assertTrue(all(selection.pair.width != width for selection in input_plan.plan.visible.values()))
                self.assertTrue(all(selection.pair.width != width for selection in matrix_quote_target_selections(input_plan.plan)))
        self.assertIsNone(build_matrix_input_plan(records[:2], matrix_input))

    def test_visible_lower_strike_can_use_upper_contract_outside_visible_range(self) -> None:
        contracts = [_option(100.0), _option(105.0)]

        plan = build_matrix_selection_plan(contracts, [100.0], [5.0])

        self.assertIn((100.0, 5.0), plan.visible)
        self.assertEqual(pair_key_for_pair(plan.visible[(100.0, 5.0)].pair), "100/105")

    def test_missing_exact_upper_strike_leaves_matrix_cell_empty(self) -> None:
        contracts = [_option(100.0), _option(100.5), _option(101.5)]

        plan = build_matrix_selection_plan(contracts, [100.0], [1.0])

        self.assertNotIn((100.0, 1.0), plan.visible)

    def test_wider_width_fair_values_derive_from_adjacent_curve(self) -> None:
        contracts = [_option(strike) for strike in range(100, 107)]
        plan = build_matrix_selection_plan(contracts, [102.0], [1.0, 2.0])

        adjacent_mids = {
            "100/101": 0.50,
            "101/102": 0.55,
            "102/103": 0.35,
            "103/104": 0.65,
            "104/105": 0.70,
            "105/106": 0.75,
        }
        leg_quotes = _chain_leg_quotes(
            [float(strike) for strike in range(100, 107)],
            adjacent_mids,
            top_leg_mid=0.10,
        )

        context_snapshots = build_matrix_context_snapshots(
            plan,
            leg_quotes,
            formula_settings=FormulaSettings(fair_value_edge_curve_min_points=4),
            underlying_price=102.5,
        )
        one_wide = {snapshot.pair_key: snapshot for snapshot in context_snapshots[1.0]}
        two_wide = {snapshot.pair_key: snapshot for snapshot in context_snapshots[2.0]}

        # Both widths share one moneyness-qualified adjacent base curve.
        self.assertEqual(one_wide["102/103"].fair_value_edge_curve_fit_points, 5)
        self.assertEqual(two_wide["102/104"].fair_value_edge_curve_fit_points, 5)
        self.assertGreater(one_wide["102/103"].fair_value_edge_curve_mid, 0.0)
        self.assertLess(one_wide["102/103"].fair_value_edge_curve_mid, 1.0)
        self.assertGreater(two_wide["102/104"].fair_value_edge_curve_mid, 0.0)
        self.assertLess(two_wide["102/104"].fair_value_edge_curve_mid, 2.0)
        # Cross-width consistency is an identity: fair(A,C) == fair(A,B) + fair(B,C).
        self.assertAlmostEqual(
            two_wide["102/104"].fair_value_edge_curve_mid,
            round(
                one_wide["102/103"].fair_value_edge_curve_mid
                + one_wide["103/104"].fair_value_edge_curve_mid,
                4,
            ),
            places=6,
        )

    def test_thin_wide_widths_get_derived_fair_values_and_signals(self) -> None:
        contracts = [_option(float(strike)) for strike in range(100, 108)]
        plan = build_matrix_selection_plan(contracts, [102.0], [1.0, 5.0])
        adjacent_mids = {
            "100/101": 0.70,
            "101/102": 0.70,
            "102/103": 0.10,
            "103/104": 0.70,
            "104/105": 0.70,
            "105/106": 0.70,
            "106/107": 0.70,
        }
        leg_quotes = _chain_leg_quotes(
            [float(strike) for strike in range(100, 108)],
            adjacent_mids,
            top_leg_mid=0.10,
        )

        snapshots = build_matrix_snapshots(
            plan,
            leg_quotes,
            formula_settings=FormulaSettings(fair_value_edge_width_rate=0.05),
            signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,
            underlying_price=102.5,
        )
        one_wide = snapshots[(102.0, 1.0)]
        five_wide = snapshots[(102.0, 5.0)]

        # The width-5 column only has three spreads of its own, but it derives
        # a bounded fair value from the shared atomic curve.
        self.assertEqual(five_wide.fair_value_edge_curve_fit_points, 6)
        self.assertGreater(five_wide.fair_value_edge_curve_mid, 0.0)
        self.assertLess(five_wide.fair_value_edge_curve_mid, 5.0)
        self.assertAlmostEqual(five_wide.synthetic_mid, 2.90, places=4)
        self.assertAlmostEqual(
            five_wide.fair_value_edge_curve_buy_endpoint,
            five_wide.fair_value_edge_curve_mid - five_wide.fair_value_edge_curve_edge,
            places=4,
        )
        self.assertAlmostEqual(
            five_wide.fair_value_edge_curve_sell_endpoint,
            five_wide.fair_value_edge_curve_mid + five_wide.fair_value_edge_curve_edge,
            places=4,
        )

        five_wide_signal = compute_matrix_signal(five_wide)
        if five_wide_signal is not None:
            self.assertEqual(five_wide_signal.kind, SIGNAL_KIND_OPPORTUNITY)
        one_wide_signal = compute_matrix_signal(one_wide)
        self.assertEqual(one_wide_signal.signal, SIGNAL_BUY)

    def test_matrix_quote_targets_include_all_context_combos(self) -> None:
        contracts = [_option(strike) for strike in range(100, 108)]
        plan = build_matrix_selection_plan(contracts, [102.0, 103.0], [1.0])

        target_labels = [
            pair_key_for_pair(selection.pair)
            for selection in matrix_quote_target_selections(plan)
        ]
        context_labels = [
            pair_key_for_pair(selection.pair)
            for selection in flatten_matrix_context(plan)
        ]

        self.assertEqual(target_labels, context_labels)
        self.assertIn("100/101", target_labels)
        self.assertIn("105/106", target_labels)
        self.assertGreater(len(target_labels), len(plan.visible))

        counts = market_data_quote_counts(matrix_quote_target_selections(plan))
        self.assertEqual(counts.combo_quotes, 6)
        self.assertEqual(counts.leg_quotes, 7)
        self.assertEqual(counts.total, 13)
        self.assertEqual(
            format_market_data_quote_status(matrix_quote_status_for_plans([plan])),
            "Live option streams: 7 legs + 1 underlying | Synthetic spreads monitored: 6",
        )
        self.assertEqual(
            format_market_data_quote_status(
                market_data_quote_status_for_selections(
                    [],
                    matrix_quote_target_selections(plan),
                )
            ),
            "Live option streams: 0 legs + 0 underlying | Synthetic spreads monitored: 0 | "
            "If loaded: 7 option streams, 6 synthetic spreads (+7 streams +6 spreads)",
        )
        self.assertEqual(
            format_market_data_quote_status(None),
            "Live option streams: -- | Synthetic spreads monitored: --",
        )

    def test_compact_market_data_status_shows_live_and_only_real_pending_deltas(self) -> None:
        contracts = [_option(strike) for strike in range(100, 110)]
        current_plan = build_matrix_selection_plan(contracts, [102.0], [1.0])
        replacement_plan = build_matrix_selection_plan(contracts, [104.0], [1.0])

        self.assertEqual(format_compact_market_data_quote_status(None), "LIVE  --")
        self.assertEqual(
            format_compact_market_data_quote_status(
                matrix_quote_status_for_plans([current_plan])
            ),
            "LIVE  6L + 1U • 5S",
        )
        self.assertEqual(
            format_compact_market_data_quote_status(
                market_data_quote_status_for_selections(
                    [],
                    matrix_quote_target_selections(current_plan),
                )
            ),
            "LIVE  0L + 0U • 0S     PENDING  +6L +1U +5S",
        )

        replacement_status = matrix_quote_status_for_plan_update(
            [current_plan],
            current_plan,
            replacement_plan,
        )
        self.assertEqual(
            format_compact_market_data_quote_status(replacement_status),
            "LIVE  6L + 1U • 5S     "
            "PENDING  ADD +2L +0U +2S   REMOVE −2L −0U −2S",
        )

        unchanged_status = matrix_quote_status_for_plan_update(
            [current_plan],
            current_plan,
            current_plan,
        )
        self.assertEqual(
            format_compact_market_data_quote_status(unchanged_status),
            "LIVE  6L + 1U • 5S",
        )

        wider_plan = build_matrix_selection_plan(
            contracts,
            [102.0, 103.0],
            [1.0, 2.0],
        )
        narrower_plan = build_matrix_selection_plan(
            contracts,
            [102.0, 103.0],
            [1.0],
        )
        removal_status = matrix_quote_status_for_plan_update(
            [wider_plan],
            wider_plan,
            narrower_plan,
        )
        removal_text = format_compact_market_data_quote_status(removal_status)
        self.assertIn("PENDING  REMOVE", removal_text)
        self.assertNotIn("PENDING  ADD", removal_text)
        self.assertIn("−0U", removal_text)

    def test_both_tab_quote_targets_union_and_dedupe_side_plans(self) -> None:
        strikes = [float(strike) for strike in range(100, 106)]
        records = [
            *[_anchor_call_option(strike) for strike in strikes],
            *[_option(strike, RIGHT_PUT) for strike in strikes],
        ]
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=MATRIX_VIEW_BOTH,
            low_strike="100",
            high_strike="105",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
            matrix_mode=MATRIX_MODE_REGULAR,
        )
        view_plan = build_matrix_view_plan(records, matrix_input)
        side_states = {
            right: MatrixSideState(right, side_plan.row_strikes, side_plan.plan)
            for right, side_plan in view_plan.side_plans.items()
        }
        tab = MatrixTabState(
            tab_id=1,
            label=matrix_tab_label(matrix_input),
            matrix_input=matrix_input,
            row_strikes=view_plan.row_strikes,
            matrix_plan=build_matrix_selection_plan([], [], []),
            side_states=side_states,
        )

        selections = union_matrix_tab_quote_target_selections([tab])
        request_keys = [selection_request_key(selection) for selection in selections]

        self.assertEqual(len(request_keys), len(set(request_keys)))
        self.assertEqual(
            set(request_keys),
            {
                selection_request_key(selection)
                for selection in union_matrix_quote_target_selections(
                    [side_plan.plan for side_plan in view_plan.side_plans.values()]
                )
            },
        )

    def test_open_matrix_tab_quote_counts_use_union_not_simple_sum(self) -> None:
        contracts = [_option(strike) for strike in range(100, 108)]
        first_plan = build_matrix_selection_plan(contracts, [102.0], [1.0])
        second_plan = build_matrix_selection_plan(contracts, [104.0], [1.0])

        first_counts = market_data_quote_counts(matrix_quote_target_selections(first_plan))
        second_counts = market_data_quote_counts(matrix_quote_target_selections(second_plan))
        union_counts = matrix_quote_target_counts_for_plans([first_plan, second_plan])

        self.assertEqual(first_counts.combo_quotes, 5)
        self.assertEqual(second_counts.combo_quotes, 5)
        self.assertIsNotNone(union_counts)
        self.assertLess(union_counts.combo_quotes, first_counts.combo_quotes + second_counts.combo_quotes)
        self.assertLess(union_counts.leg_quotes, first_counts.leg_quotes + second_counts.leg_quotes)
        self.assertEqual(union_counts.combo_quotes, 7)
        self.assertEqual(union_counts.leg_quotes, 8)
        self.assertEqual(union_counts.total, 15)
        self.assertEqual(
            format_market_data_quote_status(matrix_quote_status_for_plans([first_plan], extra_plan=second_plan)),
            "Live option streams: 6 legs + 1 underlying | Synthetic spreads monitored: 5 | "
            "If loaded: 8 option streams, 7 synthetic spreads (+2 streams +2 spreads)",
        )

    def test_existing_tab_update_quote_status_tracks_added_and_removed_slots(self) -> None:
        contracts = [_option(strike) for strike in range(100, 108)]
        current_plan = build_matrix_selection_plan(contracts, [102.0], [1.0])
        replacement_plan = build_matrix_selection_plan(contracts, [104.0], [1.0])

        status = matrix_quote_status_for_plan_update([current_plan], current_plan, replacement_plan)

        self.assertEqual(status.preview_action, "updated")
        self.assertEqual(status.current.total, 11)
        self.assertEqual(status.preview_total.total, 11)
        self.assertEqual(status.added.combo_quotes, 2)
        self.assertEqual(status.added.leg_quotes, 2)
        self.assertEqual(status.removed.combo_quotes, 2)
        self.assertEqual(status.removed.leg_quotes, 2)
        self.assertEqual(
            format_market_data_quote_status(status),
            "Live option streams: 6 legs + 1 underlying | Synthetic spreads monitored: 5 | "
            "If updated: 6 option streams, 5 synthetic spreads "
            "(+2 streams +2 spreads, -2 streams -2 spreads)",
        )

    def test_existing_tab_width_add_and_remove_quote_status(self) -> None:
        contracts = [_option(strike) for strike in range(100, 110)]
        one_wide_plan = build_matrix_selection_plan(contracts, [102.0, 103.0], [1.0])
        wider_plan = build_matrix_selection_plan(contracts, [102.0, 103.0], [1.0, 2.0])

        add_status = matrix_quote_status_for_plan_update([one_wide_plan], one_wide_plan, wider_plan)
        remove_status = matrix_quote_status_for_plan_update([wider_plan], wider_plan, one_wide_plan)

        self.assertGreater(add_status.added.combo_quotes, 0)
        self.assertGreater(add_status.added.leg_quotes, 0)
        self.assertEqual(add_status.removed.combo_quotes, 0)
        self.assertEqual(add_status.removed.leg_quotes, 0)
        self.assertEqual(remove_status.added.combo_quotes, 0)
        self.assertEqual(remove_status.added.leg_quotes, 0)
        self.assertGreater(remove_status.removed.combo_quotes, 0)
        self.assertGreater(remove_status.removed.leg_quotes, 0)

    def test_existing_tab_update_quote_status_keeps_shared_slots_from_other_tabs(self) -> None:
        contracts = [_option(strike) for strike in range(100, 110)]
        first_plan = build_matrix_selection_plan(contracts, [101.0], [1.0])
        shared_plan = build_matrix_selection_plan(contracts, [102.0], [1.0])
        replacement_plan = build_matrix_selection_plan(contracts, [104.0], [1.0])

        without_shared_tab = matrix_quote_status_for_plan_update(
            [first_plan],
            first_plan,
            replacement_plan,
        )
        with_shared_tab = matrix_quote_status_for_plan_update(
            [first_plan, shared_plan],
            first_plan,
            replacement_plan,
        )

        self.assertLess(
            with_shared_tab.removed.combo_quotes,
            without_shared_tab.removed.combo_quotes,
        )
        self.assertLess(
            with_shared_tab.removed.leg_quotes,
            without_shared_tab.removed.leg_quotes,
        )

    def test_removed_tab_targets_drop_from_union(self) -> None:
        contracts = [_option(strike) for strike in range(100, 108)]
        first_plan = build_matrix_selection_plan(contracts, [102.0], [1.0])
        second_plan = build_matrix_selection_plan(contracts, [104.0], [1.0])

        open_labels = [
            pair_key_for_pair(selection.pair)
            for selection in union_matrix_quote_target_selections([first_plan, second_plan])
        ]
        remaining_labels = [
            pair_key_for_pair(selection.pair)
            for selection in union_matrix_quote_target_selections([first_plan])
        ]

        self.assertIn("106/107", open_labels)
        self.assertNotIn("106/107", remaining_labels)
        self.assertEqual(
            matrix_quote_target_counts_for_plans([first_plan]).combo_quotes,
            5,
        )

    def test_staged_preview_suppressed_for_duplicate_or_max_tab_cases(self) -> None:
        contracts = [_option(strike) for strike in range(100, 108)]
        first_plan = build_matrix_selection_plan(contracts, [102.0], [1.0])
        duplicate_or_blocked_plan = build_matrix_selection_plan(contracts, [104.0], [1.0])

        status = matrix_quote_status_for_plans(
            [first_plan],
            extra_plan=duplicate_or_blocked_plan,
            include_preview=False,
        )

        self.assertEqual(
            format_market_data_quote_status(status),
            "Live option streams: 6 legs + 1 underlying | Synthetic spreads monitored: 5",
        )

    def test_preview_market_data_quote_counts_uses_staged_matrix_input(self) -> None:
        contracts = [_option(strike) for strike in range(100, 108)]
        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike="102",
            high_strike="103",
            width_states={2.0: True, 3.0: False, 4.0: False, 5.0: False},
        )

        counts = preview_market_data_quote_counts(contracts, matrix_input)

        self.assertIsNotNone(counts)
        self.assertEqual(counts.combo_quotes, 5)
        self.assertEqual(counts.leg_quotes, 6)
        self.assertEqual(counts.total, 11)

    def test_seed_cached_matrix_quotes_reuses_matching_session_quotes(self) -> None:
        contracts = [_option(strike) for strike in range(100, 104)]
        plan = build_matrix_selection_plan(contracts, [101.0], [1.0])
        selections = matrix_quote_target_selections(plan)
        first_leg_id = selections[0].legs[0].contract.con_id
        leg_quote = QuoteSnapshot(
            label=str(first_leg_id),
            kind="leg",
            contract_con_id=first_leg_id,
            bid=1.10,
            ask=1.20,
        )
        stale_quote = QuoteSnapshot(label="999001", kind="leg", contract_con_id=999001, bid=0.01, ask=0.02)

        leg_quotes = seed_cached_matrix_quotes(
            selections,
            {
                first_leg_id: leg_quote,
                999001: stale_quote,
            },
        )

        self.assertIs(leg_quotes[first_leg_id], leg_quote)
        self.assertNotIn(999001, leg_quotes)

    def test_cached_quotes_do_not_change_market_data_quote_targets(self) -> None:
        contracts = [_option(strike) for strike in range(100, 104)]
        plan = build_matrix_selection_plan(contracts, [101.0], [1.0])
        selections = matrix_quote_target_selections(plan)
        counts_before = market_data_quote_counts(selections)
        first_leg_id = selections[0].legs[0].contract.con_id

        seed_cached_matrix_quotes(
            selections,
            {
                first_leg_id: QuoteSnapshot(
                    label=str(first_leg_id),
                    kind="leg",
                    contract_con_id=first_leg_id,
                    bid=0.30,
                    ask=0.40,
                )
            },
        )

        self.assertEqual(market_data_quote_counts(selections), counts_before)

    def test_ranked_signals_include_all_edges(self) -> None:
        contracts = [_option(strike) for strike in (100.0, 101.0, 102.0)]
        plan = build_matrix_selection_plan(contracts, [100.0, 101.0], [1.0])

        adjacent_mids = {
            "100/101": -0.02,
            "101/102": 1.08,
        }
        leg_quotes = _chain_leg_quotes([100.0, 101.0, 102.0], adjacent_mids, top_leg_mid=0.50)

        snapshots = build_matrix_snapshots(plan, leg_quotes)
        ranked = build_ranked_matrix_signals(snapshots)

        self.assertEqual(len(ranked), 2)
        self.assertEqual([item.pair_key for item in ranked], ["101/102", "100/101"])
        self.assertEqual(ranked[0].signal.signal, SIGNAL_SELL)
        self.assertAlmostEqual(ranked[0].signal.edge, 0.09)
        self.assertEqual(ranked[1].signal.signal, SIGNAL_BUY)
        self.assertAlmostEqual(ranked[1].signal.edge, 0.03)

    def test_compute_matrix_signal_returns_anomaly_before_opportunity(self) -> None:
        snapshot = _snapshot(100.0, 1.0, 0.50, 0.10)
        snapshot.anomaly_direction = SIGNAL_BUY
        snapshot.anomaly_price = 0.60
        snapshot.opportunity_direction = SIGNAL_SELL
        snapshot.opportunity_price = 0.40

        signal = compute_matrix_signal(snapshot)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.kind, SIGNAL_KIND_ANOMALY)
        self.assertEqual(signal.signal, SIGNAL_BUY)
        self.assertAlmostEqual(signal.edge, 0.10)

    def test_ranked_signals_sort_anomalies_before_opportunities(self) -> None:
        anomaly_snapshot = _snapshot(100.0, 1.0, 0.50, 0.10)
        anomaly_snapshot.anomaly_direction = SIGNAL_BUY
        anomaly_snapshot.anomaly_price = 0.51
        opportunity_snapshot = _snapshot(101.0, 1.0, 0.50, 0.10)
        opportunity_snapshot.opportunity_direction = SIGNAL_SELL
        opportunity_snapshot.opportunity_price = 0.10

        ranked = build_ranked_matrix_signals(
            {
                (100.0, 1.0): anomaly_snapshot,
                (101.0, 1.0): opportunity_snapshot,
            },
        )

        self.assertEqual([item.signal.kind for item in ranked], [SIGNAL_KIND_ANOMALY, SIGNAL_KIND_OPPORTUNITY])
        self.assertLess(ranked[0].signal.edge, ranked[1].signal.edge)

    def test_fair_value_edge_curve_fit_is_bounded_monotone_and_has_sparse_fallback(self) -> None:
        flat_values, flat_error, flat_points = _fit_fair_value_edge_curve(
            [1.0, 2.0, 3.0],
            [0.40, None, None],
        )
        self.assertEqual(flat_points, 1)
        self.assertEqual(flat_error, 0.0)
        self.assertEqual(flat_values, [0.40, 0.40, 0.40])

        fitted_values, fit_error, fit_points = _fit_fair_value_edge_curve(
            [-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0],
            [0.08, 0.16, 0.30, 0.90, 0.70, 0.84, 0.93],
        )
        self.assertEqual(fit_points, 7)
        self.assertIsNotNone(fit_error)
        self.assertTrue(all(value is not None for value in fitted_values))
        numeric_values = [float(value) for value in fitted_values]
        self.assertTrue(all(0.0 <= value <= 1.0 for value in numeric_values))
        self.assertEqual(numeric_values, sorted(numeric_values))
        # The single high center observation does not make the common curve
        # reverse or jump to that observation.
        self.assertLess(numeric_values[3], 0.80)

    def test_fair_value_edge_curve_uses_live_moneyness_then_midpoint_fallback(self) -> None:
        contracts = [_option(float(strike)) for strike in range(100, 106)]
        plan = build_matrix_selection_plan(contracts, [102.0], [1.0])

        live_snapshots = build_matrix_snapshots(
            plan,
            {},
            signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,
            underlying_price=102.5,
        )
        fallback_snapshots = build_matrix_snapshots(
            plan,
            {},
            signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,
        )

        self.assertEqual(live_snapshots[(102.0, 1.0)].fair_value_edge_curve_x, 0.0)
        self.assertEqual(fallback_snapshots[(102.0, 1.0)].fair_value_edge_curve_x, 102.5)

    def test_fair_value_edge_curve_buy_signal_uses_synthetic_mid(self) -> None:
        contracts = [_option(float(strike)) for strike in range(100, 106)]
        plan = build_matrix_selection_plan(contracts, [102.0], [1.0])
        adjacent_mids = {
            "100/101": 0.70,
            "101/102": 0.70,
            "102/103": 0.10,
            "103/104": 0.70,
            "104/105": 0.70,
        }
        leg_quotes = _chain_leg_quotes(
            [float(strike) for strike in range(100, 106)],
            adjacent_mids,
            top_leg_mid=0.10,
        )

        snapshots = build_matrix_snapshots(
            plan,
            leg_quotes,
            formula_settings=FormulaSettings(fair_value_edge_curve_min_points=3),
            signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,
            underlying_price=102.5,
        )
        snapshot = snapshots[(102.0, 1.0)]
        signal = compute_matrix_signal(snapshot, signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE)

        self.assertEqual(snapshot.fair_value_edge_curve_fit_points, 4)
        self.assertEqual(snapshot.fair_value_edge_curve_edge, 0.10)
        self.assertEqual(signal.signal, SIGNAL_BUY)
        self.assertEqual(signal.kind, SIGNAL_KIND_OPPORTUNITY)
        self.assertGreater(signal.edge, 0.0)

    def test_fair_value_edge_curve_put_chain_fits_positive_fair_values(self) -> None:
        contracts = [_option(float(strike), RIGHT_PUT) for strike in range(100, 106)]
        plan = build_matrix_selection_plan(contracts, [102.0], [1.0])
        adjacent_mids = {
            "100/101": 0.10,
            "101/102": 0.20,
            "102/103": 0.30,
            "103/104": 0.40,
            "104/105": 0.50,
        }
        leg_quotes = _put_chain_leg_quotes(
            [float(strike) for strike in range(100, 106)],
            adjacent_mids,
            bottom_leg_mid=0.10,
        )

        snapshots = build_matrix_snapshots(
            plan,
            leg_quotes,
            formula_settings=FormulaSettings(fair_value_edge_curve_min_points=2),
            signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,
            underlying_price=105.5,
        )
        snapshot = snapshots[(102.0, 1.0)]

        self.assertEqual(snapshot.fair_value_edge_curve_fit_points, 3)
        self.assertAlmostEqual(snapshot.synthetic_quote.mid, 0.30, places=4)
        # Regression: put segments must be priced off the upper strike; the
        # bounded fit remains a positive put debit value, not its mirror image
        # below zero (the historical failure that flagged every put as SELL).
        self.assertGreater(snapshot.fair_value_edge_curve_mid, 0.0)
        self.assertLess(snapshot.fair_value_edge_curve_mid, 1.0)
        self.assertAlmostEqual(
            snapshot.fair_value_edge_curve_residual,
            snapshot.synthetic_quote.mid - snapshot.fair_value_edge_curve_mid,
            places=4,
        )

    def test_fair_value_curve_keeps_wide_markets_and_robustly_smooths_shared_leg_seesaw(self) -> None:
        # Regression for the USO recording: leg 107 quoted 10x wider than its
        # neighbors with a skewed mid, depressing 106/107 and inflating
        # 107/108. The synthetic book around each adjacent interval is wider
        # than its full $1 payoff range. It must still calibrate the robust
        # common fit rather than disappearing and collapsing the curve.
        contracts = [_option(float(strike), RIGHT_PUT) for strike in range(100, 109)]
        plan = build_matrix_selection_plan(
            contracts,
            [float(strike) for strike in range(100, 108)],
            [1.0],
        )
        trend_leg_mids = {
            100.0: 1.00, 101.0: 1.10, 102.0: 1.25, 103.0: 1.45, 104.0: 1.70,
            105.0: 2.00, 106.0: 2.35, 107.0: 2.75, 108.0: 3.20,
        }
        leg_quotes: dict[int, QuoteSnapshot] = {}
        for strike, trend_mid in trend_leg_mids.items():
            mid = 2.60 if strike == 107.0 else trend_mid  # skewed -0.15
            half_width = 0.50 if strike == 107.0 else 0.05  # 10x wider market
            con_id = int(round(strike * 1000))
            leg_quotes[con_id] = QuoteSnapshot(
                label=str(con_id),
                kind="leg",
                contract_con_id=con_id,
                bid=round(mid - half_width, 4),
                ask=round(mid + half_width, 4),
                bid_size=10,
                ask_size=10,
                market_data_type=LIVE_MARKET_DATA_TYPE,
            )
        wide_snapshots = build_matrix_snapshots(
            plan,
            leg_quotes,
            signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,
            underlying_price=108.9,
        )
        wide_quote = leg_quotes[int(round(107.0 * 1000))]
        narrow_leg_quotes = dict(leg_quotes)
        narrow_leg_quotes[int(round(107.0 * 1000))] = replace(
            wide_quote,
            bid=2.55,
            ask=2.65,
        )
        narrow_snapshots = build_matrix_snapshots(
            plan,
            narrow_leg_quotes,
            signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,
            underlying_price=108.9,
        )

        wide_106_107 = wide_snapshots[(106.0, 1.0)]
        wide_107_108 = wide_snapshots[(107.0, 1.0)]
        narrow_106_107 = narrow_snapshots[(106.0, 1.0)]
        narrow_107_108 = narrow_snapshots[(107.0, 1.0)]

        self.assertEqual(wide_106_107.fair_value_edge_curve_fit_points, 8)
        self.assertEqual(
            wide_106_107.fair_value_edge_curve_fit_points,
            narrow_106_107.fair_value_edge_curve_fit_points,
        )
        # Book width no longer changes eligibility or base statistical weight;
        # equal mids therefore produce the same fit in narrow and wide books.
        self.assertEqual(
            wide_106_107.fair_value_edge_curve_mid,
            narrow_106_107.fair_value_edge_curve_mid,
        )
        self.assertEqual(
            wide_107_108.fair_value_edge_curve_mid,
            narrow_107_108.fair_value_edge_curve_mid,
        )
        self.assertGreater(wide_106_107.fair_value_edge_curve_mid, 0.0)
        self.assertLess(wide_107_108.fair_value_edge_curve_mid, 1.0)
        # Each candidate has its own leave-one-out fit, so the shared-leg
        # seesaw can reverse their local ordering. Robust fitting still keeps
        # the two independent estimates close instead of collapsing either.
        self.assertLess(
            abs(
                wide_107_108.fair_value_edge_curve_mid
                - wide_106_107.fair_value_edge_curve_mid
            ),
            0.25,
        )

    def test_fair_value_edge_curve_width_rate_changes_endpoints(self) -> None:
        contracts = [_option(float(strike)) for strike in range(100, 106)]
        plan = build_matrix_selection_plan(contracts, [102.0], [1.0])
        adjacent_mids = {
            "100/101": 0.70,
            "101/102": 0.70,
            "102/103": 0.10,
            "103/104": 0.70,
            "104/105": 0.70,
        }
        leg_quotes = _chain_leg_quotes(
            [float(strike) for strike in range(100, 106)],
            adjacent_mids,
            top_leg_mid=0.10,
        )

        snapshots = build_matrix_snapshots(
            plan,
            leg_quotes,
            formula_settings=FormulaSettings(
                fair_value_edge_width_rate=0.20,
                fair_value_edge_curve_min_points=3,
            ),
            signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,
            underlying_price=102.5,
        )
        snapshot = snapshots[(102.0, 1.0)]

        self.assertEqual(snapshot.fair_value_edge_curve_edge, 0.20)
        self.assertAlmostEqual(
            snapshot.fair_value_edge_curve_buy_endpoint,
            snapshot.fair_value_edge_curve_mid - 0.20,
        )
        self.assertAlmostEqual(
            snapshot.fair_value_edge_curve_sell_endpoint,
            snapshot.fair_value_edge_curve_mid + 0.20,
        )

    def test_fair_value_edge_curve_skips_signal_without_synthetic_mid(self) -> None:
        contracts = [_option(float(strike)) for strike in range(100, 106)]
        plan = build_matrix_selection_plan(contracts, [102.0], [1.0])
        adjacent_mids = {
            "100/101": 0.70,
            "101/102": 0.70,
            "102/103": 0.70,
            "103/104": 0.70,
            "104/105": 0.70,
        }
        leg_quotes = _chain_leg_quotes(
            [float(strike) for strike in range(100, 106)],
            adjacent_mids,
            top_leg_mid=0.10,
        )
        # A one-sided 103 leg leaves 102/103 and 103/104 without synthetic quotes.
        broken_con_id = int(round(103.0 * 1000))
        broken = leg_quotes[broken_con_id]
        leg_quotes[broken_con_id] = QuoteSnapshot(
            label=broken.label,
            kind=broken.kind,
            contract_con_id=broken_con_id,
            bid=broken.bid,
            ask=None,
        )

        snapshots = build_matrix_snapshots(
            plan,
            leg_quotes,
            signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,
            underlying_price=102.5,
        )
        snapshot = snapshots[(102.0, 1.0)]

        # Three quoted spreads are below the configured support target. The
        # selected spread therefore has neither a fair value nor a trade.
        self.assertEqual(snapshot.fair_value_edge_curve_fit_points, 3)
        self.assertIsNone(snapshot.fair_value_edge_curve_mid)
        self.assertIsNone(snapshot.opportunity_direction)
        self.assertIsNone(compute_matrix_signal(snapshot, signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE))

    def test_fair_value_edge_curve_leaves_out_own_observation(self) -> None:
        contracts = [_option(float(strike)) for strike in range(100, 106)]
        plan = build_matrix_selection_plan(contracts, [102.0], [1.0])

        def fitted_center(center_mid: float) -> MonitoredSideSnapshot:
            leg_quotes = _chain_leg_quotes(
                [float(strike) for strike in range(100, 106)],
                {
                    "100/101": 0.55,
                    "101/102": 0.62,
                    "102/103": center_mid,
                    "103/104": 0.75,
                    "104/105": 0.82,
                },
                top_leg_mid=0.10,
            )
            return build_matrix_snapshots(
                plan,
                leg_quotes,
                formula_settings=FormulaSettings(fair_value_edge_curve_min_points=3),
                signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,
                underlying_price=102.5,
            )[(102.0, 1.0)]

        baseline = fitted_center(0.69)
        depressed = fitted_center(0.35)

        # The row's direct midpoint does not participate in its own decision
        # fit, so changing only that observation leaves its fair value fixed.
        self.assertEqual(
            depressed.fair_value_edge_curve_mid,
            baseline.fair_value_edge_curve_mid,
        )
        self.assertGreater(depressed.fair_value_edge_curve_mid, 0.35)
        self.assertAlmostEqual(
            depressed.fair_value_edge_curve_residual,
            0.35 - depressed.fair_value_edge_curve_mid,
            places=4,
        )

    def test_fair_value_edge_curve_rejects_physically_impossible_observations(self) -> None:
        contracts = [_option(100.0), _option(101.0), _option(102.0)]
        plan = build_matrix_selection_plan(contracts, [100.0, 101.0], [1.0])
        adjacent_mids = {"100/101": -0.02, "101/102": 1.08}
        leg_quotes = _chain_leg_quotes([100.0, 101.0, 102.0], adjacent_mids, top_leg_mid=0.50)

        snapshots = build_matrix_snapshots(plan, leg_quotes, underlying_price=99.5)
        buy_snapshot = snapshots[(100.0, 1.0)]
        sell_snapshot = snapshots[(101.0, 1.0)]

        # Out-of-bounds debit observations are rejected from the curve. The
        # raw anomaly checks still fire independently on their synthetic mids.
        self.assertEqual(buy_snapshot.fair_value_edge_curve_fit_points, 0)
        self.assertIsNone(buy_snapshot.fair_value_edge_curve_mid)
        self.assertIsNone(buy_snapshot.opportunity_direction)
        self.assertEqual(buy_snapshot.anomaly_direction, SIGNAL_BUY)
        self.assertEqual(sell_snapshot.anomaly_direction, SIGNAL_SELL)

    def test_compute_matrix_signal_returns_anomaly_before_fair_value_edge_curve_opportunity(self) -> None:
        snapshot = _snapshot(100.0, 1.0, 0.50, 0.10)
        snapshot.anomaly_direction = SIGNAL_BUY
        snapshot.anomaly_price = 0.60
        snapshot.opportunity_direction = SIGNAL_SELL
        snapshot.opportunity_price = 0.40

        signal = compute_matrix_signal(snapshot, signal_method=SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.kind, SIGNAL_KIND_ANOMALY)
        self.assertEqual(signal.signal, SIGNAL_BUY)

    def test_underlying_reference_price_uses_mark_mid_then_last(self) -> None:
        self.assertEqual(
            underlying_reference_price(QuoteSnapshot(label="SPY", mark=101.25, bid=100.0, ask=100.5, last=99.0)),
            101.25,
        )
        self.assertEqual(
            underlying_reference_price(QuoteSnapshot(label="SPY", bid=100.0, ask=100.5, last=99.0)),
            100.25,
        )
        self.assertEqual(
            underlying_reference_price(QuoteSnapshot(label="SPY", last=99.0)),
            99.0,
        )
        self.assertIsNone(underlying_reference_price(None))

    def test_underlying_reference_price_skips_nonpositive_and_nonfinite_candidates(self) -> None:
        self.assertEqual(
            underlying_reference_price(
                QuoteSnapshot(
                    label="SPY",
                    mark=float("inf"),
                    bid=100.0,
                    ask=102.0,
                    last=99.0,
                )
            ),
            101.0,
        )
        self.assertEqual(
            underlying_reference_price(
                QuoteSnapshot(
                    label="SPY",
                    mark=0.0,
                    bid=0.0,
                    ask=0.0,
                    last=99.25,
                )
            ),
            99.25,
        )
        self.assertEqual(
            underlying_reference_price(
                QuoteSnapshot(
                    label="SPY",
                    mark=float("nan"),
                    bid=float("nan"),
                    ask=float("nan"),
                    last=98.75,
                )
            ),
            98.75,
        )
        self.assertIsNone(
            underlying_reference_price(
                QuoteSnapshot(label="SPY", mark=-1.0, bid=-2.0, ask=0.0, last=0.0)
            )
        )

    def test_matrix_tab_underlying_reference_price_prefers_live_then_hint(self) -> None:
        tab = _tab(1, "Tab", [])
        tab.underlying_price_hint = 100.50

        self.assertEqual(
            matrix_tab_underlying_reference_price(
                tab,
                QuoteSnapshot(label="SPY", mark=101.25),
            ),
            101.25,
        )
        self.assertEqual(
            matrix_tab_underlying_reference_price(
                tab,
                QuoteSnapshot(label="SPY", mark=0.0, bid=0.0, ask=0.0, last=0.0),
            ),
            100.50,
        )

        tab.underlying_price_hint = float("nan")
        self.assertIsNone(matrix_tab_underlying_reference_price(tab, None))

    def test_rebuild_tab_uses_chain_price_hint_until_live_underlying_arrives(self) -> None:
        strikes = [float(strike) for strike in range(100, 106)]
        contracts = [_option(strike) for strike in strikes]
        plan = build_matrix_selection_plan(contracts, strikes[:-1], [1.0])
        adjacent_mids = {
            pair_key_from_strikes(lower, lower + 1.0): 0.40
            for lower in strikes[:-1]
        }
        tab = _tab(1, "Hinted", [])
        tab.row_strikes = tuple(strikes)
        tab.matrix_plan = plan
        tab.leg_quotes = _chain_leg_quotes(
            strikes,
            adjacent_mids,
            top_leg_mid=0.10,
        )
        tab.underlying_price_hint = 102.5

        class _Harness:
            underlying_quote = None
            formula_settings = FormulaSettings(fair_value_edge_curve_min_points=3)
            signal_method = DEFAULT_SIGNAL_METHOD
            _ensure_tab_selected_cell = OptionsMatrixApp._ensure_tab_selected_cell

        OptionsMatrixApp._rebuild_tab_from_quotes(_Harness(), tab)

        self.assertEqual(set(tab.matrix_snapshots), set(plan.visible))
        self.assertTrue(
            all(
                snapshot.fair_value_edge_curve_mid is not None
                for snapshot in tab.matrix_snapshots.values()
            )
        )

    def test_live_underlying_quote_supersedes_hint_on_next_matrix_refresh(self) -> None:
        strikes = [float(strike) for strike in range(100, 106)]
        contracts = [_option(strike) for strike in strikes]
        plan = build_matrix_selection_plan(contracts, strikes[:-1], [1.0])
        adjacent_mids = {
            pair_key_from_strikes(lower, lower + 1.0): 0.40
            for lower in strikes[:-1]
        }
        tab = _tab(1, "Hinted", [])
        tab.row_strikes = tuple(strikes)
        tab.matrix_plan = plan
        tab.leg_quotes = _chain_leg_quotes(strikes, adjacent_mids, top_leg_mid=0.10)
        tab.underlying_price_hint = 102.5

        class _Harness:
            _rebuild_tab_from_quotes = OptionsMatrixApp._rebuild_tab_from_quotes
            _ensure_tab_selected_cell = OptionsMatrixApp._ensure_tab_selected_cell

            def __init__(self) -> None:
                self.matrix_tabs = [tab]
                self.active_tab_id = tab.tab_id
                self.underlying_quote = None
                self.formula_settings = FormulaSettings(fair_value_edge_curve_min_points=4)
                self.signal_method = DEFAULT_SIGNAL_METHOD
                self.search_result = SimpleNamespace(
                    underlying=SimpleNamespace(symbol="SPY", con_id=77)
                )
                self._dirty_tab_ids: set[int] = set()
                self._quote_redraw_after_id = None
                self.scheduled = False

            def _schedule_quote_redraw(self) -> None:
                self.scheduled = True

            def _sync_view_from_active_tab(self) -> None:
                return

            def _refresh_active_matrix_render(self) -> None:
                return

            def _redraw_selected_detail(self) -> None:
                return

            def _redraw_rank_table_if_changed(self) -> None:
                return

        harness = _Harness()
        OptionsMatrixApp._rebuild_tab_from_quotes(harness, tab)
        self.assertEqual(tab.matrix_snapshots[(102.0, 1.0)].fair_value_edge_curve_x, 0.0)

        OptionsMatrixApp._handle_quote(
            harness,
            QuoteSnapshot(
                label="SPY",
                kind="underlying",
                contract_con_id=77,
                mark=103.0,
            ),
        )
        self.assertTrue(harness.scheduled)
        self.assertEqual(harness._dirty_tab_ids, {tab.tab_id})

        OptionsMatrixApp._refresh_matrix_quotes(harness)

        self.assertEqual(tab.matrix_snapshots[(102.0, 1.0)].fair_value_edge_curve_x, 0.5)
        self.assertEqual(harness._dirty_tab_ids, set())

    def test_selection_has_itm_strike_for_calls_puts_atm_and_unknown_price(self) -> None:
        call_selection = _snapshot(100.0, 1.0, 0.50, 0.10).selection
        self.assertTrue(selection_has_itm_strike(call_selection, 101.0))
        self.assertFalse(selection_has_itm_strike(call_selection, 100.0))
        self.assertFalse(selection_has_itm_strike(call_selection, None))

        put_pair = build_exact_width_vertical_pairs(
            [_option(100.0, RIGHT_PUT), _option(101.0, RIGHT_PUT)],
            1.0,
        )[0]
        put_selection = resolve_vertical_selection(put_pair, DIRECTION_DEBIT)
        self.assertTrue(selection_has_itm_strike(put_selection, 100.0))
        self.assertFalse(selection_has_itm_strike(put_selection, 101.0))

    def test_aggregate_rankings_include_all_tabs_with_global_ranks(self) -> None:
        low_edge = _ranked_signal(1, "99/100", 1.0, SIGNAL_BUY, 0.30, 0.03)
        high_edge = _ranked_signal(1, "100/101", 1.0, SIGNAL_SELL, 0.90, 0.09)

        rows = build_aggregate_ranked_signals(
            [_tab(1, "Jan17'30 C 99-100", [low_edge]), _tab(2, "Jan17'30 C 100-101", [high_edge])],
            underlying_price=99.0,
        )

        self.assertEqual([row.rank for row in rows], [1, 2])
        self.assertEqual([row.tab_id for row in rows], [2, 1])
        self.assertEqual([row.signal.edge for row in rows], [0.09, 0.03])

    def test_aggregate_rankings_wait_for_reference_price_then_exclude_itm_signals(self) -> None:
        itm_signal = _ranked_signal(1, "99/100", 1.0, SIGNAL_BUY, 0.30, 0.03)
        otm_signal = _ranked_signal(1, "101/102", 1.0, SIGNAL_SELL, 0.90, 0.09)
        tab = _tab(1, "Jan17'30 C 99-102", [itm_signal, otm_signal])

        unknown_price_rows = build_aggregate_ranked_signals([tab], underlying_price=None)
        tab.underlying_price_hint = 100.5
        hinted_price_rows = build_aggregate_ranked_signals([tab], underlying_price=None)
        known_price_rows = build_aggregate_ranked_signals([tab], underlying_price=100.5)

        self.assertEqual(unknown_price_rows, [])
        self.assertEqual([row.pair_key for row in hinted_price_rows], ["101/102"])
        self.assertEqual([row.pair_key for row in known_price_rows], ["101/102"])

    def test_both_aggregate_filters_call_and_put_itm_rows_independently(self) -> None:
        def ranked(right: str, lower: float, edge: float) -> RankedMatrixSignal:
            pair = build_exact_width_vertical_pairs(
                [_option(lower, right), _option(lower + 1.0, right)],
                1.0,
            )[0]
            selection = resolve_vertical_selection(pair, DIRECTION_DEBIT)
            snapshot = MonitoredSideSnapshot(
                title=pair.label,
                pair_key=pair_key_for_pair(pair),
                selection=selection,
            )
            return RankedMatrixSignal(
                rank=1,
                lower_strike=lower,
                width=1.0,
                pair_key=pair_key_for_pair(pair),
                selection=selection,
                snapshot=snapshot,
                signal=MatrixSignal(
                    signal=SIGNAL_BUY,
                    price=0.25,
                    edge=edge,
                    kind=SIGNAL_KIND_OPPORTUNITY,
                ),
            )

        matrix_input = parse_matrix_input_state(
            display_expiration="2030-01-17",
            raw_expiration="20300117",
            side=MATRIX_VIEW_BOTH,
            low_strike="100",
            high_strike="103",
            width_states={2.0: False, 3.0: False, 4.0: False, 5.0: False},
            ticker_symbol="SPY",
        )
        tab = MatrixTabState(
            tab_id=1,
            label=matrix_tab_label(matrix_input),
            matrix_input=matrix_input,
            row_strikes=(100.0, 101.0, 102.0, 103.0),
            matrix_plan=build_matrix_selection_plan([], [], []),
            ranked_signals=[
                ranked(RIGHT_CALL, 100.0, 0.40),
                ranked(RIGHT_CALL, 102.0, 0.30),
                ranked(RIGHT_PUT, 100.0, 0.20),
                ranked(RIGHT_PUT, 102.0, 0.10),
            ],
            side_states={
                RIGHT_PUT: MatrixSideState(RIGHT_PUT, (), build_matrix_selection_plan([], [], [])),
                RIGHT_CALL: MatrixSideState(RIGHT_CALL, (), build_matrix_selection_plan([], [], [])),
            },
        )

        rows = build_aggregate_ranked_signals([tab], underlying_price=101.5)

        self.assertEqual(
            [(row.selection.pair.right, row.pair_key) for row in rows],
            [(RIGHT_CALL, "102/103"), (RIGHT_PUT, "100/101")],
        )

    def test_aggregate_rank_table_remains_visible_when_active_tab_is_stale(self) -> None:
        signal = _ranked_signal(1, "102/103", 1.0, SIGNAL_BUY, 0.30, 0.05)
        live_tab = _tab(1, "Live", [signal])
        stale_tab = _tab(2, "Stale", [])
        stale_tab.is_stale = True
        aggregate = build_aggregate_ranked_signals([live_tab], underlying_price=100.0)

        class _Tree:
            def __init__(self) -> None:
                self.inserted = []

            def get_children(self):
                return ()

            def delete(self, _item) -> None:
                return

            def insert(self, _parent, _position, *, values):
                self.inserted.append(values)
                return f"row-{len(self.inserted)}"

        class _Harness:
            def __init__(self) -> None:
                self.rank_tree = _Tree()
                self.rank_item_cells = {}
                self.ranked_signals = aggregate
                self.rank_sort_state = RankSortState()
                self.matrix_tabs = [live_tab, stale_tab]

            def _redraw_rank_headings(self) -> None:
                return

        harness = _Harness()
        OptionsMatrixApp._redraw_rank_table(harness)

        self.assertEqual(len(harness.rank_tree.inserted), 1)
        self.assertEqual(harness.rank_tree.inserted[0][2], RIGHT_CALL)

    def test_buy_signal_means_debit_and_sell_signal_means_credit_action(self) -> None:
        contracts = [_option(100.0), _option(101.0)]
        pair = build_exact_width_vertical_pairs(contracts, 1.0)[0]
        selection = resolve_vertical_selection(pair, DIRECTION_DEBIT)
        plan = build_matrix_selection_plan(contracts, [100.0], [1.0])
        leg_quotes = _chain_leg_quotes([100.0, 101.0], {"100/101": -0.02}, top_leg_mid=0.50)

        snapshots = build_matrix_snapshots(plan, leg_quotes)
        signal = compute_matrix_signal(snapshots[(100.0, 1.0)])

        self.assertEqual([(leg.action, leg.contract.strike) for leg in selection.legs], [("BUY", 100.0), ("SELL", 101.0)])
        self.assertEqual(signal.signal, SIGNAL_BUY)
        self.assertAlmostEqual(signal.edge, 0.03)


if __name__ == "__main__":
    unittest.main()
