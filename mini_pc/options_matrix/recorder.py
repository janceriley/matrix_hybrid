from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    LIVE_MARKET_DATA_TYPE,
    DEFAULT_SIGNAL_METHOD,
    FormulaSettings,
    MonitoredSideSnapshot,
    QuoteSnapshot,
    VerticalSelection,
    build_matrix_snapshots,
    build_synthetic_vertical_quote,
    is_supported_matrix_pair,
    normalize_signal_method,
    pair_key_for_pair,
)

SNAPSHOT_COLUMNS = (
    "snapshot_id",
    "session_id",
    "timestamp_utc",
    "timestamp_local",
    "ticker",
    "expiration",
    "option_type/right",
    "lower_strike",
    "higher_strike",
    "dte",
    "underlying_price",
    "underlying_volume",
    "lower_bid",
    "lower_ask",
    "lower_mid",
    "higher_bid",
    "higher_ask",
    "higher_mid",
    "lower_bid_size",
    "lower_ask_size",
    "higher_bid_size",
    "higher_ask_size",
    "lower_volume",
    "higher_volume",
    "lower_open_interest",
    "higher_open_interest",
    "lower_implied_vol",
    "higher_implied_vol",
    "lower_model_option_price",
    "higher_model_option_price",
    "market_data_type",
    "quote_age_seconds",
    "bid_ask_timestamp_mismatch",
    "synthetic_spread_bid",
    "synthetic_spread_ask",
    "synthetic_spread_mid",
    "synthetic_spread_bid_size",
    "synthetic_spread_ask_size",
    "signal_method",
    "time_bucket",
    "minute_from_open",
    "session_period",
)

try:
    MARKET_TIMEZONE = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    MARKET_TIMEZONE = timezone.utc

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
_ONE_DAY = timedelta(days=1)
_FOURTEEN_DAYS = timedelta(days=14)
_UNKNOWN_RECORDING_TICKER_LABEL = "UNKNOWN"
_MULTI_RECORDING_TICKER_LABEL = "MULTI"
_RECORDING_TICKER_SEGMENT_PATTERN = re.compile(r"[^A-Z0-9._-]+")


@dataclass(frozen=True)
class RecorderStatus:
    running: bool
    session_id: str | None
    session_dir: Path | None
    current_part_path: Path | None
    rows_enqueued: int
    rows_written: int
    last_snapshot_rows: int
    started_at: datetime | None = None
    last_write_at: datetime | None = None
    error: str | None = None
    stopping: bool = False
    writer_alive: bool = False
    flush_complete: bool = True
    backlog_batches: int = 0
    backlog_capacity_batches: int = 0
    backlog_capacity_rows: int = 0
    backlog_error: str | None = None

    @property
    def pending_rows(self) -> int:
        return max(0, self.rows_enqueued - self.rows_written)


@dataclass(frozen=True)
class MarketSessionStatus:
    now_utc: datetime
    reason: str
    is_open: bool = False
    market_open_utc: datetime | None = None
    market_close_utc: datetime | None = None
    next_open_utc: datetime | None = None
    message: str = ""


