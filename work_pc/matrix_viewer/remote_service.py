"""The work PC's stand-in for the Schwab service.

The viewer never talks to Schwab. The mini PC is its only market data
source, so this object just reports whether that source is live and quietly
accepts the subscription calls the shared matrix code makes.
"""

from __future__ import annotations

import queue
from datetime import datetime, timezone
from typing import Any, Callable


class RemoteMarketDataService:
    def __init__(self) -> None:
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._generation = 0
        # Signed in to the mini PC and hearing from it regularly.
        self.link_live = False
        # The mini PC itself reports a live market-data connection.
        self.studio_connected = False
        # A connection to the mini PC is running or retrying.
        self.link_active = False
        self.on_disconnect: Callable[[], None] | None = None

    @property
    def connected(self) -> bool:
        return self.link_live and self.studio_connected

    @property
    def connection_active(self) -> bool:
        return self.link_active

    @property
    def quote_generation(self) -> int:
        return self._generation

    def advance_quote_generation(self) -> int:
        self._generation += 1
        return self._generation

    def connect(self, *_args: Any) -> None:
        raise RuntimeError("The work PC connects to the mini PC, not to Schwab.")

    def disconnect(self) -> None:
        callback = self.on_disconnect
        if callback is not None:
            callback()

    def clear_active_quotes(self) -> None:
        return None

    def subscribe_matrix_quotes(self, *_args: Any) -> None:
        return None

    def subscribe_underlying_quote(self, *_args: Any) -> None:
        return None

    def request_current_time(self, timeout: float = 10.0) -> datetime:
        del timeout
        return datetime.now(timezone.utc)

    def chain_underlying_price(self, *_args: Any) -> float | None:
        return None

    def search_ticker(self, *_args: Any) -> Any:
        raise RuntimeError("Tickers are chosen on the mini PC.")

    def load_option_records(self, *_args: Any) -> Any:
        raise RuntimeError("Option chains come from the mini PC.")
