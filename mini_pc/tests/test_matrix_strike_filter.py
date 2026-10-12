from __future__ import annotations

import unittest
from options_matrix.app import (
    MATRIX_VIEW_BOTH,
    MATRIX_WIDTH_BANDS,
    MatrixSideState,
    OptionsMatrixApp,
    build_aggregate_ranked_signals,
    build_matrix_input_plan,
    build_matrix_view_plan,
    matrix_quote_target_selections,
    parse_matrix_input_state,
)
from options_matrix.models import (
    RIGHT_CALL,
    RIGHT_PUT,
    SIGNAL_BUY,
    MatrixSelectionPlan,
    is_supported_matrix_pair,
    is_supported_matrix_strike,
)
from tests.test_matrix import _option, _ranked_signal, _snapshot, _tab


def matrix_input(side, bands=MATRIX_WIDTH_BANDS):
    return parse_matrix_input_state(
        display_expiration="2030-01-17", raw_expiration="20300117",
        side=side, low_strike="157", high_strike="163", ticker_symbol="SPY",
        width_states={band: band in bands for band in MATRIX_WIDTH_BANDS},
    )


class MatrixStrikeFilterTests(unittest.TestCase):
    def test_157_5_to_162_5_is_hidden_in_every_view_and_width_choice(self):
        records = [
            _option(157 + index * 0.5, right)
            for right in (RIGHT_CALL, RIGHT_PUT)
            for index in range(13)
        ]
        for view in (RIGHT_CALL, RIGHT_PUT, MATRIX_VIEW_BOTH):
            for bands in (MATRIX_WIDTH_BANDS, *((width,) for width in MATRIX_WIDTH_BANDS)):
                with self.subTest(view=view, bands=bands):
                    plan = build_matrix_view_plan(records, matrix_input(view, bands))
                    self.assertIsNotNone(plan)
                    self.assertEqual(plan.row_strikes, tuple(float(strike) for strike in range(157, 164)))
                    self.assertEqual(plan.widths, tuple(bands))
                    for side_plan in plan.side_plans.values():
                        self.assertNotIn((157.5, 5.0), side_plan.plan.visible)
                        pairs = [selection.pair for selection in side_plan.plan.visible.values()]
                        self.assertTrue(pairs)
                        self.assertTrue(all(pair.lower.strike % 1 != 0.5 and pair.upper.strike % 1 != 0.5
                                            for pair in pairs))
                        if 5.0 in bands:
                            self.assertEqual(
                                {(pair.lower.strike, pair.upper.strike) for pair in pairs if pair.width == 5.0},
                                {(157.0, 162.0), (158.0, 163.0)},
                            )

    def test_either_half_endpoint_is_removed_but_other_fractional_strikes_remain(self):
        records = [_option(strike) for strike in (157.25, 157.5, 157.75, 158.25, 158.5, 158.75)]
        plan = build_matrix_input_plan(records, matrix_input(RIGHT_CALL))
        self.assertIsNotNone(plan)
        self.assertEqual(plan.row_strikes, (157.25, 157.75, 158.25, 158.75))
        self.assertIn((157.25, 1.0), plan.plan.visible)
        self.assertIn((157.75, 1.0), plan.plan.visible)
        self.assertNotIn((157.25, 1.25), plan.plan.visible)  # upper = 158.50
        self.assertNotIn((157.5, 1.25), plan.plan.visible)  # lower = 157.50

    def test_only_half_strikes_cannot_create_a_matrix(self):
        records = [_option(157.5 + index) for index in range(6)]
        self.assertIsNone(build_matrix_input_plan(records, matrix_input(RIGHT_CALL)))

    def test_hidden_chain_context_is_still_available_to_existing_calculations(self):
        records = [_option(157 + index * 0.5) for index in range(13)]
        plan = build_matrix_input_plan(records, matrix_input(RIGHT_CALL))
        self.assertTrue(all(is_supported_matrix_pair(selection.pair) for selection in plan.plan.visible.values()))
        context = matrix_quote_target_selections(plan.plan)
        self.assertTrue(any(selection.pair.lower.strike % 1 == 0.5 for selection in context))
        self.assertEqual(len(records), 13)

    def test_half_strike_comparison_handles_rounding_without_rejecting_quarters(self):
        for strike in (157.5, 162.5, 157.499999999, 162.500000001, float("nan"), float("inf")):
            with self.subTest(strike=strike):
                self.assertFalse(is_supported_matrix_strike(strike))
        for strike in (157.0, 157.25, 157.75, 162.0):
            self.assertTrue(is_supported_matrix_strike(strike))

    def test_legacy_cells_cannot_reappear_in_details_watches_or_rankings(self):
        hidden = _snapshot(157.5, 5.0, 2.0, 0.10)
        kept = _snapshot(157.0, 5.0, 2.0, 0.10)
        snapshots = {(157.5, 5.0): hidden, (157.0, 5.0): kept}
        plan = MatrixSelectionPlan(
            visible={key: snapshot.selection for key, snapshot in snapshots.items()},
            context_by_width={},
        )
        tab = _tab(1, "Legacy", [])
        tab.matrix_plan = plan
        tab.matrix_snapshots = snapshots
        tab.ranked_signals = [
            _ranked_signal(1, "157.5/162.5", 5.0, SIGNAL_BUY, 2.0, 0.10),
            _ranked_signal(2, "157/162", 5.0, SIGNAL_BUY, 2.0, 0.10),
        ]
        self.assertIsNone(OptionsMatrixApp._tab_cell_selection(tab, (157.5, 5.0)))
        self.assertIsNone(OptionsMatrixApp._tab_cell_snapshot(tab, (157.5, 5.0)))
        self.assertIs(OptionsMatrixApp._tab_cell_selection(tab, (157.0, 5.0)), kept.selection)
        self.assertEqual([row.pair_key for row in build_aggregate_ranked_signals([tab], 156.0)], ["157/162"])
        tab.side_states = {RIGHT_CALL: MatrixSideState(RIGHT_CALL, (157.0, 157.5), plan)}
        tab.side_states[RIGHT_CALL].matrix_snapshots = snapshots
        self.assertIsNone(OptionsMatrixApp._tab_cell_selection(tab, (RIGHT_CALL, 157.5, 5.0)))
        self.assertIsNone(OptionsMatrixApp._tab_cell_snapshot(tab, (RIGHT_CALL, 157.5, 5.0)))


if __name__ == "__main__":
    unittest.main()