class MarketSessionGuard:
    def __init__(self, calendar: object | None = None, calendar_name: str = "NYSE") -> None:
        self.calendar_name = calendar_name
        self.calendar = calendar if calendar is not None else self._load_calendar(calendar_name)
        self._session_cache: dict[date, tuple[datetime, datetime] | None] = {}
        self._next_open_cache: dict[date, datetime | None] = {}

    def status_at(self, now: datetime) -> MarketSessionStatus:
        now_utc = _to_utc_datetime(now) or datetime.now(timezone.utc)
        market_date = now_utc.astimezone(MARKET_TIMEZONE).date()
        session = self._session_for_date(market_date)
        if session is None:
            next_open = self._next_open_after(market_date)
            return MarketSessionStatus(
                now_utc=now_utc,
                reason="no_session",
                next_open_utc=next_open,
                message=(
                    f"NYSE market is closed today. Next open: {_format_market_time(next_open)}."
                    if next_open is not None
                    else "NYSE market is closed today. No next open found in the next 14 days."
                ),
            )

        market_open, market_close = session
        if now_utc < market_open:
            return MarketSessionStatus(
                now_utc=now_utc,
                reason="pre_open",
                market_open_utc=market_open,
                market_close_utc=market_close,
                next_open_utc=market_open,
                message=(
                    f"NYSE market is not open yet. Recording can start at {_format_market_time(market_open)} "
                    f"and will stop at {_format_market_time(market_close)}."
                ),
            )
        if now_utc < market_close:
            return MarketSessionStatus(
                now_utc=now_utc,
                reason="open",
                is_open=True,
                market_open_utc=market_open,
                market_close_utc=market_close,
                message=f"NYSE market is open until {_format_market_time(market_close)}.",
            )

        next_open = self._next_open_after(market_date)
        return MarketSessionStatus(
            now_utc=now_utc,
            reason="after_close",
            market_open_utc=market_open,
            market_close_utc=market_close,
            next_open_utc=next_open,
            message=(
                f"NYSE market closed at {_format_market_time(market_close)}. "
                f"Next open: {_format_market_time(next_open)}."
                if next_open is not None
                else f"NYSE market closed at {_format_market_time(market_close)}."
            ),
        )

    @staticmethod
    def _load_calendar(calendar_name: str) -> object:
        try:
            import pandas_market_calendars as mcal
        except ModuleNotFoundError as exc:
            if exc.name != "pandas_market_calendars":
                raise
            raise RuntimeError(
                "Missing dependency `pandas_market_calendars`. Install project dependencies with "
                "`pip install -e .` before starting the recorder."
            ) from exc
        return mcal.get_calendar(calendar_name)

    def _session_for_date(self, market_date: date) -> tuple[datetime, datetime] | None:
        if market_date in self._session_cache:
            return self._session_cache[market_date]
        rows = self._schedule_rows(market_date, market_date)
        session = rows[0] if rows else None
        self._session_cache[market_date] = session
        return session

    def _next_open_after(self, market_date: date) -> datetime | None:
        if market_date in self._next_open_cache:
            return self._next_open_cache[market_date]
        search_start = market_date + _ONE_DAY
        search_end = market_date + _FOURTEEN_DAYS
        rows = self._schedule_rows(search_start, search_end)
        next_open = rows[0][0] if rows else None
        self._next_open_cache[market_date] = next_open
        return next_open

    def _schedule_rows(self, start_date: date, end_date: date) -> list[tuple[datetime, datetime]]:
        schedule = self.calendar.schedule(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )
        rows: list[tuple[datetime, datetime]] = []
        for row in _iter_schedule_rows(schedule):
            market_open = _coerce_schedule_datetime(row["market_open"])
            market_close = _coerce_schedule_datetime(row["market_close"])
            if market_open is not None and market_close is not None:
                rows.append((market_open, market_close))
        rows.sort(key=lambda item: item[0])
        return rows


def normalize_recording_ticker_label(value: object) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return _UNKNOWN_RECORDING_TICKER_LABEL
    safe = _RECORDING_TICKER_SEGMENT_PATTERN.sub("-", raw).strip(" .-_")
    return safe or _UNKNOWN_RECORDING_TICKER_LABEL


def recording_ticker_label_from_tabs(tabs: Sequence[object]) -> str:
    labels: set[str] = set()
    for tab in tabs:
        if getattr(tab, "is_stale", False):
            continue
        matrix_input = getattr(tab, "matrix_input", None)
        labels.add(normalize_recording_ticker_label(getattr(matrix_input, "ticker_symbol", "")))
    if not labels:
        return _UNKNOWN_RECORDING_TICKER_LABEL
    if len(labels) > 1:
        return _MULTI_RECORDING_TICKER_LABEL
    return next(iter(labels))


