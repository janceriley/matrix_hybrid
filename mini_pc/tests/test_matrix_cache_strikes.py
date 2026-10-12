from __future__ import annotations

import unittest
from dataclasses import replace

from options_matrix.app import (
    MATRIX_VIEW_BOTH,
    CachedMatrixTab,
    CachedMatrixWorkspace,
    MatrixInputState,
    MatrixTabState,
    cached_matrix_workspace_from_tabs,
    matrix_workspace_from_mapping,
    matrix_workspace_to_dict,
)
from options_matrix.models import (
    RIGHT_CALL,
    RIGHT_PUT,
    MatrixSelectionPlan,
)


def _cached_tab(**changes: object) -> CachedMatrixTab:
    return replace(
        CachedMatrixTab(
            raw_expiration="20300117",
            side=RIGHT_CALL,
            low_strike=157.0,
            high_strike=163.0,
            widths=(1.0, 2.0, 3.0, 4.0, 5.0),
            row_strikes=(157.0, 157.25, 157.5, 157.75, 158.0, 162.0, 162.5, 163.0),
        ),
        **changes,
    )


def _legacy_payload(tab: CachedMatrixTab) -> dict[str, object]:
    # Construct the old representation directly so the save filter cannot mask
    # a missing restore filter.
    return {
        "version": 1,
        "ticker_symbol": "SPY",
        "tabs": [{
            "raw_expiration": tab.raw_expiration,
            "view_type": tab.side,
            "low_strike": tab.low_strike,
            "high_strike": tab.high_strike,
            "widths": list(tab.widths),
            "row_strikes": list(tab.row_strikes),
            "selected_cell_key": list(tab.selected_cell_key) if tab.selected_cell_key else None,
        }],
    }


class MatrixCacheStrikeFilterTests(unittest.TestCase):
    def test_save_and_restore_reject_half_strike_selections_across_all_widths(self) -> None:
        for side in (RIGHT_CALL, RIGHT_PUT, MATRIX_VIEW_BOTH):
            for width in (1.0, 2.0, 3.0, 4.0, 5.0):
                with self.subTest(side=side, width=width):
                    key = (RIGHT_PUT, 157.5, width) if side == MATRIX_VIEW_BOTH else (157.5, width)
                    tab = _cached_tab(side=side, selected_cell_key=key)
                    saved = matrix_workspace_to_dict(CachedMatrixWorkspace("SPY", (tab,)))
                    restored = matrix_workspace_from_mapping(_legacy_payload(tab))
                    self.assertIsNone(saved["tabs"][0]["selected_cell_key"])
                    self.assertIsNotNone(restored)
                    self.assertIsNone(restored.tabs[0].selected_cell_key)
                    self.assertEqual(restored.tabs[0].row_strikes, (157.0, 157.25, 157.75, 158.0, 162.0, 163.0))
                    self.assertEqual(saved["tabs"][0]["row_strikes"], list(restored.tabs[0].row_strikes))

    def test_save_and_restore_reject_either_half_strike_endpoint(self) -> None:
        for lower, upper in ((157.25, 157.5), (157.5, 157.75), (157.5, 162.5)):
            with self.subTest(lower=lower, upper=upper):
                width = upper - lower
                tab = _cached_tab(
                    side=MATRIX_VIEW_BOTH,
                    widths=(0.25, 0.75, 5.0),
                    selected_cell_key=(RIGHT_CALL, lower, width),
                )
                saved = matrix_workspace_to_dict(CachedMatrixWorkspace("SPY", (tab,)))
                restored = matrix_workspace_from_mapping(_legacy_payload(tab))
                self.assertIsNone(saved["tabs"][0]["selected_cell_key"])
                self.assertIsNotNone(restored)
                self.assertIsNone(restored.tabs[0].selected_cell_key)

    def test_supported_fractional_strikes_and_range_boundaries_are_preserved(self) -> None:
        tab = _cached_tab(
            low_strike=156.5,
            high_strike=163.5,
            widths=(0.25, 0.75, 1.0, 5.0),
            selected_cell_key=(157.25, 0.75),
        )
        saved = matrix_workspace_to_dict(CachedMatrixWorkspace("SPY", (tab,)))
        restored = matrix_workspace_from_mapping(saved)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.tabs[0], replace(tab, row_strikes=(157.0, 157.25, 157.75, 158.0, 162.0, 163.0)))

    def test_current_tab_cache_filters_rows_and_selection(self) -> None:
        state = MatrixTabState(
            tab_id=1,
            label="Five wide",
            matrix_input=MatrixInputState("2030-01-17", "20300117", RIGHT_CALL, 157.5, 163.5, (5.0,), "SPY"),
            row_strikes=(157.5, 158.0, 162.5, 163.0),
            matrix_plan=MatrixSelectionPlan(visible={}, context_by_width={}),
            selected_cell_key=(157.5, 5.0),
        )
        cached = cached_matrix_workspace_from_tabs([state], 1)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.tabs[0].row_strikes, (158.0, 163.0))
        self.assertIsNone(cached.tabs[0].selected_cell_key)
        self.assertEqual((cached.tabs[0].low_strike, cached.tabs[0].high_strike), (157.5, 163.5))

    def test_half_strike_only_rows_never_return_through_fallback(self) -> None:
        for rows in ((), (157.5, 162.5)):
            with self.subTest(rows=rows):
                tab = _cached_tab(
                    low_strike=157.5,
                    high_strike=162.5,
                    widths=(5.0,),
                    row_strikes=rows,
                    selected_cell_key=(157.5, 5.0),
                )
                restored = matrix_workspace_from_mapping(_legacy_payload(tab))
                self.assertIsNotNone(restored)
                self.assertEqual(restored.tabs[0].row_strikes, ())
                self.assertIsNone(restored.tabs[0].selected_cell_key)
                without_widths = replace(tab, widths=())
                self.assertIsNone(matrix_workspace_from_mapping(_legacy_payload(without_widths)))

    def test_cache_width_discovery_uses_only_supported_strike_endpoints(self) -> None:
        tab = _cached_tab(widths=(), row_strikes=(157.0, 157.5, 157.75))
        restored = matrix_workspace_from_mapping(_legacy_payload(tab))
        self.assertIsNotNone(restored)
        self.assertEqual(restored.tabs[0].widths, (0.75,))
        self.assertEqual(restored.tabs[0].row_strikes, (157.0, 157.75))

    def test_excluded_rows_do_not_create_fallback_rows_from_integer_bounds(self) -> None:
        tab = _cached_tab(
            low_strike=157.0,
            high_strike=163.0,
            widths=(5.0,),
            row_strikes=(157.5, 162.5),
        )
        restored = matrix_workspace_from_mapping(_legacy_payload(tab))
        self.assertIsNotNone(restored)
        self.assertEqual(restored.tabs[0].row_strikes, ())
        self.assertIsNone(matrix_workspace_from_mapping(_legacy_payload(replace(tab, widths=()))))


if __name__ == "__main__":
    unittest.main()
