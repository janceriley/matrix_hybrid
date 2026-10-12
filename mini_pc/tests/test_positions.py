from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from options_matrix.models import (
    DEFAULT_FORMULA_SETTINGS,
    DIRECTION_DEBIT,
    FormulaSettings,
    MonitoredSideSnapshot,
    OptionContractRecord,
    RIGHT_CALL,
    RIGHT_PUT,
    SIGNAL_BUY,
    SIGNAL_SELL,
    SyntheticSpreadQuote,
    VerticalPair,
    resolve_vertical_selection,
)
from options_matrix.positions import (
    POSITION_BOUGHT,
    POSITION_SOLD,
    PositionRecord,
    evaluate_position,
    load_positions,
    position_key_for_selection,
    position_store_path,
    save_positions,
)


def _snapshot(
    mid: float = 0.38,
    *,
    ticker: str = "SPY",
    expiration: str = "20300117",
    right: str = RIGHT_CALL,
    lower: float = 100.0,
    width: float = 1.0,
) -> MonitoredSideSnapshot:
    lower_contract = OptionContractRecord(
        con_id=1, symbol=ticker, expiration=expiration, strike=lower, right=right,
        exchange="SMART", currency="USD", trading_class=ticker, multiplier="100",
        local_symbol=f"{ticker} {right} {lower}",
    )
    upper_contract = replace(lower_contract, con_id=2, strike=lower + width)
    selection = resolve_vertical_selection(
        VerticalPair(right=right, lower=lower_contract, upper=upper_contract), DIRECTION_DEBIT
    )
    return MonitoredSideSnapshot(
        title=selection.label, pair_key=selection.pair.label, selection=selection,
        synthetic_quote=SyntheticSpreadQuote(bid=mid, ask=mid),
    )