class SnapshotRowBuilder:
    def __init__(
        self,
        formula_settings: FormulaSettings | None = None,
        *,
        signal_method: str | None = None,
    ) -> None:
        self.formula_settings = formula_settings
        self.signal_method = normalize_signal_method(signal_method or DEFAULT_SIGNAL_METHOD)

    def build_rows(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        captured_at: datetime,
        tabs: Sequence[object],
        underlying_quote: QuoteSnapshot | None,
    ) -> list[dict[str, Any]]:
        captured_utc = _to_utc_datetime(captured_at) or datetime.now(timezone.utc)
        market_dt = captured_utc.astimezone(MARKET_TIMEZONE)
        live_underlying_price = _underlying_reference_price(underlying_quote)
        underlying_volume = underlying_quote.volume if underlying_quote is not None else None
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, float, float]] = set()

        for tab in tabs:
            if getattr(tab, "is_stale", False):
                continue
            matrix_input = getattr(tab, "matrix_input", None)
            matrix_plan = getattr(tab, "matrix_plan", None)
            if matrix_input is None or matrix_plan is None:
                continue
            leg_quotes = getattr(tab, "leg_quotes", {})
            underlying_price = live_underlying_price
            if underlying_price is None:
                underlying_price = _positive_finite_price(
                    getattr(tab, "underlying_price_hint", None)
                )
            visible_snapshots = build_matrix_snapshots(
                matrix_plan,
                leg_quotes,
                formula_settings=self.formula_settings,
                signal_method=self.signal_method,
                underlying_price=underlying_price,
            )

            # The visible plan is the recording contract. Hidden same-side
            # curve context may shape these fair values, but it never becomes
            # matrix rows of its own.
            for cell_key, selection in matrix_plan.visible.items():
                if not is_supported_matrix_pair(selection.pair):
                    continue
                snapshot = visible_snapshots.get(cell_key)
                if snapshot is None:
                    # Be defensive about manually assembled plans: a visible
                    # selection still gets a row even if it was omitted from
                    # the calibration context or has no usable quote.
                    snapshot = MonitoredSideSnapshot(
                        title=selection.pair.label,
                        pair_key=pair_key_for_pair(selection.pair),
                        selection=selection,
                        is_center=True,
                        synthetic_quote=build_synthetic_vertical_quote(selection, leg_quotes),
                    )
                row_key = (
                    getattr(matrix_input, "ticker_symbol", ""),
                    getattr(matrix_input, "raw_expiration", ""),
                    snapshot.selection.pair.right,
                    float(snapshot.selection.pair.lower.strike),
                    float(snapshot.selection.pair.upper.strike),
                )
                if row_key in seen:
                    continue
                seen.add(row_key)
                rows.append(
                    self._build_row(
                        session_id=session_id,
                        snapshot_id=snapshot_id,
                        captured_utc=captured_utc,
                        market_dt=market_dt,
                        matrix_input=matrix_input,
                        snapshot=snapshot,
                        leg_quotes=leg_quotes,
                        underlying_price=underlying_price,
                        underlying_volume=underlying_volume,
                    )
                )
        return rows

    def _build_row(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        captured_utc: datetime,
        market_dt: datetime,
        matrix_input: object,
        snapshot: MonitoredSideSnapshot,
        leg_quotes: Mapping[int, QuoteSnapshot],
        underlying_price: float | None,
        underlying_volume: float | None,
    ) -> dict[str, Any]:
        pair = snapshot.selection.pair
        synthetic_quote = snapshot.synthetic_quote
        lower_quote = leg_quotes.get(pair.lower.con_id)
        higher_quote = leg_quotes.get(pair.upper.con_id)
        synthetic_mid = synthetic_quote.mid if synthetic_quote is not None else None

        row = {column: None for column in SNAPSHOT_COLUMNS}
        row.update(
            {
                "snapshot_id": snapshot_id,
                "session_id": session_id,
                "timestamp_utc": captured_utc,
                "timestamp_local": captured_utc.astimezone().isoformat(timespec="microseconds"),
                "ticker": getattr(matrix_input, "ticker_symbol", ""),
                "expiration": getattr(matrix_input, "raw_expiration", ""),
                "option_type/right": pair.right,
                "lower_strike": float(pair.lower.strike),
                "higher_strike": float(pair.upper.strike),
                "dte": _days_to_expiration(getattr(matrix_input, "raw_expiration", ""), market_dt.date()),
                "underlying_price": underlying_price,
                "underlying_volume": underlying_volume,
                "lower_bid": lower_quote.bid if lower_quote is not None else None,
                "lower_ask": lower_quote.ask if lower_quote is not None else None,
                "lower_mid": lower_quote.mid if lower_quote is not None else None,
                "higher_bid": higher_quote.bid if higher_quote is not None else None,
                "higher_ask": higher_quote.ask if higher_quote is not None else None,
                "higher_mid": higher_quote.mid if higher_quote is not None else None,
                "lower_bid_size": lower_quote.bid_size if lower_quote is not None else None,
                "lower_ask_size": lower_quote.ask_size if lower_quote is not None else None,
                "higher_bid_size": higher_quote.bid_size if higher_quote is not None else None,
                "higher_ask_size": higher_quote.ask_size if higher_quote is not None else None,
                "lower_volume": lower_quote.volume if lower_quote is not None else None,
                "higher_volume": higher_quote.volume if higher_quote is not None else None,
                "lower_open_interest": lower_quote.open_interest if lower_quote is not None else None,
                "higher_open_interest": higher_quote.open_interest if higher_quote is not None else None,
                "lower_implied_vol": lower_quote.implied_volatility if lower_quote is not None else None,
                "higher_implied_vol": higher_quote.implied_volatility if higher_quote is not None else None,
                "lower_model_option_price": (
                    lower_quote.model_option_price if lower_quote is not None else None
                ),
                "higher_model_option_price": (
                    higher_quote.model_option_price if higher_quote is not None else None
                ),
                "market_data_type": _spread_market_data_type(lower_quote, higher_quote),
                "quote_age_seconds": _spread_quote_age_seconds(captured_utc, lower_quote, higher_quote),
                "bid_ask_timestamp_mismatch": _spread_bid_ask_timestamp_mismatch(
                    snapshot.selection,
                    leg_quotes,
                ),
                "synthetic_spread_bid": synthetic_quote.bid if synthetic_quote is not None else None,
                "synthetic_spread_ask": synthetic_quote.ask if synthetic_quote is not None else None,
                "synthetic_spread_mid": synthetic_mid,
                "synthetic_spread_bid_size": synthetic_quote.bid_size if synthetic_quote is not None else None,
                "synthetic_spread_ask_size": synthetic_quote.ask_size if synthetic_quote is not None else None,
                "signal_method": self.signal_method,
                "time_bucket": market_dt.strftime("%Y-%m-%d %H:%M"),
                "minute_from_open": _minute_from_open(market_dt),
                "session_period": _session_period(market_dt),
            }
        )
        return row


