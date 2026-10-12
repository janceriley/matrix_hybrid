"""Network server that shares the mini PC's live matrix with work PC viewers.

The hub keeps the latest tabs, settings, subscription states, health and the
newest quote for every contract. A viewer that signs in receives all of that
first, then every change as it happens. Quote times are encoded when a frame
is actually sent, so a slow connection can never understate a quote's age.
A viewer that falls far behind is disconnected; it reconnects by itself and
starts again from a fresh full state.
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from .models import QUOTE_KIND_UNDERLYING, QuoteSnapshot
from .share_protocol import (
    PROTOCOL_VERSION,
    FramedSocket,
    ProtocolError,
    encode_quote,
    new_nonce,
    proofs_match,
    studio_proof,
    valid_nonce,
    viewer_proof,
)

LOG = logging.getLogger(__name__)

MAX_VIEWERS = 4
VIEWER_QUEUE_LIMIT = 2000
HANDSHAKE_TIMEOUT_SECONDS = 10.0
SEND_TIMEOUT_SECONDS = 10.0
ACCEPT_POLL_SECONDS = 0.5


@dataclass(frozen=True)
class ViewerInfo:
    address: str
    name: str
    connected_at: float


class _QuoteBatch:
    """Queue item holding raw quotes; encoded only when actually sent."""

    __slots__ = ("quotes",)

    def __init__(self, quotes: list[QuoteSnapshot]) -> None:
        self.quotes = quotes


def _quote_key(quote: QuoteSnapshot) -> tuple[str, Any]:
    if quote.kind == QUOTE_KIND_UNDERLYING:
        return (quote.kind, str(quote.label).upper())
    return (quote.kind, quote.contract_con_id)


def _quotes_message(quotes: Iterable[QuoteSnapshot]) -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now()
    return {
        "type": "quotes",
        "quotes": [encode_quote(quote, now_utc=now_utc, now_local=now_local) for quote in quotes],
    }


class ShareHub:
    """Latest shareable state plus the set of signed-in viewers."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._tabs: dict[str, Any] | None = None
        self._settings: dict[str, Any] | None = None
        self._diagnostics: dict[str, Any] | None = None
        self._health: dict[str, Any] | None = None
        self._quotes: dict[tuple[str, Any], QuoteSnapshot] = {}
        self._viewers: set[_ViewerConnection] = set()

    # Updates from the studio (Tk thread) -----------------------------------
    def set_tabs(self, message: dict[str, Any], *, keep_con_ids: Iterable[int] = (), keep_underlying: str | None = None) -> None:
        keep = set(keep_con_ids)
        underlying = keep_underlying.upper() if keep_underlying else None
        with self.lock:
            self._tabs = message
            self._quotes = {
                key: quote
                for key, quote in self._quotes.items()
                if (key[0] == QUOTE_KIND_UNDERLYING and key[1] == underlying)
                or (key[0] != QUOTE_KIND_UNDERLYING and key[1] in keep)
            }
            self._broadcast_locked(message)

    def set_settings(self, message: dict[str, Any]) -> None:
        with self.lock:
            self._settings = message
            self._broadcast_locked(message)

    def set_diagnostics(self, message: dict[str, Any]) -> None:
        with self.lock:
            self._diagnostics = message
            self._broadcast_locked(message)

    def set_health(self, message: dict[str, Any]) -> None:
        with self.lock:
            self._health = message
            self._broadcast_locked(message)

    def update_quotes(self, quotes: Iterable[QuoteSnapshot]) -> None:
        batch = list(quotes)
        if not batch:
            return
        with self.lock:
            for quote in batch:
                self._quotes[_quote_key(quote)] = quote
            self._broadcast_locked(_QuoteBatch(batch))

    # Viewer bookkeeping (server threads) ------------------------------------
    def register(self, viewer: _ViewerConnection) -> None:
        """Queue the full current state, then every later change, for one viewer."""
        with self.lock:
            for message in (self._settings, self._tabs, self._diagnostics, self._health):
                if message is not None:
                    viewer.enqueue(message)
            if self._quotes:
                viewer.enqueue(_QuoteBatch(list(self._quotes.values())))
            self._viewers.add(viewer)

    def unregister(self, viewer: _ViewerConnection) -> None:
        with self.lock:
            self._viewers.discard(viewer)

    def viewers(self) -> list[ViewerInfo]:
        with self.lock:
            return sorted(
                (ViewerInfo(viewer.address, viewer.name, viewer.connected_at) for viewer in self._viewers),
                key=lambda info: info.connected_at,
            )

    def disconnect_all(self) -> None:
        with self.lock:
            viewers = list(self._viewers)
        for viewer in viewers:
            viewer.close("The mini PC ended the connection.")

    def _broadcast_locked(self, item: Any) -> None:
        for viewer in list(self._viewers):
            if not viewer.enqueue(item):
                LOG.warning("Viewer %s fell behind; disconnecting it", viewer.address)
                viewer.close("Too far behind; reconnecting for a fresh copy.")
                self._viewers.discard(viewer)


