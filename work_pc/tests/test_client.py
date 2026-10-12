from __future__ import annotations

import queue
import socket
import threading
import time
import unittest

from options_matrix.share_protocol import FramedSocket, new_nonce
from options_matrix.share_server import ShareHub, ShareServer

from matrix_viewer.client import (
    EVENT_CONNECTED,
    EVENT_DISCONNECTED,
    EVENT_MESSAGE,
    EVENT_STOPPED,
    ShareClient,
)

PASSWORD = "ABCD-EFGH-JKLM-NPQR"


def _wait_for(events: queue.Queue, kind: str, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event_kind, payload = events.get(timeout=0.1)
        except queue.Empty:
            continue
        if event_kind == kind:
            return payload
    raise AssertionError(f"no {kind} event")


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class ShareClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = ShareHub()
        self.hub.set_settings({"type": "settings", "formula": {}})
        self.server = ShareServer(self.hub, host="127.0.0.1", port=0, password=PASSWORD,
                                  welcome={"fingerprint": "f", "studio_name": "mini"})
        self.server.start()
        self.events: queue.Queue = queue.Queue()
        self.clients: list[ShareClient] = []

    def tearDown(self) -> None:
        for client in self.clients:
            client.stop()
        self.server.stop()

    def client(self, password: str = PASSWORD, port: int | None = None) -> ShareClient:
        client = ShareClient("127.0.0.1", port or self.server.port, password, self.events, viewer_name="work")
        self.clients.append(client)
        client.start()
        return client

    def test_signs_in_and_receives_messages(self):
        self.client()
        welcome = _wait_for(self.events, EVENT_CONNECTED)
        self.assertEqual(welcome["studio_name"], "mini")
        message = _wait_for(self.events, EVENT_MESSAGE)
        self.assertEqual(message["type"], "settings")
        self.assertEqual([viewer.name for viewer in self.hub.viewers()], ["work"])

    def test_wrong_password_stops_retrying(self):
        client = self.client(password="WRONG-WRONG-WRONG")
        reason, text = _wait_for(self.events, EVENT_STOPPED)
        self.assertEqual(reason, "password")
        self.assertIn("Wrong password", text)
        time.sleep(0.3)
        self.assertFalse(client.running)

    def test_unreachable_mini_pc_reports_a_plain_message_and_keeps_trying(self):
        client = self.client(port=_free_port())
        text = _wait_for(self.events, EVENT_DISCONNECTED)
        self.assertIn("nothing is listening", text)
        self.assertTrue(client.running)

    def test_reconnects_after_the_mini_pc_restarts(self):
        port = self.server.port
        self.client()
        _wait_for(self.events, EVENT_CONNECTED)
        self.server.stop()
        _wait_for(self.events, EVENT_DISCONNECTED)
        self.server = ShareServer(self.hub, host="127.0.0.1", port=port, password=PASSWORD,
                                  welcome={"fingerprint": "f", "studio_name": "mini"})
        self.server.start()
        _wait_for(self.events, EVENT_CONNECTED, timeout=15)

    def test_impostor_that_cannot_prove_the_password_is_rejected(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def impostor():
            connection, _ = listener.accept()
            connection.settimeout(5)
            framed = FramedSocket(connection)
            framed.send({"type": "challenge", "protocol": 1, "nonce": new_nonce()})
            while framed.receive() is None:
                pass
            framed.send({"type": "welcome", "protocol": 1, "proof": "0" * 64})
            time.sleep(1)
            framed.close()

        thread = threading.Thread(target=impostor, daemon=True)
        thread.start()
        self.client(port=port)
        reason, text = _wait_for(self.events, EVENT_STOPPED)
        self.assertEqual(reason, "identity")
        self.assertIn("could not prove", text)
        listener.close()


if __name__ == "__main__":
    unittest.main()