class PositionExitTestCase(unittest.TestCase):
    def test_sold_position_triggers_at_profit_target_without_a_fair_value_signal(self) -> None:
        for right in (RIGHT_CALL, RIGHT_PUT):
            with self.subTest(right=right):
                snapshot = _snapshot(right=right)
                snapshot.fair_value_edge_curve_mid = 0.40
                position = PositionRecord(position_key_for_selection(snapshot.selection), POSITION_SOLD, 0.48)
                result = evaluate_position(position, snapshot, DEFAULT_FORMULA_SETTINGS, live=True)
                self.assertEqual((result.target_price, result.edge, result.profit), (0.38, 0.10, 0.10))
                self.assertEqual((result.signal, result.status), (SIGNAL_BUY, "ready"))
                self.assertIsNone(snapshot.opportunity_direction)

    def test_bought_position_is_symmetric_and_requires_the_target(self) -> None:
        snapshot = _snapshot(0.5799)
        position = PositionRecord(position_key_for_selection(snapshot.selection), POSITION_BOUGHT, 0.48)
        before = evaluate_position(position, snapshot, DEFAULT_FORMULA_SETTINGS, live=True)
        self.assertEqual((before.target_price, before.profit, before.status), (0.58, 0.0999, "monitoring"))
        self.assertIsNone(before.signal)
        snapshot.synthetic_quote = SyntheticSpreadQuote(bid=0.58, ask=0.58)
        ready = evaluate_position(position, snapshot, DEFAULT_FORMULA_SETTINGS, live=True)
        self.assertEqual((ready.signal, ready.profit, ready.status), (SIGNAL_SELL, 0.10, "ready"))

    def test_threshold_comparisons_use_four_decimal_prices(self) -> None:
        snapshot = _snapshot(0.3800000000000001)
        position = PositionRecord(position_key_for_selection(snapshot.selection), POSITION_SOLD, 0.48)
        ready = evaluate_position(position, snapshot, DEFAULT_FORMULA_SETTINGS, live=True)
        self.assertEqual(ready.signal, SIGNAL_BUY)
        snapshot.synthetic_quote = SyntheticSpreadQuote(bid=0.3801, ask=0.3801)
        self.assertEqual(evaluate_position(position, snapshot, DEFAULT_FORMULA_SETTINGS, live=True).status, "monitoring")

    def test_targets_use_each_width_edge_and_recalculate_after_setting_changes(self) -> None:
        for width, edge in ((1.0, 0.10), (2.0, 0.14), (3.0, 0.17), (4.0, 0.20), (5.0, 0.22)):
            with self.subTest(width=width):
                snapshot = _snapshot(0.38, width=width)
                position = PositionRecord(position_key_for_selection(snapshot.selection), POSITION_SOLD, 0.48)
                result = evaluate_position(position, snapshot, DEFAULT_FORMULA_SETTINGS, live=True)
                self.assertEqual(result.edge, edge)
                self.assertEqual(result.target_price, round(0.48 - edge, 4))
        snapshot = _snapshot(0.38, width=3.0)
        position = PositionRecord(position_key_for_selection(snapshot.selection), POSITION_SOLD, 0.48)
        changed = evaluate_position(position, snapshot, FormulaSettings(fair_value_edge_width_3=0.08), live=True)
        self.assertEqual((changed.target_price, changed.signal), (0.40, SIGNAL_BUY))

    def test_missing_nonfinite_and_non_live_quotes_wait_without_signalling(self) -> None:
        valid = _snapshot()
        position = PositionRecord(position_key_for_selection(valid.selection), POSITION_SOLD, 0.48)
        for snapshot in (None, replace(valid, synthetic_quote=None), _snapshot(float("nan")), _snapshot(float("inf"))):
            with self.subTest(snapshot=snapshot):
                result = evaluate_position(position, snapshot, DEFAULT_FORMULA_SETTINGS, live=True)
                self.assertEqual(result.status, "waiting")
                self.assertIsNone(result.signal)
                self.assertIsNone(result.mid)
                self.assertIsNone(result.profit)
        result = evaluate_position(position, valid, DEFAULT_FORMULA_SETTINGS, live=False)
        self.assertEqual(result.status, "waiting")
        self.assertIsNone(result.signal)
        self.assertIsNone(result.mid)

    def test_paused_record_retains_live_price_but_cannot_signal(self) -> None:
        snapshot = _snapshot()
        position = PositionRecord(position_key_for_selection(snapshot.selection), POSITION_SOLD, 0.48, armed=False)
        result = evaluate_position(position, snapshot, DEFAULT_FORMULA_SETTINGS, live=True)
        self.assertEqual((result.status, result.mid, result.profit), ("paused", 0.38, 0.10))
        self.assertIsNone(result.signal)

    def test_ready_exit_does_not_require_fair_value_or_match_entry_signal(self) -> None:
        snapshot = _snapshot()
        snapshot.fair_value_edge_curve_mid = None
        snapshot.opportunity_direction = SIGNAL_SELL
        position = PositionRecord(position_key_for_selection(snapshot.selection), POSITION_SOLD, 0.48)
        result = evaluate_position(position, snapshot, DEFAULT_FORMULA_SETTINGS, live=True)
        self.assertEqual((result.status, result.signal), ("ready", SIGNAL_BUY))
        self.assertEqual(snapshot.opportunity_direction, SIGNAL_SELL)

    def test_identity_isolates_ticker_expiration_right_and_strikes(self) -> None:
        original = _snapshot()
        key = position_key_for_selection(original.selection)
        position = PositionRecord(key, POSITION_SOLD, 0.48)
        variants = (
            _snapshot(ticker="QQQ"), _snapshot(expiration="20300215"),
            _snapshot(right=RIGHT_PUT), _snapshot(lower=101.0), _snapshot(width=2.0),
        )
        for snapshot in variants:
            with self.subTest(key=position_key_for_selection(snapshot.selection)):
                self.assertNotEqual(position_key_for_selection(snapshot.selection), key)
                self.assertEqual(evaluate_position(position, snapshot, DEFAULT_FORMULA_SETTINGS, live=True).status, "waiting")

    def test_invalid_records_and_mixed_contract_identity_are_rejected(self) -> None:
        key = position_key_for_selection(_snapshot().selection)
        for average in (0.0, -0.1, float("nan"), float("inf"), True, "0.48"):
            with self.subTest(average=average), self.assertRaises(ValueError):
                PositionRecord(key, POSITION_SOLD, average)
        for lower, upper in ((100.0, 100.5), (100.5, 101.5), (100.0, 101.5), (101.0, 100.0)):
            with self.subTest(strikes=(lower, upper)), self.assertRaises(ValueError):
                PositionRecord((*key[:3], lower, upper), POSITION_SOLD, 0.48)
        selection = _snapshot().selection
        mixed = replace(selection, pair=replace(selection.pair, upper=replace(selection.pair.upper, symbol="QQQ")))
        with self.assertRaises(ValueError):
            position_key_for_selection(mixed)

    def test_unreachable_targets_are_not_silently_clamped(self) -> None:
        snapshot = _snapshot(0.0)
        sold = PositionRecord(position_key_for_selection(snapshot.selection), POSITION_SOLD, 0.05)
        self.assertEqual(evaluate_position(sold, snapshot, DEFAULT_FORMULA_SETTINGS, live=True).target_price, -0.05)
        self.assertEqual(evaluate_position(sold, snapshot, DEFAULT_FORMULA_SETTINGS, live=True).status, "monitoring")
        bought = replace(sold, side=POSITION_BOUGHT, average_price=0.95)
        self.assertEqual(evaluate_position(bought, snapshot, DEFAULT_FORMULA_SETTINGS, live=True).target_price, 1.05)


