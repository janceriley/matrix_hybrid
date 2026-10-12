from __future__ import annotations

import socket
import time
import unittest
from datetime import datetime, timezone

from options_matrix.models import LIVE_MARKET_DATA_TYPE, QUOTE_KIND_LEG, QUOTE_KIND_UNDERLYING, QuoteSnapshot
from options_matrix.share_protocol import (
    PROTOCOL_VERSION,
    FramedSocket,
    new_nonce,
    proofs_match,
    studio_proof,
    viewer_proof,
)
from options_matrix.share_server import ShareHub, ShareServer

PASSWORD = "ABCD-EFGH-JKLM-NPQR"


def _leg(con_id: int, bid: float) -> QuoteSnapshot:
    now = datetime.now(timezone.utc)
    return QuoteSnapshot(label=str(con_id), kind=QUOTE_KIND_LEG, contract_con_id=con_id, bid=bid, ask=bid + 0.05,
                         market_data_type=LIVE_MARKET_DATA_TYPE, bid_confirmed_at=now, ask_confirmed_at=now)


class _RawViewer:
    """A minimal viewer speaking the protocol directly."""

    def __init__(self, port: int, password: str = PASSWORD, protocol: int = PROTOCOL_VERSION) -> None:
        connection = socket.create_connection(("127.0.0.1", port), timeout=5)
        connection.settimeout(0.2)
        self.framed = FramedSocket(connection)
        challenge = self.receive()
        assert challenge["type"] == "challenge"
        self.client_nonce = new_nonce()
        self.server_nonce = challenge["nonce"]
        self.framed.send({
            "type": "hello", "protocol": protocol, "client_nonce": self.client_nonce,
            "proof": viewer_proof(password, self.server_nonce, self.client_nonce), "viewer_name": "test",
        })
        self.reply = self.receive()

    def receive(self, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.framed.receive()
            if message is not None:
                return message
        raise AssertionError("no message")

    def receive_type(self, kind: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.receive(deadline - time.monotonic())
            if message["type"] == kind:
                return message
        raise AssertionError(f"no {kind} message")

    def close(self) -> None:
        self.framed.close()


class ShareServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = ShareHub()
        self.server = ShareServer(
            self.hub, host="127.0.0.1", port=0, password=PASSWORD,
            welcome={"fingerprint": "abc", "studio_name": "mini"},
        )
        self.server.start()
        self.viewers: list[_RawViewer] = []

    def tearDown(self) -> None:
        for viewer in self.viewers:
            viewer.close()
        self.server.stop()

    def connect(self, **kwargs) -> _RawViewer:
        viewer = _RawViewer(self.server.port, **kwargs)
        self.viewers.append(viewer)
        return viewer

    def wait_for_viewers(self, count: int) -> None:
        deadline = time.monotonic() + 5
        while len(self.hub.viewers()) != count and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.hub.viewers()), count)

    def test_correct_password_gets_a_welcome_that_proves_the_mini_pc(self):
        viewer = self.connect()
        self.assertEqual(viewer.reply["type"], "welcome")
        self.assertEqual(viewer.reply["fingerprint"], "abc")
        self.assertTrue(proofs_match(
            studio_proof(PASSWORD, viewer.server_nonce, viewer.client_nonce), viewer.reply["proof"]
        ))
        self.wait_for_viewers(1)

    def test_wrong_password_and_wrong_version_are_refused(self):
        wrong = self.connect(password="NOPE-NOPE-NOPE-NOPE")
        self.assertEqual((wrong.reply["type"], wrong.reply["reason"]), ("denied", "password"))
        old = self.connect(protocol=PROTOCOL_VERSION + 1)
        self.assertEqual((old.reply["type"], old.reply["reason"]), ("denied", "version"))
        time.sleep(0.2)
        self.assertEqual(self.hub.viewers(), [])

    def test_new_viewer_gets_full_state_in_order_then_live_changes(self):
        self.hub.set_settings({"type": "settings", "formula": {}})
        self.hub.set_tabs({"type": "tabs", "tabs": []}, keep_con_ids={1, 2}, keep_underlying="SPY")
        self.hub.set_health({"type": "health"})
        self.hub.update_quotes([_leg(1, 1.0), _leg(2, 2.0), _leg(9, 9.0)])
        self.hub.set_tabs({"type": "tabs", "tabs": [{"id": 1}]}, keep_con_ids={1, 2}, keep_underlying="SPY")

        viewer = self.connect()
        kinds = [viewer.receive()["type"] for _ in range(4)]
        self.assertEqual(kinds, ["settings", "tabs", "health", "quotes"])
        self.wait_for_viewers(1)

        self.hub.update_quotes([_leg(1, 1.5)])
        live = viewer.receive_type("quotes")
        self.assertEqual([item[2] for item in live["quotes"]], [1])

    def test_quotes_outside_the_shared_tabs_are_dropped_from_the_initial_state(self):
        self.hub.update_quotes([_leg(1, 1.0), _leg(9, 9.0)])
        underlying = QuoteSnapshot(label="SPY", kind=QUOTE_KIND_UNDERLYING, contract_con_id=3, mark=600.0)
        self.hub.update_quotes([underlying])
        self.hub.set_tabs({"type": "tabs", "tabs": []}, keep_con_ids={1}, keep_underlying="SPY")
        viewer = self.connect()
        initial = viewer.receive_type("quotes")
        self.assertEqual(sorted(str(item[0]) for item in initial["quotes"]), ["1", "SPY"])

    def test_viewer_limit_is_enforced(self):
        self.server._max_viewers = 1
        self.connect()
        self.wait_for_viewers(1)
        second = self.connect()
        self.assertEqual((second.reply["type"], second.reply["reason"]), ("denied", "full"))

    def test_disconnect_all_closes_viewers(self):
        viewer = self.connect()
        self.wait_for_viewers(1)
        self.hub.disconnect_all()
        with self.assertRaises((EOFError, OSError)):
            for _ in range(50):
                viewer.framed.receive()
        self.wait_for_viewers(0)

    def test_port_in_use_raises(self):
        blocker = ShareServer(ShareHub(), host="127.0.0.1", port=self.server.port, password=PASSWORD, welcome={})
        with self.assertRaises(OSError):
            blocker.start()
        self.assertTrue(blocker.error)


if __name__ == "__main__":
    unittest.main()
