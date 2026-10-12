from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace

from options_matrix.app import (
    POSITION_ICON_COLOR,
    POSITION_ICON_TEXT,
    OptionsMatrixApp,
)
from options_matrix.models import (
    DIRECTION_DEBIT,
    MatrixSelectionPlan,
    OptionContractRecord,
    RIGHT_CALL,
    RIGHT_PUT,
    VerticalPair,
    resolve_vertical_selection,
)
from options_matrix.positions import POSITION_SOLD, PositionRecord, position_key_for_selection


def _selection(*, ticker="SPY", expiration="20300117", right=RIGHT_CALL, lower=100.0, upper=101.0):
    contract = OptionContractRecord(
        con_id=1, symbol=ticker, expiration=expiration, strike=lower, right=right,
        exchange="SMART", currency="USD", trading_class=ticker, multiplier="100",
        local_symbol=f"{ticker} {right} {lower}",
    )
    higher = replace(contract, con_id=2, strike=upper, local_symbol=f"{ticker} {right} {upper}")
    return resolve_vertical_selection(VerticalPair(right, contract, higher), DIRECTION_DEBIT)


class _Canvas:
    def __init__(self):
        self.items = {item_id: {} for item_id in range(1, 5)}
        self.points = {}

    def create_text(self, x, y, **options):
        item_id = len(self.items) + 1
        self.items[item_id] = options
        self.points[item_id] = (x, y)
        return item_id

    def itemconfigure(self, item_id, **options):
        self.items[item_id].update(options)

    def canvasx(self, value):
        return value

    def canvasy(self, value):
        return value


class _Harness:
    cell_key = (100.0, 1.0)

    def __init__(self, selection):
        self.grid_font = "font"
        self.positions = {}
        self._position_results = {}
        self.matrix_canvas = _Canvas()
        self._matrix_render_signature = ("stable",)
        self._matrix_cell_items = {}
        self.selected_cell_key = None
        self.redraws = 0
        self.synced = 0
        self.tab = SimpleNamespace(
            matrix_input=SimpleNamespace(side=selection.pair.right),
            matrix_plan=MatrixSelectionPlan({self.cell_key: selection}, {}),
            matrix_snapshots={}, side_states={}, is_stale=False,
            selected_cell_key=None,
        )
        self.icon_id = OptionsMatrixApp._draw_matrix_position_icon(
            self, self.matrix_canvas, combo_x=10.0, y=20.0,
            row_height=24.0, has_position=False,
        )
        OptionsMatrixApp._remember_matrix_cell_items(
            self, self.cell_key, (1, 2), 3, 4, position_icon_id=self.icon_id,
        )
        self.matrix_reload_hitbox = None
        self.matrix_hitboxes = [(10.0, 20.0, 160.0, 44.0, self.cell_key)]

    def _active_tab(self):
        return self.tab

    def _matrix_render_state_signature(self):
        return ("stable",)

    def _selection_is_itm(self, _selection):
        return False

    def _cell_signal_text(self, _snapshot, *, muted=False):
        return "", "#111111"

    def _redraw_matrix(self):
        self.redraws += 1

    def _redraw_all(self):
        self.redraws += 1

    def _sync_view_from_active_tab(self):
        self.synced += 1

    def refresh(self):
        OptionsMatrixApp._refresh_active_matrix_render(self)

    @property
    def icon(self):
        return self.matrix_canvas.items[self.icon_id]


class PositionIndicatorTests(unittest.TestCase):
    def test_marker_appears_and_disappears_on_incremental_position_changes(self):
        selection = _selection()
        harness = _Harness(selection)
        key = position_key_for_selection(selection)
        item_ids = set(harness.matrix_canvas.items)

        harness.refresh()
        self.assertEqual(harness.icon["text"], "")
        harness.positions[key] = PositionRecord(key, POSITION_SOLD, 0.48)
        harness.refresh()
        self.assertEqual(harness.icon["text"], POSITION_ICON_TEXT)
        self.assertEqual(harness.icon["fill"], POSITION_ICON_COLOR)
        self.assertIn("position_indicator", harness.icon["tags"])

        del harness.positions[key]
        harness.refresh()
        self.assertEqual(harness.icon["text"], "")
        self.assertEqual(set(harness.matrix_canvas.items), item_ids)
        self.assertEqual(harness.redraws, 0)

    def test_marker_matches_all_five_position_identity_fields(self):
        selection = _selection()
        harness = _Harness(selection)
        key = position_key_for_selection(selection)
        harness.positions[key] = PositionRecord(key, POSITION_SOLD, 0.48)
        harness.refresh()
        self.assertEqual(harness.icon["text"], POSITION_ICON_TEXT)

        for changed in (
            _selection(ticker="QQQ"),
            _selection(expiration="20300118"),
            _selection(right=RIGHT_PUT),
            _selection(lower=99.0),
            _selection(upper=102.0),
            None,
        ):
            with self.subTest(key=position_key_for_selection(changed) if changed else None):
                if changed is None:
                    del harness.tab.matrix_plan.visible[harness.cell_key]
                else:
                    harness.tab.matrix_plan.visible[harness.cell_key] = changed
                harness.refresh()
                self.assertEqual(harness.icon["text"], "")
        self.assertEqual(harness.redraws, 0)

    def test_pausing_exit_alert_keeps_saved_position_marker(self):
        selection = _selection()
        harness = _Harness(selection)
        key = position_key_for_selection(selection)
        for armed in (True, False, True):
            with self.subTest(armed=armed):
                harness.positions[key] = PositionRecord(key, POSITION_SOLD, 0.48, armed=armed)
                harness.refresh()
                self.assertEqual(harness.icon["text"], POSITION_ICON_TEXT)

    def test_clicking_marker_selects_cell_without_toggling_or_deleting_position(self):
        selection = _selection()
        key = position_key_for_selection(selection)
        for armed in (True, False):
            with self.subTest(armed=armed):
                harness = _Harness(selection)
                record = PositionRecord(key, POSITION_SOLD, 0.48, armed=armed)
                harness.positions[key] = record
                harness.refresh()
                x, y = harness.matrix_canvas.points[harness.icon_id]
                OptionsMatrixApp._on_matrix_click(harness, SimpleNamespace(x=x, y=y))
                self.assertEqual(harness.tab.selected_cell_key, harness.cell_key)
                self.assertEqual(harness.positions, {key: record})
                self.assertEqual((harness.synced, harness.redraws), (1, 1))
                self.assertEqual(harness.icon["text"], POSITION_ICON_TEXT)


if __name__ == "__main__":
    unittest.main()