class PositionPersistenceTestCase(unittest.TestCase):
    def test_store_location_follows_appdata_with_home_fallback(self) -> None:
        self.assertEqual(position_store_path(environ={"APPDATA": "profile"}), Path("profile") / "OptionsMatrix" / "positions.json")
        self.assertEqual(position_store_path(environ={}, home=Path("profile")), Path("profile") / ".options_matrix_positions.json")

    def test_round_trip_preserves_positions_across_tickers_and_pause_state(self) -> None:
        records = [
            PositionRecord(position_key_for_selection(_snapshot().selection), POSITION_SOLD, 0.48, armed=False),
            PositionRecord(position_key_for_selection(_snapshot(ticker="QQQ", right=RIGHT_PUT).selection), POSITION_BOUGHT, 0.65),
        ]
        expected = {record.key: record for record in records}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new" / "positions.json"
            save_positions(expected, path)
            self.assertEqual(load_positions(path), expected)
            save_positions({}, path)
            self.assertEqual(load_positions(path), {})

    def test_legacy_notification_history_is_ignored_without_losing_saved_positions(self) -> None:
        active = PositionRecord(position_key_for_selection(_snapshot().selection), POSITION_SOLD, 0.48)
        paused = PositionRecord(position_key_for_selection(_snapshot(ticker="QQQ").selection), POSITION_BOUGHT, 0.65, armed=False)
        expected = {record.key: record for record in (active, paused)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "positions.json"
            save_positions(expected, path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            for entry in payload["positions"]:
                entry["notified"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            restored = load_positions(path)
            self.assertEqual(restored, expected)
            self.assertEqual(evaluate_position(restored[active.key], _snapshot(), DEFAULT_FORMULA_SETTINGS, live=True).signal, SIGNAL_BUY)
            save_positions(restored, path)
            self.assertTrue(all("notified" not in entry for entry in json.loads(path.read_text(encoding="utf-8"))["positions"]))

    def test_malformed_files_and_entries_are_ignored_without_losing_valid_records(self) -> None:
        record = PositionRecord(position_key_for_selection(_snapshot().selection), POSITION_SOLD, 0.48)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "positions.json"
            self.assertEqual(load_positions(path), {})
            for text in ("{", "[]", '{"version":2,"positions":[]}', '{"version":true,"positions":[]}'):
                path.write_text(text, encoding="utf-8")
                self.assertEqual(load_positions(path), {})
            save_positions({record.key: record}, path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            valid = payload["positions"][0]
            payload["positions"].extend([
                None, {}, {**valid, "side": "SELL"}, {**valid, "average_price": "NaN"},
                {**valid, "armed": "false"},
                {**valid, "key": ["SPY", "bad", RIGHT_CALL, 100, 101]},
                {**valid, "key": ["SPY", "20300117", RIGHT_CALL, 100.5, 101.5]},
                {**valid, "key": ["SPY", "20300117", RIGHT_CALL, 100, 101.5]},
            ])
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(load_positions(path), {record.key: record})

    def test_failed_atomic_replace_preserves_existing_store_and_removes_temporary_file(self) -> None:
        record = PositionRecord(position_key_for_selection(_snapshot().selection), POSITION_SOLD, 0.48)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "positions.json"
            save_positions({record.key: record}, path)
            before = path.read_bytes()
            with patch("options_matrix.positions.os.replace", side_effect=OSError("write failed")):
                with self.assertRaises(OSError):
                    save_positions({}, path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
