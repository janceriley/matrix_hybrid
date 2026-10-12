from __future__ import annotations

import queue
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from options_matrix.models import QuoteSnapshot
from options_matrix.schwab_client import (
    SERVICE_LEVELONE_OPTIONS,
    SchwabMarketDataService,
    SubscriptionDiagnostic,
    _LegQuoteState,
)


class QuoteFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SchwabMarketDataService()
        self.state = _LegQuoteState(con_id=1, symbol="TEST OPTION")
        self.service._leg_states[1] = self.state
        self.service._legs_by_symbol[self.state.symbol] = 1
        self.service._symbol_ids[self.state.symbol] = 1
        self.now = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)

    def update(self, fields: dict[str, object], *, seconds: int = 0) -> QuoteSnapshot:
        with patch("options_matrix.schwab_client.datetime") as clock:
            clock.now.return_value = self.now + timedelta(seconds=seconds)
            self.service._handle_option_data({"key": self.state.symbol, **fields})
        return self.state.snapshot()

    def quotes(self) -> list[QuoteSnapshot]:
        result = []
        while True:
            try:
                event = self.service.events.get_nowait()
            except queue.Empty:
                return result
            if event["type"] == "quote":
                result.append(event["quote"])

    def test_default_and_seeded_prices_are_unconfirmed(self) -> None:
        quote = QuoteSnapshot(label="seed", bid=2.0, ask=2.2, updated_at=self.now)
        self.assertIsNone(quote.bid_confirmed_at)
        self.assertIsNone(quote.ask_confirmed_at)
        self.assertIsNone(self.state.bid_confirmed_at)
        self.assertIsNone(self.state.ask_confirmed_at)

    def test_same_prices_refresh_confirmations_without_recording_change_times(self) -> None:
        initial = self.update({"2": 2.0, "3": 2.2})
        self.quotes()
        refreshed = self.update({"2": 2.0, "3": 2.2}, seconds=65)
        self.assertEqual(refreshed.bid_confirmed_at, self.now + timedelta(seconds=65))
        self.assertEqual(refreshed.ask_confirmed_at, refreshed.bid_confirmed_at)
        self.assertEqual(refreshed.updated_at, initial.updated_at)
        self.assertEqual(refreshed.bid_update_received_at, initial.bid_update_received_at)
        self.assertEqual(refreshed.ask_update_received_at, initial.ask_update_received_at)
        self.assertEqual(self.quotes(), [refreshed])

    def test_partial_price_update_and_size_updates_do_not_refresh_other_price(self) -> None:
        self.update({"2": 2.0, "3": 2.2})
        partial = self.update({"2": 2.0}, seconds=65)
        self.assertEqual(partial.bid_confirmed_at, self.now + timedelta(seconds=65))
        self.assertEqual(partial.ask_confirmed_at, self.now)
        sizes = self.update({"16": 10, "17": 12}, seconds=70)
        self.assertEqual(sizes.bid_confirmed_at, partial.bid_confirmed_at)
        self.assertEqual(sizes.ask_confirmed_at, partial.ask_confirmed_at)

    def test_invalid_prices_clear_only_provided_side_and_keep_recorded_values(self) -> None:
        for field, attribute in (("2", "bid_confirmed_at"), ("3", "ask_confirmed_at")):
            for value in (-1, None, "invalid", float("nan"), float("inf"), True):
                with self.subTest(field=field, value=value):
                    initial = self.update({"2": 2.0, "3": 2.2})
                    self.quotes()
                    invalid = self.update({field: value}, seconds=1)
                    self.assertIsNone(getattr(invalid, attribute))
                    other = "ask_confirmed_at" if field == "2" else "bid_confirmed_at"
                    self.assertEqual(getattr(invalid, other), self.now)
                    self.assertEqual((invalid.bid, invalid.ask), (2.0, 2.2))
                    self.assertEqual(invalid.updated_at, initial.updated_at)
                    self.assertEqual(self.quotes(), [invalid])
                    restored = self.update({field: 2.0 if field == "2" else 2.2}, seconds=2)
                    self.assertEqual(getattr(restored, attribute), self.now + timedelta(seconds=2))

    def test_invalid_size_sentinel_invalidates_side_until_price_reconfirmed(self) -> None:
        self.update({"2": 2.0, "3": 2.2, "16": 10, "17": 10})
        invalid = self.update({"16": -1}, seconds=1)
        self.assertIsNone(invalid.bid_confirmed_at)
        self.assertEqual(invalid.ask_confirmed_at, self.now)
        self.assertIsNone(self.update({"16": 10}, seconds=2).bid_confirmed_at)
        self.assertIsNotNone(self.update({"2": 2.0}, seconds=3).bid_confirmed_at)

    def test_crossed_market_requires_both_sides_reconfirmed(self) -> None:
        self.update({"2": 2.0, "3": 2.2})
        crossed = self.update({"2": 2.3}, seconds=1)
        self.assertIsNone(crossed.bid_confirmed_at)
        self.assertIsNone(crossed.ask_confirmed_at)
        repaired = self.update({"3": 2.4}, seconds=2)
        self.assertIsNone(repaired.bid_confirmed_at)
        self.assertEqual(repaired.ask_confirmed_at, self.now + timedelta(seconds=2))
        complete = self.update({"2": 2.3}, seconds=3)
        self.assertEqual(complete.bid_confirmed_at, self.now + timedelta(seconds=3))

    def test_delayed_prices_require_new_live_price_confirmation(self) -> None:
        delayed = self.update({"2": 2.0, "3": 2.2, "delayed": True})
        self.assertIsNone(delayed.bid_confirmed_at)
        self.assertIsNone(delayed.ask_confirmed_at)
        live_status = self.update({"delayed": False}, seconds=1)
        self.assertIsNone(live_status.bid_confirmed_at)
        self.assertIsNone(live_status.ask_confirmed_at)
        live_prices = self.update({"2": 2.0, "3": 2.2}, seconds=2)
        self.assertIsNotNone(live_prices.bid_confirmed_at)
        self.assertIsNotNone(live_prices.ask_confirmed_at)

    def test_subscription_loss_clears_confirmations_and_ack_does_not_restore_them(self) -> None:
        self.update({"2": 2.0, "3": 2.2})
        self.quotes()
        self.service._handle_subscription_diagnostics((SubscriptionDiagnostic(
            service=SERVICE_LEVELONE_OPTIONS,
            symbol=self.state.symbol,
            state="desired",
            desired=True,
            confirmed=False,
        ),))
        invalidated = self.quotes()
        self.assertEqual(len(invalidated), 1)
        self.assertIsNone(invalidated[0].bid_confirmed_at)
        self.assertIsNone(invalidated[0].ask_confirmed_at)
        self.service._handle_subscription_diagnostics((SubscriptionDiagnostic(
            service=SERVICE_LEVELONE_OPTIONS,
            symbol=self.state.symbol,
            state="subscribed",
            desired=True,
            confirmed=True,
        ),))
        self.assertIsNone(self.state.bid_confirmed_at)
        self.assertIsNone(self.state.ask_confirmed_at)

    def test_confirmed_subscription_refresh_retains_valid_price_confirmations(self) -> None:
        quote = self.update({"2": 2.0, "3": 2.2})
        self.quotes()
        self.service._handle_subscription_diagnostics((SubscriptionDiagnostic(
            service=SERVICE_LEVELONE_OPTIONS,
            symbol=self.state.symbol,
            state="subscribed",
            desired=True,
            confirmed=True,
        ),))
        self.assertEqual(self.state.bid_confirmed_at, quote.bid_confirmed_at)
        self.assertEqual(self.state.ask_confirmed_at, quote.ask_confirmed_at)
        self.assertEqual(self.quotes(), [])

    def test_cancellation_emits_unconfirmed_snapshot_before_discarding_state(self) -> None:
        self.update({"2": 2.0, "3": 2.2})
        self.quotes()
        self.service._cancel_active_quotes()
        invalidated = self.quotes()
        self.assertEqual(len(invalidated), 1)
        self.assertIsNone(invalidated[0].bid_confirmed_at)
        self.assertIsNone(invalidated[0].ask_confirmed_at)
        self.assertEqual(self.service._leg_states, {})


if __name__ == "__main__":
    unittest.main()