class _ViewerConnection:
    def __init__(self, connection: socket.socket, address: str) -> None:
        self.socket = connection
        self.framed = FramedSocket(connection)
        self.address = address
        self.name = ""
        self.connected_at = time.time()
        self.queue: queue.Queue[Any] = queue.Queue(VIEWER_QUEUE_LIMIT)
        self.closed = threading.Event()

    def enqueue(self, item: Any) -> bool:
        if self.closed.is_set():
            return True
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            return False
        return True

    def close(self, _reason: str = "") -> None:
        if not self.closed.is_set():
            self.closed.set()
            self.framed.close()


class ShareServer:
    """Listens on every network connection of this PC for work PC viewers."""

    def __init__(
        self,
        hub: ShareHub,
        *,
        port: int,
        password: str,
        welcome: Mapping[str, Any],
        host: str = "0.0.0.0",
        max_viewers: int = MAX_VIEWERS,
        on_viewers_changed: Callable[[], None] | None = None,
    ) -> None:
        self.hub = hub
        self.host = host
        self.port = port
        self._password = password
        self._welcome = dict(welcome)
        self._max_viewers = max_viewers
        self._on_viewers_changed = on_viewers_changed
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_password(self, password: str) -> None:
        self._password = password

    def start(self) -> None:
        """Bind and start accepting viewers; raises OSError if the port is unusable."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((self.host, self.port))
            listener.listen(8)
            listener.settimeout(ACCEPT_POLL_SECONDS)
        except OSError as exc:
            listener.close()
            self.error = str(exc)
            raise
        self.port = listener.getsockname()[1]
        self._listener = listener
        self.error = None
        self._stopping.clear()
        self._thread = threading.Thread(target=self._accept_loop, name="ShareServer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        self.hub.disconnect_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def _notify(self) -> None:
        callback = self._on_viewers_changed
        if callback is not None:
            try:
                callback()
            except Exception:
                LOG.exception("Viewer change callback failed")

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                connection, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                time.sleep(0.2)
                continue
            viewer = _ViewerConnection(connection, f"{address[0]}:{address[1]}")
            threading.Thread(
                target=self._serve, args=(viewer,), name=f"ShareViewer-{address[0]}", daemon=True
            ).start()

    def _handshake(self, viewer: _ViewerConnection) -> bool:
        viewer.socket.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
        nonce = new_nonce()
        viewer.framed.send({"type": "challenge", "protocol": PROTOCOL_VERSION, "nonce": nonce})
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT_SECONDS
        hello = None
        while hello is None and time.monotonic() < deadline:
            hello = viewer.framed.receive()
        if not hello or hello.get("type") != "hello":
            raise ProtocolError("The viewer did not sign in.")
        if hello.get("protocol") != PROTOCOL_VERSION:
            viewer.framed.send({
                "type": "denied",
                "reason": "version",
                "message": (
                    f"The work PC uses link version {hello.get('protocol')}, but this mini PC uses "
                    f"version {PROTOCOL_VERSION}. Update both PCs to the same copy of multi_matrix."
                ),
            })
            return False
        client_nonce = hello.get("client_nonce")
        if not valid_nonce(client_nonce) or not proofs_match(
            viewer_proof(self._password, nonce, client_nonce), hello.get("proof")
        ):
            viewer.framed.send({"type": "denied", "reason": "password",
                                "message": "Wrong password. Check the password shown on the mini PC."})
            return False
        if len(self.hub.viewers()) >= self._max_viewers:
            viewer.framed.send({"type": "denied", "reason": "full",
                                "message": f"The mini PC already has {self._max_viewers} viewers connected."})
            return False
        name = hello.get("viewer_name")
        viewer.name = name[:64] if isinstance(name, str) else ""
        viewer.framed.send({
            **self._welcome,
            "type": "welcome",
            "protocol": PROTOCOL_VERSION,
            "proof": studio_proof(self._password, nonce, client_nonce),
        })
        return True

    def _serve(self, viewer: _ViewerConnection) -> None:
        registered = False
        try:
            if not self._handshake(viewer):
                return
            viewer.socket.settimeout(SEND_TIMEOUT_SECONDS)
            self.hub.register(viewer)
            registered = True
            self._notify()
            reader = threading.Thread(target=self._read_loop, args=(viewer,), daemon=True)
            reader.start()
            while not viewer.closed.is_set() and not self._stopping.is_set():
                try:
                    item = viewer.queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                message = _quotes_message(item.quotes) if isinstance(item, _QuoteBatch) else item
                viewer.framed.send(message)
        except (OSError, EOFError, ProtocolError, ValueError) as exc:
            LOG.info("Viewer %s disconnected: %s", viewer.address, exc)
        finally:
            viewer.close()
            if registered:
                self.hub.unregister(viewer)
                self._notify()

    @staticmethod
    def _read_loop(viewer: _ViewerConnection) -> None:
        """Viewers only send a goodbye; reading also notices a closed link."""
        try:
            while not viewer.closed.is_set():
                try:
                    message = viewer.framed.receive()
                except socket.timeout:
                    continue
                if message is not None and message.get("type") == "goodbye":
                    break
        except (OSError, EOFError, ProtocolError, ValueError):
            pass
        finally:
            viewer.close()
