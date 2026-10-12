"""Background connection from the work PC to the mini PC.

The client signs in with the shared password (both sides prove they know it
without sending it), then passes every message to the Tk thread through a
queue. It reconnects by itself after network trouble. A wrong password or a
different program version stops retrying until the settings change.
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from typing import Any

from options_matrix.share_protocol import (
    PROTOCOL_VERSION,
    FramedSocket,
    ProtocolError,
    new_nonce,
    proofs_match,
    studio_proof,
    viewer_proof,
)

LOG = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5.0
HANDSHAKE_TIMEOUT_SECONDS = 10.0
RECEIVE_POLL_SECONDS = 0.5
RETRY_DELAYS_SECONDS = (1.0, 2.0, 3.0, 5.0)
FULL_RETRY_SECONDS = 10.0

# Events posted to the inbound queue as (kind, payload):
EVENT_CONNECTING = "connecting"      # payload: "host:port"
EVENT_CONNECTED = "connected"        # payload: welcome message
EVENT_MESSAGE = "message"            # payload: one message from the mini PC
EVENT_DISCONNECTED = "disconnected"  # payload: reason text; will retry
EVENT_STOPPED = "stopped"            # payload: (reason code, text); no retry


class _Denied(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class ShareClient:
    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        inbound: queue.Queue[tuple[str, Any]],
        *,
        viewer_name: str | None = None,
    ) -> None:
        self.host = host.strip()
        self.port = int(port)
        self._password = password
        self.inbound = inbound
        self.viewer_name = viewer_name or socket.gethostname()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._framed: FramedSocket | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="MiniPcLink", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            framed = self._framed
        if framed is not None:
            try:
                framed.send({"type": "goodbye"})
            except (OSError, ProtocolError):
                pass
            framed.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._thread = None

    def _post(self, kind: str, payload: Any = None) -> None:
        self.inbound.put((kind, payload))

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            self._post(EVENT_CONNECTING, f"{self.host}:{self.port}")
            try:
                self._session()
                attempt = 0
                if not self._stop.is_set():
                    self._post(EVENT_DISCONNECTED, "The mini PC closed the connection.")
            except _Denied as denied:
                if denied.reason == "full":
                    self._post(EVENT_DISCONNECTED, denied.message)
                    if self._stop.wait(FULL_RETRY_SECONDS):
                        break
                    continue
                self._post(EVENT_STOPPED, (denied.reason, denied.message))
                break
            except (OSError, EOFError, ProtocolError, ValueError) as exc:
                if self._stop.is_set():
                    break
                self._post(EVENT_DISCONNECTED, _friendly_error(exc, self.host, self.port))
            finally:
                with self._lock:
                    framed = self._framed
                    self._framed = None
                if framed is not None:
                    framed.close()
            delay = RETRY_DELAYS_SECONDS[min(attempt, len(RETRY_DELAYS_SECONDS) - 1)]
            attempt += 1
            if self._stop.wait(delay):
                break

    def _session(self) -> None:
        connection = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT_SECONDS)
        framed = FramedSocket(connection)
        with self._lock:
            self._framed = framed
        connection.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
        challenge = self._receive_handshake(framed)
        if challenge.get("type") != "challenge":
            raise ProtocolError("The program at that address is not an Options Matrix mini PC.")
        if challenge.get("protocol") != PROTOCOL_VERSION:
            raise _Denied(
                "version",
                f"The mini PC uses link version {challenge.get('protocol')}, this viewer uses "
                f"{PROTOCOL_VERSION}. Copy the same multi_matrix folder to both PCs.",
            )
        server_nonce = challenge.get("nonce")
        if not isinstance(server_nonce, str):
            raise ProtocolError("The mini PC sent an invalid sign-in request.")
        client_nonce = new_nonce()
        framed.send({
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "client_nonce": client_nonce,
            "proof": viewer_proof(self._password, server_nonce, client_nonce),
            "viewer_name": self.viewer_name,
        })
        reply = self._receive_handshake(framed)
        if reply.get("type") == "denied":
            raise _Denied(str(reply.get("reason", "")), str(reply.get("message", "The mini PC refused the connection.")))
        if reply.get("type") != "welcome":
            raise ProtocolError("The mini PC sent an unexpected reply.")
        if not proofs_match(studio_proof(self._password, server_nonce, client_nonce), reply.get("proof")):
            raise _Denied(
                "identity",
                "The program at that address could not prove it knows the password. Check the address.",
            )
        self._post(EVENT_CONNECTED, reply)
        connection.settimeout(RECEIVE_POLL_SECONDS)
        while not self._stop.is_set():
            message = framed.receive()
            if message is not None:
                self._post(EVENT_MESSAGE, message)

    def _receive_handshake(self, framed: FramedSocket) -> dict[str, Any]:
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not self._stop.is_set():
            message = framed.receive()
            if message is not None:
                return message
        raise ProtocolError("The mini PC did not answer the sign-in in time.")


def _friendly_error(exc: BaseException, host: str, port: int) -> str:
    if isinstance(exc, socket.gaierror):
        return f"Cannot find a computer called {host!r} on this network."
    if isinstance(exc, ConnectionRefusedError):
        return (
            f"{host} answered, but nothing is listening on port {port}. Is Options Matrix "
            "Studio open on the mini PC with sharing turned on?"
        )
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return (
            f"No answer from {host}:{port}. Check the address, that the mini PC is on, "
            "and that its firewall allows this port on Private networks."
        )
    if isinstance(exc, EOFError):
        return "The connection to the mini PC closed."
    return f"Connection problem: {exc}"
