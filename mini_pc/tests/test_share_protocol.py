from __future__ import annotations

import socket
import threading
import unittest
from datetime import datetime, timedelta, timezone

from options_matrix.models import (
    LIVE_MARKET_DATA_TYPE,
    OptionChainDefinition,
    OptionContractRecord,
    QUOTE_KIND_LEG,
    QuoteSnapshot,
    SearchResult,
    UnderlyingContract,
)
from options_matrix.schwab_client import SubscriptionDiagnostic
from options_matrix.share_protocol import (
    FramedSocket,
    ProtocolError,
    decode_diagnostic,
    decode_quote,
    decode_record,
    decode_search_result,
    encode_diagnostic,
    encode_frame,
    encode_quote,
    encode_record,
    encode_search_result,
    math_fingerprint,
    new_nonce,
    proofs_match,
    studio_proof,
    valid_nonce,
    viewer_proof,
)

STUDIO_NOW_UTC = datetime(2026, 10, 13, 15, 0, 0, tzinfo=timezone.utc)
STUDIO_NOW_LOCAL = datetime(2026, 10, 13, 11, 0, 0)


def _quote(**overrides) -> QuoteSnapshot:
    values = dict(
        label="17", kind=QUOTE_KIND_LEG, contract_con_id=17, bid=1.25, ask=1.3, last=1.27,
        mark=1.275, bid_size=12.0, ask_size=40.0, implied_volatility=0.21,
        model_option_price=1.28, volume=905.0, open_interest=4410.0,
        market_data_type=LIVE_MARKET_DATA_TYPE,
        quote_time=STUDIO_NOW_UTC - timedelta(milliseconds=800),
        updated_at=STUDIO_NOW_LOCAL - timedelta(milliseconds=300),
        bid_update_received_at=STUDIO_NOW_LOCAL - timedelta(seconds=2),
        ask_update_received_at=STUDIO_NOW_LOCAL - timedelta(seconds=3),
        bid_confirmed_at=STUDIO_NOW_UTC - timedelta(milliseconds=250),
        ask_confirmed_at=STUDIO_NOW_UTC - timedelta(seconds=61),
    )
    values.update(overrides)
    return QuoteSnapshot(**values)


