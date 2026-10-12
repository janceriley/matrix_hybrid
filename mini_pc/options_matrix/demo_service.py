"""Made-up market data for trying the setup without Schwab (``--demo``).

The demo service has the same surface the app uses from the Schwab service:
ticker search, option chains, quote subscriptions and streamed quote events.
Prices come from a random-walk underlying and a simple Black-Scholes model
with a little noise, so spreads drift around fair value and signals appear.
Nothing here is real market data; demo windows say DEMO DATA in their title.
"""

from __future__ import annotations

import hashlib
import math
import random
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from .models import (
    LIVE_MARKET_DATA_TYPE,
    OptionChainDefinition,
    OptionContractRecord,
    QUOTE_KIND_LEG,
    QUOTE_KIND_UNDERLYING,
    QuoteSnapshot,
    RIGHT_CALL,
    RIGHT_PUT,
    SearchResult,
    UnderlyingContract,
    VerticalSelection,
)
from .recorder import MarketSessionStatus
from .schwab_client import (
    SERVICE_LEVELONE_EQUITIES,
    SERVICE_LEVELONE_OPTIONS,
    CoalescingEventQueue,
    SubscriptionDiagnostic,
)

DEMO_TICK_SECONDS = 0.25
DEMO_EXPIRATION_COUNT = 10
DEMO_STRIKE_RANGE_FRACTION = 0.12
_BASE_PRICES = {"SPY": 600.0, "QQQ": 520.0, "IWM": 220.0, "AAPL": 230.0, "TSLA": 250.0}


def _base_price(symbol: str) -> float:
    if symbol in _BASE_PRICES:
        return _BASE_PRICES[symbol]
    digest = int(hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:8], 16)
    return float(40 + digest % 460)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def black_scholes_price(
    right: str,
    spot: float,
    strike: float,
    years: float,
    volatility: float,
    rate: float = 0.04,
) -> float:
    years = max(years, 1.0 / (365.0 * 24.0))
    sigma_root_t = volatility * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * volatility * volatility) * years) / sigma_root_t
    d2 = d1 - sigma_root_t
    discount = math.exp(-rate * years)
    if right == RIGHT_CALL:
        return spot * _normal_cdf(d1) - strike * discount * _normal_cdf(d2)
    return strike * discount * _normal_cdf(-d2) - spot * _normal_cdf(-d1)


def demo_expirations(today: date, count: int = DEMO_EXPIRATION_COUNT) -> tuple[str, ...]:
    expirations: list[str] = []
    day = today
    while len(expirations) < count:
        if day.weekday() < 5:
            expirations.append(day.strftime("%Y%m%d"))
        day += timedelta(days=1)
    return tuple(expirations)


def _strike_step(price: float) -> float:
    return 0.5 if price < 100 else 1.0


class DemoMarketSessionGuard:
    """Treats the market as open so demo recordings can be tried any time."""

    def status_at(self, now: datetime) -> MarketSessionStatus:
        now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
        return MarketSessionStatus(
            now_utc=now_utc,
            reason="open",
            is_open=True,
            market_open_utc=now_utc - timedelta(hours=1),
            market_close_utc=now_utc + timedelta(hours=6),
            message="Market treated as open (demo).",
        )


