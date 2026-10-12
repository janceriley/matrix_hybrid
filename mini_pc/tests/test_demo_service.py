from __future__ import annotations

import time
import unittest
from datetime import date, datetime, timezone

from options_matrix.demo_service import (
    DemoMarketDataService,
    DemoMarketSessionGuard,
    black_scholes_price,
    demo_expirations,
)
from options_matrix.models import QUOTE_KIND_LEG, QUOTE_KIND_UNDERLYING, RIGHT_CALL, RIGHT_PUT, resolve_vertical_selection, VerticalPair


class DemoServiceTests(unittest.TestCase):
    def test_prices_follow_option_basics(self):
        call = black_scholes_price(RIGHT_CALL, 600, 600, 30 / 365, 0.2)
        put = black_scholes_price(RIGHT_PUT, 600, 600, 30 / 365, 0.2)
        self.assertGreater(call, 0)
        self.assertGreater(put, 0)
        self.assertGreater(black_scholes_price(RIGHT_CALL, 600, 590, 30 / 365, 0.2), call)
        self.assertLess(black_scholes_price(RIGHT_PUT, 600, 590, 30 / 365, 0.2), put)

    def test_expirations_skip_weekends(self):
        expirations = demo_expirations(date(2026, 10, 10), count=3)  # a Saturday
        self.assertEqual(expirations, ("20261012", "20261013", "20261014"))

    def test_session_guard_treats_the_market_as_open(self):
        status = DemoMarketSessionGuard().status_at(datetime(2026, 10, 11, 3, 0, tzinfo=timezone.utc))
        self.assertTrue(status.is_open)
        self.assertIn("demo", status.message)

    def test_search_chain_subscribe_and_stream_quotes(self):
        service = DemoMarketDataService(seed=3, tick_seconds=0.05)
        with self.assertRaises(RuntimeError):
            service.search_ticker("SPY")
        service.connect()
        try:
            self.assertTrue(service.connected)
            result = service.search_ticker("spy")
            self.assertEqual(result.underlying.symbol, "SPY")
            records = service.load_option_records(result, result.chain.expirations[0])
            self.assertEqual(records, service.load_option_records(result, result.chain.expirations[0]))
            calls = sorted((r for r in records if r.right == RIGHT_CALL), key=lambda r: r.strike)
            lower, upper = calls[len(calls) // 2], calls[len(calls) // 2 + 1]
            selection = resolve_vertical_selection(VerticalPair(RIGHT_CALL, lower, upper), "DEBIT")
            service.subscribe_matrix_quotes(result, [selection])
            seen_kinds = set()
            seen_legs = set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and (len(seen_legs) < 2 or QUOTE_KIND_UNDERLYING not in seen_kinds):
                try:
                    event = service.events.get(timeout=0.2)
                except Exception:
                    continue
                if event.get("type") == "quote":
                    quote = event["quote"]
                    seen_kinds.add(quote.kind)
                    if quote.kind == QUOTE_KIND_LEG:
                        seen_legs.add(quote.contract_con_id)
                        self.assertLessEqual(quote.bid, quote.ask)
                        self.assertIsNotNone(quote.bid_confirmed_at.tzinfo)
            self.assertEqual(seen_legs, {lower.con_id, upper.con_id})
            self.assertIn(QUOTE_KIND_UNDERLYING, seen_kinds)
        finally:
            service.disconnect()
        self.assertFalse(service.connected)


if __name__ == "__main__":
    unittest.main()