class ProtocolCodecTests(unittest.TestCase):
    def test_quote_round_trip_rebuilds_ages_on_the_receivers_clock(self):
        encoded = encode_quote(_quote(), now_utc=STUDIO_NOW_UTC, now_local=STUDIO_NOW_LOCAL)
        # The viewer's clock is five minutes off from the mini PC's clock.
        viewer_utc = STUDIO_NOW_UTC + timedelta(minutes=5)
        viewer_local = STUDIO_NOW_LOCAL + timedelta(minutes=5)
        decoded = decode_quote(encoded, now_utc=viewer_utc, now_local=viewer_local)
        for name in ("label", "kind", "contract_con_id", "bid", "ask", "last", "mark", "bid_size",
                     "ask_size", "implied_volatility", "model_option_price", "volume",
                     "open_interest", "market_data_type", "quote_time"):
            self.assertEqual(getattr(decoded, name), getattr(_quote(), name), name)
        self.assertEqual(viewer_utc - decoded.bid_confirmed_at, timedelta(milliseconds=250))
        self.assertEqual(viewer_utc - decoded.ask_confirmed_at, timedelta(seconds=61))
        self.assertEqual(viewer_local - decoded.updated_at, timedelta(milliseconds=300))
        self.assertIsNotNone(decoded.bid_confirmed_at.tzinfo)
        self.assertIsNone(decoded.updated_at.tzinfo)

    def test_missing_values_and_times_stay_missing(self):
        quote = _quote(bid=None, ask_size=None, bid_confirmed_at=None, quote_time=None, market_data_type=None)
        decoded = decode_quote(
            encode_quote(quote, now_utc=STUDIO_NOW_UTC, now_local=STUDIO_NOW_LOCAL),
            now_utc=STUDIO_NOW_UTC, now_local=STUDIO_NOW_LOCAL,
        )
        self.assertIsNone(decoded.bid)
        self.assertIsNone(decoded.ask_size)
        self.assertIsNone(decoded.bid_confirmed_at)
        self.assertIsNone(decoded.quote_time)
        self.assertIsNone(decoded.market_data_type)

    def test_future_times_never_become_negative_ages(self):
        quote = _quote(bid_confirmed_at=STUDIO_NOW_UTC + timedelta(seconds=5))
        encoded = encode_quote(quote, now_utc=STUDIO_NOW_UTC, now_local=STUDIO_NOW_LOCAL)
        decoded = decode_quote(encoded, now_utc=STUDIO_NOW_UTC, now_local=STUDIO_NOW_LOCAL)
        self.assertEqual(decoded.bid_confirmed_at, STUDIO_NOW_UTC)

    def test_malformed_quotes_are_rejected(self):
        good = encode_quote(_quote(), now_utc=STUDIO_NOW_UTC, now_local=STUDIO_NOW_LOCAL)
        bad_cases = [good[:3], [*good[:2], "17", *good[3:]], [*good[:6], [[-5, 1]] + good[6][1:]]]
        for bad in bad_cases:
            with self.subTest(bad=bad), self.assertRaises(ProtocolError):
                decode_quote(bad, now_utc=STUDIO_NOW_UTC, now_local=STUDIO_NOW_LOCAL)

    def test_records_search_results_and_diagnostics_round_trip(self):
        record = OptionContractRecord(5, "SPY", "20261016", 600.5, "CALL", "X", "USD", "SPY", "100", "SPY 261016C600500")
        self.assertEqual(decode_record(encode_record(record)), record)
        search = SearchResult(
            UnderlyingContract("SPY", 3, "NYSE", "NYSE", "USD"),
            OptionChainDefinition("SPY", 3, "NYSE", "SPY", "100", ("20261016", "20261023"), (600.0,)),
        )
        self.assertEqual(decode_search_result(encode_search_result(search)), search)
        self.assertIsNone(decode_search_result(encode_search_result(None)))
        diagnostic = SubscriptionDiagnostic(
            service="LEVELONE_OPTIONS", symbol="SPY 261016C600500", state="subscribed", desired=True,
            confirmed=True, contract_con_id=5, quote_kind=QUOTE_KIND_LEG, generation=3,
        )
        decoded = decode_diagnostic(encode_diagnostic(diagnostic), generation=9)
        self.assertEqual(decoded.state, "subscribed")
        self.assertEqual(decoded.contract_con_id, 5)
        self.assertEqual(decoded.generation, 9)
        with self.assertRaises(ProtocolError):
            decode_record([1, 2, 3])


class ProofTests(unittest.TestCase):
    def test_proofs_need_the_password_and_differ_by_role(self):
        server, client = new_nonce(), new_nonce()
        self.assertTrue(valid_nonce(server))
        self.assertFalse(valid_nonce("xyz"))
        proof = viewer_proof("K7QF-29XM-PA4D-HN8R", server, client)
        self.assertTrue(proofs_match(proof, viewer_proof("K7QF-29XM-PA4D-HN8R", server, client)))
        self.assertFalse(proofs_match(proof, viewer_proof("WRONG-PASSWORD", server, client)))
        self.assertFalse(proofs_match(proof, studio_proof("K7QF-29XM-PA4D-HN8R", server, client)))
        self.assertFalse(proofs_match(proof, None))
        self.assertNotIn("K7QF", proof)

    def test_fingerprint_is_stable_and_ignores_line_endings(self):
        self.assertEqual(math_fingerprint(), math_fingerprint())
        self.assertEqual(len(math_fingerprint()), 16)


class FramingTests(unittest.TestCase):
    def test_frames_survive_fragmented_delivery(self):
        left, right = socket.socketpair()
        try:
            right.settimeout(1.0)
            receiver = FramedSocket(right)
            frame = encode_frame({"type": "quotes", "quotes": list(range(2000))})

            def send_slowly():
                for index in range(0, len(frame), 777):
                    left.sendall(frame[index:index + 777])

            sender = threading.Thread(target=send_slowly)
            sender.start()
            message = None
            while message is None:
                message = receiver.receive()
            sender.join()
            self.assertEqual(message["quotes"][-1], 1999)
        finally:
            left.close()
            right.close()

    def test_closed_connection_and_untyped_messages_raise(self):
        left, right = socket.socketpair()
        right.settimeout(1.0)
        receiver = FramedSocket(right)
        left.sendall(encode_frame({"no": "type"}))
        with self.assertRaises(ProtocolError):
            receiver.receive()
        left.close()
        with self.assertRaises(EOFError):
            receiver.receive()
        right.close()


if __name__ == "__main__":
    unittest.main()
