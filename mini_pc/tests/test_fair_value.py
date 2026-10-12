from __future__ import annotations

import unittest
from dataclasses import replace

from options_matrix.models import (
    DIRECTION_DEBIT,
    FormulaSettings,
    RIGHT_CALL,
    RIGHT_PUT,
    SIGNAL_BUY,
    MatrixSelectionPlan,
    MonitoredSideSnapshot,
    OptionContractRecord,
    QuoteSnapshot,
    VerticalPair,
    _apply_fair_value_edge_curve,
    _base_curve_segments,
    _fit_fair_value_edge_curve,
    _qualified_vertical_observation,
    build_matrix_context_snapshots,
    build_matrix_selection_plan,
    build_matrix_snapshots,
    fair_value_edge_for_width,
    flatten_matrix_context,
    resolve_vertical_selection,
)


def _option(strike: float, right: str) -> OptionContractRecord:
    side_offset = 100_000 if right == RIGHT_CALL else 200_000
    return OptionContractRecord(
        con_id=side_offset + int(round(strike * 100)),
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


def _quotes_for_adjacent_mids(
    records: list[OptionContractRecord],
    adjacent_mids: list[float],
) -> dict[int, QuoteSnapshot]:
    ordered = sorted(records, key=lambda record: record.strike)
    if ordered[0].right == RIGHT_CALL:
        mids = {ordered[-1].strike: 1.0}
        for index in range(len(ordered) - 2, -1, -1):
            mids[ordered[index].strike] = (
                mids[ordered[index + 1].strike] + adjacent_mids[index]
            )
    else:
        mids = {ordered[0].strike: 1.0}
        for index in range(1, len(ordered)):
            mids[ordered[index].strike] = (
                mids[ordered[index - 1].strike] + adjacent_mids[index - 1]
            )
    return {
        record.con_id: QuoteSnapshot(
            label=str(record.con_id),
            contract_con_id=record.con_id,
            bid=round(mids[record.strike] - 0.01, 4),
            ask=round(mids[record.strike] + 0.01, 4),
        )
        for record in ordered
    }


class FairValuePolicyTestCase(unittest.TestCase):
    def test_all_widths_ending_in_half_are_excluded_from_generated_and_manual_plans(self) -> None:
        records = [_option(100.0 + index * 0.5, RIGHT_CALL) for index in range(13)]
        quotes = _quotes_for_adjacent_mids(records, [0.20] * 12)
        excluded_widths = [0.5, 1.5, 2.5, 3.5, 4.5]
        plan = build_matrix_selection_plan(
            records,
            [record.strike for record in records],
            [0.5 * index for index in range(1, 11)],
            context_neighbors=2,
        )
        self.assertTrue(all(width not in plan.context_by_width for width in excluded_widths))
        self.assertTrue(plan.visible)
        self.assertTrue(all(width % 1.0 != 0.5 for _strike, width in plan.visible))
        self.assertTrue(all(selection.pair.width % 1.0 != 0.5 for selection in flatten_matrix_context(plan)))
        self.assertIn((100.5, 1.0), plan.visible)
        self.assertIn((100.5, 5.0), plan.visible)

        excluded_pairs = [
            VerticalPair(right=RIGHT_CALL, lower=records[0], upper=records[int(width * 2)])
            for width in excluded_widths
        ]
        excluded_selections = [resolve_vertical_selection(pair, DIRECTION_DEBIT) for pair in excluded_pairs]
        supported_selection = plan.visible[(100.0, 1.0)]
        manual_plan = MatrixSelectionPlan(
            visible={
                **{(100.0, selection.pair.width): selection for selection in excluded_selections},
                (100.0, 1.0): supported_selection,
            },
            context_by_width={
                **{selection.pair.width: (selection,) for selection in excluded_selections},
                1.0: (*excluded_selections, supported_selection),
            },
        )
        self.assertEqual(flatten_matrix_context(manual_plan), [supported_selection])
        context = build_matrix_context_snapshots(manual_plan, quotes, underlying_price=99.0)
        self.assertTrue(all(snapshot.width % 1.0 != 0.5 for group in context.values() for snapshot in group))
        self.assertEqual(set(build_matrix_snapshots(manual_plan, quotes, underlying_price=99.0)), {(100.0, 1.0)})
        for pair in excluded_pairs:
            with self.subTest(excluded_width=pair.width):
                self.assertIsNone(_qualified_vertical_observation(pair, quotes))

    def test_half_width_only_snapshots_cannot_supply_fair_value_support(self) -> None:
        records = [_option(100.0 + index * 0.5, RIGHT_CALL) for index in range(17)]
        quotes = _quotes_for_adjacent_mids(records, [0.20] * 16)
        snapshots = [
            MonitoredSideSnapshot(
                title="Excluded width ending in half",
                pair_key=f"{lower.strike}/{upper.strike}",
                selection=resolve_vertical_selection(
                    VerticalPair(right=RIGHT_CALL, lower=lower, upper=upper),
                    DIRECTION_DEBIT,
                ),
                is_center=True,
            )
            for distance in (1, 3, 5, 7, 9)
            for lower, upper in zip(records, records[distance:])
        ]
        self.assertEqual(_base_curve_segments(snapshots, quotes, 99.0), [])
        _apply_fair_value_edge_curve(snapshots, quotes, 99.0)
        self.assertTrue(all(snapshot.fair_value_edge_curve_fit_points == 0 for snapshot in snapshots))
        self.assertTrue(all(snapshot.fair_value_edge_curve_mid is None for snapshot in snapshots))
        self.assertTrue(all(snapshot.opportunity_direction is None for snapshot in snapshots))

    def test_half_step_chains_have_supported_additive_fair_values_for_wider_pairs(self) -> None:
        strikes = [100.0 + index * 0.5 for index in range(17)]
        for right, spot in ((RIGHT_CALL, 99.0), (RIGHT_PUT, 109.0)):
            with self.subTest(right=right):
                records = [_option(strike, right) for strike in strikes]
                adjacent = [0.40 - index * 0.02 for index in range(16)]
                if right == RIGHT_PUT:
                    adjacent.reverse()
                quotes = _quotes_for_adjacent_mids(records, adjacent)
                plan = build_matrix_selection_plan(
                    records, strikes, [0.5 * index for index in range(1, 11)], context_neighbors=0
                )
                snapshots = build_matrix_snapshots(plan, quotes, underlying_price=spot)
                self.assertIn((100.5, 1.0), snapshots)
                self.assertIn((100.5, 5.0), snapshots)
                for (_strike, width), snapshot in snapshots.items():
                    self.assertNotEqual(width % 1.0, 0.5)
                    self.assertIsNotNone(snapshot.fair_value_edge_curve_mid)
                    self.assertEqual(snapshot.fair_value_edge_curve_fit_points, 8)
                    self.assertGreater(float(snapshot.fair_value_edge_curve_mid), 0.0)
                    self.assertLess(float(snapshot.fair_value_edge_curve_mid), width)
                self.assertAlmostEqual(
                    snapshots[(100.5, 3.0)].fair_value_edge_curve_mid,
                    snapshots[(100.5, 1.0)].fair_value_edge_curve_mid
                    + snapshots[(101.5, 2.0)].fair_value_edge_curve_mid,
                    places=4,
                )
                self.assertAlmostEqual(
                    snapshots[(102.0, 3.0)].fair_value_edge_curve_mid,
                    sum(snapshots[(float(strike), 1.0)].fair_value_edge_curve_mid for strike in range(102, 105)),
                    places=4,
                )

    def test_skipped_half_strike_quote_changes_quotes_without_changing_fair_values(self) -> None:
        strikes = [100.0 + index * 0.5 for index in range(17)]
        for right, spot in ((RIGHT_CALL, 99.0), (RIGHT_PUT, 109.0)):
            with self.subTest(right=right):
                records = [_option(strike, right) for strike in strikes]
                quotes = _quotes_for_adjacent_mids(records, [0.35 - index * 0.01 for index in range(16)])
                plan = build_matrix_selection_plan(
                    records, strikes, [1.0, 2.0, 3.0], context_neighbors=0
                )
                before = build_matrix_snapshots(plan, quotes, underlying_price=spot)
                changed_quotes = dict(quotes)
                changed_id = records[5].con_id  # 102.5 is inside the 102/103 calibration interval.
                original_quote = quotes[changed_id]
                changed_quotes[changed_id] = replace(
                    original_quote,
                    bid=original_quote.bid + 0.10,
                    ask=original_quote.ask + 0.10,
                )
                after = build_matrix_snapshots(plan, changed_quotes, underlying_price=spot)
                self.assertNotEqual(before[(102.5, 1.0)].synthetic_mid, after[(102.5, 1.0)].synthetic_mid)
                self.assertEqual(before[(102.0, 1.0)].synthetic_mid, after[(102.0, 1.0)].synthetic_mid)
                for key in before:
                    self.assertIsNotNone(before[key].fair_value_edge_curve_mid)
                    self.assertEqual(before[key].fair_value_edge_curve_mid, after[key].fair_value_edge_curve_mid)
                    self.assertEqual(before[key].fair_value_edge_curve_fit_points, after[key].fair_value_edge_curve_fit_points)
                    self.assertEqual(before[key].fair_value_edge_curve_fit_error, after[key].fair_value_edge_curve_fit_error)

    def test_half_step_chain_prices_an_odd_tail_without_fitting_an_excluded_width(self) -> None:
        strikes = [100.0 + index * 0.5 for index in range(18)]
        for right, spot in ((RIGHT_CALL, 99.0), (RIGHT_PUT, 109.0)):
            with self.subTest(right=right):
                records = [_option(strike, right) for strike in strikes]
                quotes = _quotes_for_adjacent_mids(records, [0.30 - index * 0.005 for index in range(17)])
                plan = build_matrix_selection_plan(records, strikes, [1.0, 2.0, 3.0], context_neighbors=0)
                snapshots = build_matrix_snapshots(plan, quotes, underlying_price=spot)
                segments = _base_curve_segments(list(snapshots.values()), quotes, spot)
                self.assertTrue(all(segment.width == 1.0 for segment in segments))
                self.assertEqual((segments[-1].lower_strike, segments[-1].upper_strike), (107.0, 108.0))
                self.assertTrue(all(left.upper_strike == right.lower_strike for left, right in zip(segments, segments[1:])))
                tail = snapshots[(107.5, 1.0)]
                self.assertIsNotNone(tail.fair_value_edge_curve_mid)
                self.assertEqual(tail.fair_value_edge_curve_fit_points, 8)
                self.assertGreater(float(tail.fair_value_edge_curve_mid), 0.0)
                self.assertLess(float(tail.fair_value_edge_curve_mid), 1.0)
                self.assertAlmostEqual(tail.fair_value_edge_curve_mid, snapshots[(107.0, 1.0)].fair_value_edge_curve_mid, places=4)
                self.assertAlmostEqual(
                    snapshots[(106.5, 2.0)].fair_value_edge_curve_mid,
                    snapshots[(106.5, 1.0)].fair_value_edge_curve_mid + tail.fair_value_edge_curve_mid,
                    places=4,
                )
                changed_quotes = dict(quotes)
                tail_id = records[-1].con_id
                changed_quotes[tail_id] = replace(
                    quotes[tail_id], bid=quotes[tail_id].bid + 0.10, ask=quotes[tail_id].ask + 0.10
                )
                changed = build_matrix_snapshots(plan, changed_quotes, underlying_price=spot)
                self.assertNotEqual(tail.synthetic_mid, changed[(107.5, 1.0)].synthetic_mid)
                for key, snapshot in snapshots.items():
                    self.assertEqual(snapshot.fair_value_edge_curve_mid, changed[key].fair_value_edge_curve_mid)
                    self.assertEqual(snapshot.fair_value_edge_curve_fit_points, changed[key].fair_value_edge_curve_fit_points)
                    self.assertEqual(snapshot.fair_value_edge_curve_fit_error, changed[key].fair_value_edge_curve_fit_error)

    def test_one_and_half_strike_gaps_use_only_supported_three_wide_observations(self) -> None:
        strikes = [100.0 + index * 1.5 for index in range(14)]
        records = [_option(strike, RIGHT_CALL) for strike in strikes]
        quotes = _quotes_for_adjacent_mids(records, [0.80 - index * 0.03 for index in range(13)])
        plan = build_matrix_selection_plan(
            records, strikes, [1.5, 3.0, 4.5, 6.0, 7.5, 9.0], context_neighbors=0
        )
        snapshots = build_matrix_snapshots(plan, quotes, underlying_price=99.0)
        segments = _base_curve_segments(list(snapshots.values()), quotes, 99.0)
        self.assertEqual(len(segments), 6)
        self.assertTrue(all(segment.width == 3.0 for segment in segments))
        self.assertEqual(segments[-1].upper_strike, 118.0)
        self.assertIn((116.5, 3.0), snapshots)
        for (_strike, width), snapshot in snapshots.items():
            self.assertNotEqual(width % 1.0, 0.5)
            self.assertEqual(snapshot.fair_value_edge_curve_fit_points, 6)
            self.assertIsNotNone(snapshot.fair_value_edge_curve_mid)
            self.assertGreater(float(snapshot.fair_value_edge_curve_mid), 0.0)
            self.assertLess(float(snapshot.fair_value_edge_curve_mid), width)
        self.assertAlmostEqual(
            snapshots[(101.5, 6.0)].fair_value_edge_curve_mid,
            snapshots[(101.5, 3.0)].fair_value_edge_curve_mid + snapshots[(104.5, 3.0)].fair_value_edge_curve_mid,
            places=4,
        )

    def test_other_fractional_widths_remain_eligible_for_quotes_and_fair_value(self) -> None:
        records = [_option(100.0 + index * 0.25, RIGHT_CALL) for index in range(9)]
        quotes = _quotes_for_adjacent_mids(records, [0.10] * 8)
        plan = build_matrix_selection_plan(
            records, [record.strike for record in records], [0.25, 0.5, 0.75, 1.0], context_neighbors=0
        )
        snapshots = build_matrix_snapshots(plan, quotes, underlying_price=99.0)
        self.assertIn((100.0, 0.25), snapshots)
        self.assertIn((100.25, 0.75), snapshots)
        self.assertNotIn((100.0, 0.5), snapshots)
        self.assertIsNotNone(snapshots[(100.0, 0.25)].fair_value_edge_curve_mid)
        self.assertIsNotNone(snapshots[(100.25, 0.75)].fair_value_edge_curve_mid)
        segments = _base_curve_segments(list(snapshots.values()), quotes, 99.0)
        self.assertEqual(len(segments), 8)
        self.assertTrue(all(segment.width == 0.25 and segment.density is not None for segment in segments))

    def test_edge_schedule_uses_configurable_knots_and_fractional_interpolation(self) -> None:
        self.assertEqual(
            [fair_value_edge_for_width(width) for width in range(1, 6)],
            [0.10, 0.14, 0.17, 0.20, 0.22],
        )
        self.assertEqual(fair_value_edge_for_width(0.5), 0.10)
        self.assertEqual(fair_value_edge_for_width(5.5), 0.22)
        self.assertEqual(fair_value_edge_for_width(1.5), 0.12)
        self.assertEqual(fair_value_edge_for_width(4.5), 0.21)

        custom = FormulaSettings(
            fair_value_edge_width_rate=0.08,
            fair_value_edge_width_2=0.12,
            fair_value_edge_width_3=0.15,
            fair_value_edge_width_4=0.19,
            fair_value_edge_width_5=0.25,
        )
        self.assertEqual(fair_value_edge_for_width(1.0, custom), 0.08)
        self.assertEqual(fair_value_edge_for_width(2.5, custom), 0.135)
        self.assertEqual(fair_value_edge_for_width(6.0, custom), 0.25)

        # Numeric-base callers keep the previous square-root behavior.
        self.assertEqual(fair_value_edge_for_width(4.0, 0.05), 0.10)
        self.assertEqual(fair_value_edge_for_width(0.5, base_rate=0.10), 0.07)

    def test_visible_snapshot_loads_without_context_quotes_or_underlying(self) -> None:
        lower = _option(100.0, RIGHT_CALL)
        upper = _option(101.0, RIGHT_CALL)
        selection = resolve_vertical_selection(
            VerticalPair(right=RIGHT_CALL, lower=lower, upper=upper),
            DIRECTION_DEBIT,
        )
        plan = MatrixSelectionPlan(
            visible={(100.0, 1.0): selection},
            context_by_width={},
        )

        snapshots = build_matrix_snapshots(plan, {}, underlying_price=None)

        self.assertEqual(set(snapshots), {(100.0, 1.0)})
        self.assertIsNone(snapshots[(100.0, 1.0)].synthetic_quote)
        self.assertEqual(snapshots[(100.0, 1.0)].fair_value_edge_curve_fit_points, 0)
        self.assertIsNone(snapshots[(100.0, 1.0)].fair_value_edge_curve_mid)

    def test_same_side_pools_exclude_deep_itm_quotes(self) -> None:
        strikes = [float(strike) for strike in range(100, 109)]
        calls = [_option(strike, RIGHT_CALL) for strike in strikes]
        puts = [_option(strike, RIGHT_PUT) for strike in strikes]
        # The first direct call interval is deliberately wrong.  It is deep
        # ITM at spot 103.5 and must remain an evaluation quote, not a fit input.
        call_adjacent = [0.05, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.20]
        put_adjacent = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]
        quotes = _quotes_for_adjacent_mids(calls, call_adjacent)
        quotes.update(_quotes_for_adjacent_mids(puts, put_adjacent))

        call_plan = build_matrix_selection_plan(
            calls,
            strikes[:-1],
            [1.0, 2.0],
            context_neighbors=0,
        )
        put_plan = build_matrix_selection_plan(
            puts,
            strikes[:-1],
            [1.0],
            context_neighbors=0,
        )

        settings = FormulaSettings(
            fair_value_edge_curve_min_points=4,
            fair_value_edge_width_2=0.33,
        )
        call_snapshots = build_matrix_snapshots(
            call_plan,
            quotes,
            underlying_price=103.5,
            formula_settings=settings,
        )
        put_snapshots = build_matrix_snapshots(
            put_plan,
            quotes,
            underlying_price=103.5,
            formula_settings=settings,
        )

        # The sides have independent eligible pools: four OTM calls plus the
        # same-side crossing and bridge, versus three OTM puts plus theirs.
        self.assertEqual(call_snapshots[(103.0, 1.0)].fair_value_edge_curve_fit_points, 6)
        self.assertEqual(put_snapshots[(103.0, 1.0)].fair_value_edge_curve_fit_points, 5)

        corrected_quotes = _quotes_for_adjacent_mids(
            calls,
            [0.95, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.20],
        )
        corrected = build_matrix_snapshots(
            call_plan,
            corrected_quotes,
            underlying_price=103.5,
            formula_settings=settings,
        )
        # Changing only the deep-ITM 100/101 market changes its displayed MID,
        # but cannot bend any CALL fair value because it is not calibrated.
        self.assertNotEqual(
            call_snapshots[(100.0, 1.0)].synthetic_mid,
            corrected[(100.0, 1.0)].synthetic_mid,
        )
        self.assertEqual(
            call_snapshots[(104.0, 1.0)].fair_value_edge_curve_mid,
            corrected[(104.0, 1.0)].fair_value_edge_curve_mid,
        )
        self.assertAlmostEqual(
            call_snapshots[(102.0, 2.0)].fair_value_edge_curve_mid,
            call_snapshots[(102.0, 1.0)].fair_value_edge_curve_mid
            + call_snapshots[(103.0, 1.0)].fair_value_edge_curve_mid,
            places=4,
        )
        self.assertEqual(call_snapshots[(102.0, 2.0)].fair_value_edge_curve_edge, 0.33)

    def test_atomic_fair_value_leaves_out_its_own_direct_mid(self) -> None:
        strikes = [float(strike) for strike in range(100, 107)]
        calls = [_option(strike, RIGHT_CALL) for strike in strikes]
        plan = build_matrix_selection_plan(
            calls,
            strikes[:-1],
            [1.0],
            context_neighbors=0,
        )

        def target_snapshot(target_mid: float):
            adjacent_mids = [0.75, 0.65, target_mid, 0.45, 0.35, 0.25]
            return build_matrix_snapshots(
                plan,
                _quotes_for_adjacent_mids(calls, adjacent_mids),
                underlying_price=99.5,
            )[(102.0, 1.0)]

        baseline = target_snapshot(0.55)
        dislocated = target_snapshot(0.05)

        self.assertEqual(baseline.fair_value_edge_curve_fit_points, 6)
        self.assertEqual(dislocated.fair_value_edge_curve_fit_points, 6)
        self.assertEqual(
            dislocated.fair_value_edge_curve_mid,
            baseline.fair_value_edge_curve_mid,
        )
        self.assertIsNone(baseline.opportunity_direction)
        self.assertEqual(dislocated.opportunity_direction, SIGNAL_BUY)
        self.assertNotEqual(
            dislocated.fair_value_edge_curve_fit_error,
            baseline.fair_value_edge_curve_fit_error,
        )
        self.assertAlmostEqual(
            dislocated.fair_value_edge_curve_residual,
            0.05 - float(dislocated.fair_value_edge_curve_mid),
            places=4,
        )

    def test_leave_one_out_requires_configured_remaining_support(self) -> None:
        strikes = [float(strike) for strike in range(100, 106)]
        calls = [_option(strike, RIGHT_CALL) for strike in strikes]
        plan = build_matrix_selection_plan(
            calls,
            strikes[:-1],
            [1.0, 2.0],
            context_neighbors=0,
        )
        snapshots = build_matrix_snapshots(
            plan,
            _quotes_for_adjacent_mids(calls, [0.70, 0.60, 0.50, 0.40, 0.30]),
            underlying_price=99.5,
        )

        atomic = snapshots[(102.0, 1.0)]
        wider = snapshots[(102.0, 2.0)]
        self.assertEqual(atomic.fair_value_edge_curve_fit_points, 5)
        self.assertIsNone(atomic.fair_value_edge_curve_mid)
        self.assertIsNone(atomic.opportunity_direction)
        self.assertIsNone(wider.fair_value_edge_curve_mid)
        self.assertIsNone(wider.opportunity_direction)

    def test_opposite_side_mid_never_enters_leave_one_out_fit(self) -> None:
        strikes = [float(strike) for strike in range(100, 107)]
        calls = [_option(strike, RIGHT_CALL) for strike in strikes]
        puts = [_option(strike, RIGHT_PUT) for strike in strikes]
        plan = build_matrix_selection_plan(
            calls,
            strikes[:-1],
            [1.0],
            context_neighbors=0,
        )

        def target_snapshot(call_mid: float, put_mid: float):
            quotes = _quotes_for_adjacent_mids(
                calls,
                [0.80, 0.70, call_mid, 0.50, 0.40, 0.30],
            )
            quotes.update(
                _quotes_for_adjacent_mids(
                    puts,
                    [0.20, 0.30, put_mid, 0.50, 0.60, 0.70],
                )
            )
            return build_matrix_snapshots(
                plan,
                quotes,
                formula_settings=FormulaSettings(fair_value_edge_curve_min_points=3),
                underlying_price=102.5,
            )[(102.0, 1.0)]

        baseline = target_snapshot(0.60, 0.40)
        direct_dislocation = target_snapshot(0.10, 0.40)
        opposite_dislocation = target_snapshot(0.10, 0.10)

        self.assertEqual(
            direct_dislocation.fair_value_edge_curve_mid,
            baseline.fair_value_edge_curve_mid,
        )
        self.assertEqual(
            opposite_dislocation.fair_value_edge_curve_mid,
            direct_dislocation.fair_value_edge_curve_mid,
        )
        self.assertEqual(
            opposite_dislocation.fair_value_edge_curve_fit_error,
            direct_dislocation.fair_value_edge_curve_fit_error,
        )

    def test_noncalibration_atomic_uses_only_a_supported_full_reference_fit(self) -> None:
        strikes = [float(strike) for strike in range(100, 109)]
        calls = [_option(strike, RIGHT_CALL) for strike in strikes]
        plan = build_matrix_selection_plan(
            calls,
            strikes[:-1],
            [1.0],
            context_neighbors=0,
        )
        quotes = _quotes_for_adjacent_mids(
            calls,
            [0.95, 0.85, 0.75, 0.65, 0.55, 0.45, 0.35, 0.25],
        )

        probe = build_matrix_snapshots(
            plan,
            quotes,
            formula_settings=FormulaSettings(fair_value_edge_curve_min_points=2),
            underlying_price=103.5,
        )
        full_support = probe[(100.0, 1.0)].fair_value_edge_curve_fit_points
        self.assertGreaterEqual(full_support, 2)

        supported = build_matrix_snapshots(
            plan,
            quotes,
            formula_settings=FormulaSettings(
                fair_value_edge_curve_min_points=full_support,
            ),
            underlying_price=103.5,
        )
        self.assertIsNotNone(supported[(100.0, 1.0)].fair_value_edge_curve_mid)
        self.assertIsNone(supported[(104.0, 1.0)].fair_value_edge_curve_mid)

        unsupported = build_matrix_snapshots(
            plan,
            quotes,
            formula_settings=FormulaSettings(
                fair_value_edge_curve_min_points=full_support + 1,
            ),
            underlying_price=103.5,
        )
        self.assertIsNone(unsupported[(100.0, 1.0)].fair_value_edge_curve_mid)

    def test_wider_fair_values_sum_atomic_leave_one_out_values(self) -> None:
        strikes = [float(strike) for strike in range(100, 107)]
        calls = [_option(strike, RIGHT_CALL) for strike in strikes]
        plan = build_matrix_selection_plan(
            calls,
            strikes[:-1],
            [1.0, 2.0, 3.0],
            context_neighbors=0,
        )
        snapshots = build_matrix_snapshots(
            plan,
            _quotes_for_adjacent_mids(calls, [0.75, 0.65, 0.20, 0.45, 0.35, 0.25]),
            underlying_price=99.5,
        )

        self.assertAlmostEqual(
            snapshots[(101.0, 3.0)].fair_value_edge_curve_mid,
            snapshots[(101.0, 1.0)].fair_value_edge_curve_mid
            + snapshots[(102.0, 1.0)].fair_value_edge_curve_mid
            + snapshots[(103.0, 1.0)].fair_value_edge_curve_mid,
            places=4,
        )

    def test_same_side_bridge_reselects_when_spot_moves(self) -> None:
        strikes = [float(strike) for strike in range(100, 109)]
        calls = [_option(strike, RIGHT_CALL) for strike in strikes]
        call_adjacent = [0.90, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.20]
        quotes = _quotes_for_adjacent_mids(calls, call_adjacent)
        plan = build_matrix_selection_plan(
            calls,
            strikes[:-1],
            [1.0],
            context_neighbors=0,
        )

        settings = FormulaSettings(fair_value_edge_curve_min_points=4)
        before = build_matrix_snapshots(
            plan,
            quotes,
            underlying_price=103.5,
            formula_settings=settings,
        )
        after = build_matrix_snapshots(
            plan,
            quotes,
            underlying_price=104.5,
            formula_settings=settings,
        )

        self.assertEqual(set(before), set(after))
        self.assertIsNotNone(before[(103.0, 1.0)].fair_value_edge_curve_mid)
        self.assertIsNotNone(after[(104.0, 1.0)].fair_value_edge_curve_mid)

    def test_shared_call_put_quote_cache_preserves_standalone_fair_values(self) -> None:
        """A composed BOTH view must not alter either single-right fit."""
        strikes = [float(strike) for strike in range(100, 109)]
        calls = [_option(strike, RIGHT_CALL) for strike in strikes]
        puts = [_option(strike, RIGHT_PUT) for strike in strikes]
        call_adjacent = [0.90, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.20]
        put_adjacent = [1.0 - value for value in call_adjacent]
        shared_quotes = _quotes_for_adjacent_mids(calls, call_adjacent)
        shared_quotes.update(_quotes_for_adjacent_mids(puts, put_adjacent))

        call_plan = build_matrix_selection_plan(
            calls,
            strikes[:-1],
            [1.0, 2.0],
            context_neighbors=0,
        )
        put_plan = build_matrix_selection_plan(
            puts,
            strikes[:-1],
            [1.0, 2.0],
            context_neighbors=0,
        )

        def quotes_for_plan(plan: MatrixSelectionPlan) -> dict[int, QuoteSnapshot]:
            con_ids = {
                leg.contract.con_id
                for selection in flatten_matrix_context(plan)
                for leg in selection.legs
            }
            return {con_id: shared_quotes[con_id] for con_id in con_ids}

        def fair_value_signature(snapshot: object) -> tuple[object, ...]:
            return (
                snapshot.synthetic_mid,
                snapshot.fair_value_edge_curve_mid,
                snapshot.fair_value_edge_curve_edge,
                snapshot.fair_value_edge_curve_buy_endpoint,
                snapshot.fair_value_edge_curve_sell_endpoint,
                snapshot.fair_value_edge_curve_residual,
                snapshot.fair_value_edge_curve_fit_points,
                snapshot.fair_value_edge_curve_fit_error,
                snapshot.opportunity_direction,
                snapshot.opportunity_price,
            )

        for plan in (call_plan, put_plan):
            standalone = build_matrix_snapshots(
                plan,
                quotes_for_plan(plan),
                underlying_price=103.5,
            )
            composed_half = build_matrix_snapshots(
                plan,
                shared_quotes,
                underlying_price=103.5,
            )

            self.assertEqual(set(standalone), set(composed_half))
            for cell_key in standalone:
                self.assertEqual(
                    fair_value_signature(standalone[cell_key]),
                    fair_value_signature(composed_half[cell_key]),
                )

    def test_fit_caps_outer_context_and_uses_only_nearest_direct_itm_fallback(self) -> None:
        strikes = [float(strike) for strike in range(95, 111)]
        calls = [_option(strike, RIGHT_CALL) for strike in strikes]
        quotes = _quotes_for_adjacent_mids(calls, [0.50] * (len(strikes) - 1))
        plan = build_matrix_selection_plan(
            calls,
            [100.0, 101.0, 102.0, 103.0, 104.0],
            [1.0],
            context_neighbors=10,
        )

        context = build_matrix_context_snapshots(
            plan,
            quotes,
            underlying_price=102.5,
        )
        segments = _base_curve_segments(
            [snapshot for group in context.values() for snapshot in group],
            quotes,
            102.5,
        )
        density_by_bounds = {
            (segment.lower_strike, segment.upper_strike): segment.density
            for segment in segments
        }

        # With the crossing interval consuming one bridge slot, only the
        # immediately adjacent ITM interval may fall back to the direct book.
        self.assertIsNotNone(density_by_bounds[(101.0, 102.0)])
        self.assertIsNone(density_by_bounds[(100.0, 101.0)])

        # Hidden direct OTM context stops after two atomic intervals beyond the
        # visible upper bound (105), even though the plan subscribed farther.
        fitted_outer = [
            bounds
            for bounds, density in density_by_bounds.items()
            if bounds[0] >= 105.0 and density is not None
        ]
        self.assertEqual(fitted_outer, [(105.0, 106.0), (106.0, 107.0)])

    def test_wide_books_remain_equal_weight_calibration_observations(self) -> None:
        lower = _option(100.0, RIGHT_PUT)
        upper = _option(101.0, RIGHT_PUT)
        pair = VerticalPair(right=RIGHT_PUT, lower=lower, upper=upper)
        quotes = {
            lower.con_id: QuoteSnapshot(
                label=str(lower.con_id),
                contract_con_id=lower.con_id,
                bid=1.20,
                ask=2.80,
            ),
            upper.con_id: QuoteSnapshot(
                label=str(upper.con_id),
                contract_con_id=upper.con_id,
                bid=1.60,
                ask=3.20,
            ),
        }

        observation = _qualified_vertical_observation(pair, quotes)
        narrow_observation = _qualified_vertical_observation(
            pair,
            {
                lower.con_id: QuoteSnapshot(
                    label=str(lower.con_id),
                    contract_con_id=lower.con_id,
                    bid=1.95,
                    ask=2.05,
                ),
                upper.con_id: QuoteSnapshot(
                    label=str(upper.con_id),
                    contract_con_id=upper.con_id,
                    bid=2.35,
                    ask=2.45,
                ),
            },
        )

        # Combined leg width is $3.20 for a $1 vertical.  The $0.40 midpoint
        # remains structurally valid and enters with the same initial weight
        # as a tight market.
        self.assertIsNotNone(observation)
        self.assertEqual(observation, narrow_observation)
        self.assertAlmostEqual(observation.density, 0.40)

    def test_sparse_support_uses_flat_market_center_instead_of_disabling(self) -> None:
        values, fit_error, fit_points = _fit_fair_value_edge_curve(
            [0.0, 1.0, 2.0, 3.0],
            [0.20, None, 0.40, 0.90],
            minimum_points=5,
        )

        self.assertEqual(fit_points, 3)
        self.assertEqual(values, [0.40, 0.40, 0.40, 0.40])
        self.assertAlmostEqual(float(fit_error), (0.20 + 0.0 + 0.50) / 3.0)

    def test_robust_sigmoid_is_bounded_monotone_and_resists_local_reversal(self) -> None:
        x_values = [float(value) for value in range(-4, 5)]
        # The 0.39 -> 0.20 reversal mirrors the shared-leg seesaw observed in
        # the USO volatility event.
        mids = [0.12, 0.18, 0.25, 0.39, 0.20, 0.48, 0.58, 0.68, 0.78]

        first, first_error, fit_points = _fit_fair_value_edge_curve(
            x_values,
            mids,
            minimum_points=5,
        )
        second, second_error, _fit_points = _fit_fair_value_edge_curve(
            x_values,
            mids,
            minimum_points=5,
        )

        self.assertEqual(first, second)
        self.assertEqual(first_error, second_error)
        self.assertEqual(fit_points, len(mids))
        finite = [float(value) for value in first if value is not None]
        self.assertTrue(all(0.0 < value < 1.0 for value in finite))
        self.assertTrue(
            all(left <= right for left, right in zip(finite, finite[1:]))
        )
        self.assertGreater(min(finite), 0.01)

    def test_near_money_bridge_has_only_one_same_side_observation(self) -> None:
        strikes = [float(strike) for strike in range(100, 107)]
        calls = [_option(strike, RIGHT_CALL) for strike in strikes]
        puts = [_option(strike, RIGHT_PUT) for strike in strikes]
        call_mids = [0.80, 0.70, 0.60, 0.50, 0.40, 0.30]
        put_mids = [1.0 - value for value in call_mids]
        quotes = _quotes_for_adjacent_mids(calls, call_mids)
        quotes.update(_quotes_for_adjacent_mids(puts, put_mids))
        plan = build_matrix_selection_plan(
            calls,
            strikes[:-1],
            [1.0],
            context_neighbors=0,
        )
        context = build_matrix_context_snapshots(
            plan,
            quotes,
            underlying_price=102.5,
        )

        segments = _base_curve_segments(
            [snapshot for group in context.values() for snapshot in group],
            quotes,
            102.5,
        )
        crossing = [
            segment
            for segment in segments
            if (segment.lower_strike, segment.upper_strike) == (102.0, 103.0)
            and segment.density is not None
        ]

        self.assertEqual(len(crossing), 1)
        self.assertTrue(all(abs(float(segment.density) - 0.60) < 1e-12 for segment in crossing))
        self.assertEqual(len(segments), len(strikes) - 1)

    def test_uso_style_wide_books_and_shared_leg_seesaw_do_not_collapse(self) -> None:
        strikes = [float(strike) for strike in range(113, 130)]
        puts = [_option(strike, RIGHT_PUT) for strike in strikes]
        adjacent_mids = [
            0.10,
            0.14,
            0.18,
            0.24,
            0.39,
            0.20,
            0.31,
            0.36,
            0.42,
            0.49,
            0.55,
            0.61,
            0.67,
            0.72,
            0.78,
            0.83,
        ]
        tight_quotes = _quotes_for_adjacent_mids(puts, adjacent_mids)
        wide_quotes: dict[int, QuoteSnapshot] = {}
        for record in puts:
            mid = tight_quotes[record.con_id].mid
            self.assertIsNotNone(mid)
            wide_quotes[record.con_id] = QuoteSnapshot(
                label=str(record.con_id),
                contract_con_id=record.con_id,
                bid=round(float(mid) - 0.35, 4),
                ask=round(float(mid) + 0.35, 4),
            )

        plan = build_matrix_selection_plan(
            puts,
            strikes[:-1],
            [1.0, 2.0, 3.0, 4.0, 5.0],
            context_neighbors=0,
        )
        snapshots = build_matrix_snapshots(
            plan,
            wide_quotes,
            underlying_price=123.5,
        )

        atomic = [
            snapshot
            for (_lower, width), snapshot in snapshots.items()
            if width == 1.0
        ]
        self.assertTrue(atomic)
        self.assertGreaterEqual(atomic[0].fair_value_edge_curve_fit_points, 5)
        self.assertTrue(
            all(snapshot.fair_value_edge_curve_mid is not None for snapshot in snapshots.values())
        )
        self.assertTrue(
            all(float(snapshot.fair_value_edge_curve_mid) > 0.01 for snapshot in atomic)
        )
        self.assertTrue(
            all(
                0.0 < float(snapshot.fair_value_edge_curve_mid) < 1.0
                for snapshot in atomic
            )
        )


if __name__ == "__main__":
    unittest.main()