def _to_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc)


def _iter_schedule_rows(schedule: object) -> list[Mapping[str, object]]:
    if schedule is None:
        return []
    if hasattr(schedule, "empty") and getattr(schedule, "empty"):
        return []
    if hasattr(schedule, "iterrows"):
        return [
            {"market_open": row["market_open"], "market_close": row["market_close"]}
            for _index, row in schedule.iterrows()
        ]
    if isinstance(schedule, Mapping):
        if "market_open" in schedule and "market_close" in schedule:
            return [schedule]
        return [
            row
            for row in schedule.values()
            if isinstance(row, Mapping) and "market_open" in row and "market_close" in row
        ]
    rows: list[Mapping[str, object]] = []
    for row in schedule:
        if isinstance(row, Mapping) and "market_open" in row and "market_close" in row:
            rows.append(row)
    return rows


def _coerce_schedule_datetime(value: object) -> datetime | None:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return _to_utc_datetime(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return _to_utc_datetime(parsed)
    return None


def _format_market_time(value: datetime | None) -> str:
    if value is None:
        return "--"
    market_time = value.astimezone(MARKET_TIMEZONE)
    return market_time.strftime("%Y-%m-%d %I:%M %p ET")


def _positive_finite_price(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0.0 else None


def _underlying_reference_price(quote: QuoteSnapshot | None) -> float | None:
    if quote is None:
        return None
    for candidate in (quote.mark, quote.mid, quote.last):
        price = _positive_finite_price(candidate)
        if price is not None:
            return price
    return None


def _days_to_expiration(raw_expiration: str, market_date: date) -> int | None:
    expiration_date = _parse_expiration_date(raw_expiration)
    if expiration_date is None:
        return None
    return (expiration_date - market_date).days


def _parse_expiration_date(raw_expiration: str) -> date | None:
    raw = (raw_expiration or "").strip()
    if len(raw) < 8 or not raw[:8].isdigit():
        return None
    try:
        return date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def _spread_market_data_type(
    lower_quote: QuoteSnapshot | None,
    higher_quote: QuoteSnapshot | None,
) -> int | None:
    if lower_quote is None or higher_quote is None:
        return None
    worst = LIVE_MARKET_DATA_TYPE
    for quote in (lower_quote, higher_quote):
        if quote.market_data_type is not None and quote.market_data_type != LIVE_MARKET_DATA_TYPE:
            worst = quote.market_data_type
    return worst


def _spread_quote_age_seconds(
    captured_utc: datetime,
    lower_quote: QuoteSnapshot | None,
    higher_quote: QuoteSnapshot | None,
) -> float | None:
    if lower_quote is None or higher_quote is None:
        return None
    lower_at = _leg_freshness(lower_quote)
    higher_at = _leg_freshness(higher_quote)
    if lower_at is None or higher_at is None:
        return None
    oldest = min(lower_at, higher_at)
    return round(max(0.0, (captured_utc - oldest).total_seconds()), 6)


def _leg_freshness(quote: QuoteSnapshot) -> datetime | None:
    """Market-side quote time when the streamer supplied it, else receive time."""
    return _to_utc_datetime(quote.quote_time) or _to_utc_datetime(quote.updated_at)


def _spread_bid_ask_timestamp_mismatch(
    selection: VerticalSelection,
    leg_quotes: Mapping[int, QuoteSnapshot],
) -> bool | None:
    """True when any required spread-price contribution has a different timestamp."""
    contribution_times: list[datetime] = []
    for leg in selection.legs:
        quote = leg_quotes.get(leg.contract.con_id)
        if quote is None:
            return None
        if leg.action == "BUY":
            leg_bid_at = _to_utc_datetime(quote.bid_update_received_at)
            leg_ask_at = _to_utc_datetime(quote.ask_update_received_at)
        else:
            leg_bid_at = _to_utc_datetime(quote.ask_update_received_at)
            leg_ask_at = _to_utc_datetime(quote.bid_update_received_at)
        if leg_bid_at is None or leg_ask_at is None:
            return None
        contribution_times.extend((leg_bid_at, leg_ask_at))
    if not contribution_times:
        return None
    first_timestamp = contribution_times[0]
    return any(timestamp != first_timestamp for timestamp in contribution_times[1:])


def _minute_from_open(market_dt: datetime) -> int:
    open_dt = market_dt.replace(hour=REGULAR_OPEN.hour, minute=REGULAR_OPEN.minute, second=0, microsecond=0)
    return int((market_dt - open_dt).total_seconds() // 60)


def _session_period(market_dt: datetime) -> str:
    current_time = market_dt.time()
    if current_time < REGULAR_OPEN:
        return "PRE_MARKET"
    if current_time <= REGULAR_CLOSE:
        return "REGULAR"
    return "POST_MARKET"
