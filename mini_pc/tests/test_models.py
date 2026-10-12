from __future__ import annotations

import unittest

from options_matrix.models import (
    DEFAULT_FORMULA_SETTINGS,
    DIRECTION_CREDIT,
    DIRECTION_DEBIT,
    FAIR_VALUE_EDGE_CURVE_MIN_POINTS,
    FAIR_VALUE_EDGE_WIDTH_RATE,
    RIGHT_CALL,
    RIGHT_PUT,
    OptionContractRecord,
    QuoteSnapshot,
    build_exact_width_vertical_pairs,
    build_synthetic_vertical_quote,
    compute_mid,
    compute_width,
    format_size,
    normalize_right,
    resolve_vertical_selection,
)


def _option(strike: float, right: str) -> OptionContractRecord:
    return OptionContractRecord(
        con_id=int(strike * 100),
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


def _pair(right: str, lower_strike: float, upper_strike: float):
    return build_exact_width_vertical_pairs(
        [_option(lower_strike, right), _option(upper_strike, right)],
        upper_strike - lower_strike,
    )[0]


def _selection(
    right: str = RIGHT_CALL,
    lower_strike: float = 100.0,
    upper_strike: float = 101.0,
) -> object:
    return resolve_vertical_selection(_pair(right, lower_strike, upper_strike), DIRECTION_DEBIT)


class ModelsTestCase(unittest.TestCase):
    def test_default_formula_settings_match_existing_constants(self) -> None:
        self.assertEqual(
            DEFAULT_FORMULA_SETTINGS.fair_value_edge_width_rate,
            FAIR_VALUE_EDGE_WIDTH_RATE,
        )
        self.assertEqual(
            DEFAULT_FORMULA_SETTINGS.fair_value_edge_curve_min_points,
            FAIR_VALUE_EDGE_CURVE_MIN_POINTS,
        )

    def test_normalize_right_accepts_short_codes(self) -> None:
        self.assertEqual(normalize_right("c"), RIGHT_CALL)
        self.assertEqual(normalize_right("P"), RIGHT_PUT)

    def test_call_credit_selection_flips_leg_actions(self) -> None:
        selection = resolve_vertical_selection(_pair(RIGHT_CALL, 100, 105), DIRECTION_CREDIT)
        self.assertEqual([(leg.action, leg.contract.strike) for leg in selection.legs], [("SELL", 100), ("BUY", 105)])

    def test_put_debit_selection_buys_the_higher_strike(self) -> None:
        selection = resolve_vertical_selection(_pair(RIGHT_PUT, 100, 105), DIRECTION_DEBIT)
        self.assertEqual([(leg.action, leg.contract.strike) for leg in selection.legs], [("BUY", 105), ("SELL", 100)])

    def test_compute_mid_returns_none_if_one_side_missing(self) -> None:
        self.assertIsNone(compute_mid(None, 1.2))
        self.assertEqual(compute_mid(1.0, 1.4), 1.2)

    def test_compute_width_returns_ask_minus_bid(self) -> None:
        self.assertIsNone(compute_width(None, 1.2))
        self.assertEqual(compute_width(1.0, 1.4), 0.4)

    def test_build_synthetic_vertical_quote_crosses_leg_sides(self) -> None:
        selection = _selection()
        leg_quotes = {
            selection.legs[0].contract.con_id: QuoteSnapshot(
                label=str(selection.legs[0].contract.con_id),
                contract_con_id=selection.legs[0].contract.con_id,
                bid=1.20,
                ask=1.30,
                bid_size=7,
                ask_size=6,
            ),
            selection.legs[1].contract.con_id: QuoteSnapshot(
                label=str(selection.legs[1].contract.con_id),
                contract_con_id=selection.legs[1].contract.con_id,
                bid=0.65,
                ask=0.75,
                bid_size=8,
                ask_size=5,
            ),
        }

        quote = build_synthetic_vertical_quote(selection, leg_quotes)

        self.assertIsNotNone(quote)
        self.assertAlmostEqual(quote.bid, 0.45)
        self.assertAlmostEqual(quote.ask, 0.65)
        self.assertAlmostEqual(quote.mid, 0.55)
        self.assertEqual(quote.bid_size, 5)
        self.assertEqual(quote.ask_size, 6)
        self.assertIsNone(build_synthetic_vertical_quote(selection, {}))

    def test_format_size_removes_trailing_decimal_when_whole(self) -> None:
        self.assertEqual(format_size(12.0), "12")
        self.assertEqual(format_size(12.5), "12.5")


if __name__ == "__main__":
    unittest.main()