class DemoMarketDataService:
    """Stand-in for SchwabMarketDataService that invents live-looking quotes."""

    def __init__(self, *, seed: int | None = None, tick_seconds: float = DEMO_TICK_SECONDS) -> None:
        self.events = CoalescingEventQueue()
        self._random = random.Random(seed)
        self._tick_seconds = tick_seconds
        self._lock = threading.RLock()
        self._connected = False
        self._generation = 0
        self._next_id = 1
        self._symbol_ids: dict[str, int] = {}
        self._spot: dict[str, float] = {}
        self._records: dict[int, OptionContractRecord] = {}
        self._records_by_chain: dict[tuple[str, str], list[OptionContractRecord]] = {}
        self._subscribed_legs: dict[int, OptionContractRecord] = {}
        self._underlying: tuple[str, int] | None = None
        self._leg_noise: dict[int, float] = {}
        self._volume: dict[int, float] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # Connection lifecycle -------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def connection_active(self) -> bool:
        return self._connected

    @property
    def quote_generation(self) -> int:
        with self._lock:
            return self._generation

    def advance_quote_generation(self) -> int:
        with self._lock:
            self._generation += 1
            generation = self._generation
        self._emit_diagnostics()
        return generation

    def connect(self, *_args: Any) -> None:
        with self._lock:
            self._connected = True
            self._stop.clear()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="DemoMarketData", daemon=True)
                self._thread.start()
        self.events.put({"type": "status", "message": "Connected to DEMO market data (made-up prices).",
                         "connected": True, "connection_active": True})

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            self._subscribed_legs.clear()
            self._underlying = None
        self._stop.set()
        self._emit_diagnostics()
        self.events.put({"type": "status", "message": "Disconnected.", "connected": False,
                         "connection_active": False})

    def clear_active_quotes(self) -> None:
        with self._lock:
            self._subscribed_legs.clear()
            self._underlying = None
        self._emit_diagnostics()

    def request_current_time(self, timeout: float = 10.0) -> datetime:
        del timeout
        return datetime.now(timezone.utc)

    # Discovery -------------------------------------------------------------
    def search_ticker(self, symbol: str) -> SearchResult:
        self._ensure_connected()
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("Enter a ticker symbol before searching.")
        with self._lock:
            con_id = self._symbol_id(f"EQ:{normalized}")
            self._spot.setdefault(normalized, _base_price(normalized))
        underlying = UnderlyingContract(normalized, con_id, "DEMO", "DEMO", "USD")
        chain = OptionChainDefinition(
            symbol=normalized,
            underlying_con_id=con_id,
            exchange="DEMO",
            trading_class=normalized,
            multiplier="100",
            expirations=demo_expirations(datetime.now().date()),
            strikes=(),
        )
        return SearchResult(underlying=underlying, chain=chain)

    def load_option_records(self, search_result: SearchResult, expiration: str) -> list[OptionContractRecord]:
        self._ensure_connected()
        symbol = search_result.underlying.symbol
        key = (symbol, expiration)
        with self._lock:
            cached = self._records_by_chain.get(key)
            if cached is not None:
                return list(cached)
            spot = self._spot.setdefault(symbol, _base_price(symbol))
            step = _strike_step(spot)
            low = math.floor(spot * (1 - DEMO_STRIKE_RANGE_FRACTION) / step) * step
            high = math.ceil(spot * (1 + DEMO_STRIKE_RANGE_FRACTION) / step) * step
            records: list[OptionContractRecord] = []
            strike = low
            while strike <= high + 1e-9:
                for right in (RIGHT_CALL, RIGHT_PUT):
                    local_symbol = f"{symbol} {expiration[2:]}{right[0]}{int(round(strike * 1000)):08d}"
                    record = OptionContractRecord(
                        con_id=self._symbol_id(local_symbol),
                        symbol=symbol,
                        expiration=expiration,
                        strike=round(strike, 4),
                        right=right,
                        exchange="DEMO",
                        currency="USD",
                        trading_class=symbol,
                        multiplier="100",
                        local_symbol=local_symbol,
                    )
                    self._records[record.con_id] = record
                    records.append(record)
                strike = round(strike + step, 4)
            self._records_by_chain[key] = records
            return list(records)

    def chain_underlying_price(self, search_result: SearchResult, expiration: str) -> float | None:
        del expiration
        with self._lock:
            return self._spot.get(search_result.underlying.symbol)

    # Subscriptions ---------------------------------------------------------
    def subscribe_matrix_quotes(self, search_result: SearchResult, selections: Sequence[VerticalSelection]) -> None:
        self._ensure_connected()
        legs: dict[int, OptionContractRecord] = {}
        for selection in selections:
            for leg in selection.legs:
                legs.setdefault(leg.contract.con_id, leg.contract)
        with self._lock:
            self._subscribed_legs = legs
            for con_id in legs:
                self._leg_noise.setdefault(con_id, self._random.gauss(0.0, 0.04))
                self._volume.setdefault(con_id, float(self._random.randint(0, 4000)))
            self._underlying = (
                (search_result.underlying.symbol, search_result.underlying.con_id) if legs else None
            )
        self._emit_diagnostics()
        self._emit_quotes()

    def subscribe_underlying_quote(self, search_result: SearchResult) -> None:
        with self._lock:
            self._underlying = (search_result.underlying.symbol, search_result.underlying.con_id)
        self._emit_diagnostics()

    # Internals -------------------------------------------------------------
    def _ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("Connect to the demo market data first.")

    def _symbol_id(self, symbol: str) -> int:
        existing = self._symbol_ids.get(symbol)
        if existing is not None:
            return existing
        assigned = self._next_id
        self._next_id += 1
        self._symbol_ids[symbol] = assigned
        return assigned

    def _run(self) -> None:
        while not self._stop.wait(self._tick_seconds):
            if not self._connected:
                continue
            self._advance_prices()
            self._emit_quotes()

    def _advance_prices(self) -> None:
        with self._lock:
            for symbol, spot in list(self._spot.items()):
                shock = self._random.gauss(0.0, 0.00025)
                self._spot[symbol] = max(1.0, spot * math.exp(shock))
            for con_id in list(self._leg_noise):
                # Mean-reverting mispricing of each option, so spreads wander
                # around fair value and BUY/SELL signals come and go.
                drift = self._leg_noise[con_id] * 0.95 + self._random.gauss(0.0, 0.015)
                self._leg_noise[con_id] = max(-0.18, min(0.18, drift))
            for con_id in list(self._volume):
                if self._random.random() < 0.05:
                    self._volume[con_id] += self._random.randint(1, 20)

    def _option_quote(self, record: OptionContractRecord, spot: float, now: datetime) -> QuoteSnapshot:
        expiry = datetime.strptime(record.expiration, "%Y%m%d").replace(hour=20, tzinfo=timezone.utc)
        years = max((expiry - now).total_seconds(), 3600.0) / (365.0 * 24 * 3600)
        moneyness = math.log(record.strike / spot)
        volatility = max(0.08, 0.17 - 0.35 * moneyness + 0.9 * moneyness * moneyness)
        fair = max(0.0, black_scholes_price(record.right, spot, record.strike, years, volatility))
        mid = max(0.0, fair + self._leg_noise.get(record.con_id, 0.0))
        half = max(0.005, round(min(0.15, 0.01 + mid * 0.015), 2) / 2)
        bid = max(0.0, round(mid - half, 2))
        ask = max(bid + 0.01, round(mid + half, 2))
        local = now.astimezone().replace(tzinfo=None)
        return QuoteSnapshot(
            label=str(record.con_id),
            kind=QUOTE_KIND_LEG,
            contract_con_id=record.con_id,
            bid=bid,
            ask=ask,
            last=round(mid, 2),
            mark=round((bid + ask) / 2, 4),
            bid_size=float(self._random.randint(1, 250)),
            ask_size=float(self._random.randint(1, 250)),
            implied_volatility=round(volatility, 6),
            model_option_price=round(fair, 4),
            volume=self._volume.get(record.con_id, 0.0),
            open_interest=float(1000 + record.con_id % 9000),
            market_data_type=LIVE_MARKET_DATA_TYPE,
            quote_time=now,
            updated_at=local,
            bid_update_received_at=local,
            ask_update_received_at=local,
            bid_confirmed_at=now,
            ask_confirmed_at=now,
        )

    def _emit_quotes(self) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            if not self._connected:
                return
            generation = self._generation
            quotes: list[QuoteSnapshot] = []
            if self._underlying is not None:
                symbol, con_id = self._underlying
                spot = self._spot.get(symbol)
                if spot is not None:
                    quotes.append(
                        QuoteSnapshot(
                            label=symbol,
                            kind=QUOTE_KIND_UNDERLYING,
                            contract_con_id=con_id,
                            bid=round(spot - 0.01, 2),
                            ask=round(spot + 0.01, 2),
                            last=round(spot, 2),
                            mark=round(spot, 4),
                            bid_size=float(self._random.randint(1, 20) * 100),
                            ask_size=float(self._random.randint(1, 20) * 100),
                            volume=float(10_000_000 + int(time.time()) % 1_000_000),
                            market_data_type=LIVE_MARKET_DATA_TYPE,
                            quote_time=now,
                            updated_at=now.astimezone().replace(tzinfo=None),
                        )
                    )
            for record in self._subscribed_legs.values():
                spot = self._spot.get(record.symbol)
                if spot is not None:
                    quotes.append(self._option_quote(record, spot, now))
        for quote in quotes:
            self.events.put({"type": "quote", "quote": quote, "generation": generation})

    def _emit_diagnostics(self) -> None:
        with self._lock:
            generation = self._generation
            diagnostics = [
                SubscriptionDiagnostic(
                    service=SERVICE_LEVELONE_OPTIONS,
                    symbol=record.local_symbol,
                    state="subscribed",
                    desired=True,
                    confirmed=True,
                    contract_con_id=con_id,
                    quote_kind=QUOTE_KIND_LEG,
                    generation=generation,
                )
                for con_id, record in self._subscribed_legs.items()
            ]
            if self._underlying is not None:
                symbol, con_id = self._underlying
                diagnostics.append(
                    SubscriptionDiagnostic(
                        service=SERVICE_LEVELONE_EQUITIES,
                        symbol=symbol,
                        state="subscribed",
                        desired=True,
                        confirmed=True,
                        contract_con_id=con_id,
                        quote_kind=QUOTE_KIND_UNDERLYING,
                        generation=generation,
                    )
                )
        self.events.put({"type": "subscription_status", "generation": generation,
                         "subscriptions": tuple(diagnostics)})
