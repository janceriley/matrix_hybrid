"""Manual spread positions and midpoint-based exit targets.

Position records are independent of visible matrix tabs. Exit targets use the
configured width edge, but do not depend on a fair-value fit or an entry signal.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import (
    FormulaSettings,
    MonitoredSideSnapshot,
    SIGNAL_BUY,
    SIGNAL_SELL,
    VerticalSelection,
    fair_value_edge_for_width,
    is_supported_matrix_strike,
    is_supported_spread_width,
    normalize_right,
)

PositionKey = tuple[str, str, str, float, float]
POSITION_BOUGHT = "BOUGHT"
POSITION_SOLD = "SOLD"
POSITION_STORE_VERSION = 1


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number.")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number.")
    return round(number, 4)


def _normalized_position_key(value: object) -> PositionKey:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 5:
        raise ValueError("A position needs ticker, expiration, right, and two strikes.")
    ticker, expiration, right, raw_lower, raw_upper = value
    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError("A position needs a ticker.")
    if not isinstance(expiration, str) or len(expiration.strip()) != 8:
        raise ValueError("A position expiration must use YYYYMMDD.")
    expiration = expiration.strip()
    try:
        datetime.strptime(expiration, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("A position expiration must use a valid YYYYMMDD date.") from exc
    if not isinstance(right, str):
        raise ValueError("A position right must be CALL or PUT.")
    normalized_right = normalize_right(right)
    lower = _finite_number(raw_lower, "Lower strike")
    upper = _finite_number(raw_upper, "Upper strike")
    if (
        lower < 0.0
        or lower >= upper
        or not is_supported_matrix_strike(lower)
        or not is_supported_matrix_strike(upper)
        or not is_supported_spread_width(upper - lower)
    ):
        raise ValueError("The position's strike pair is not supported by the matrix.")
    return ticker.strip().upper(), expiration, normalized_right, lower, upper


@dataclass(frozen=True)
class PositionRecord:
    key: PositionKey
    side: str
    average_price: float
    armed: bool = True

    def __post_init__(self) -> None:
        key = _normalized_position_key(self.key)
        side = self.side.strip().upper() if isinstance(self.side, str) else ""
        if side not in {POSITION_BOUGHT, POSITION_SOLD}:
            raise ValueError("Position side must be BOUGHT or SOLD.")
        average = _finite_number(self.average_price, "Average price")
        if average <= 0.0:
            raise ValueError("Average price must be greater than zero.")
        if not isinstance(self.armed, bool):
            raise ValueError("Position armed flag must be a boolean.")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "average_price", average)


@dataclass(frozen=True)
class PositionExit:
    target_price: float
    edge: float
    mid: float | None
    profit: float | None
    signal: str | None
    status: str


def position_key_for_selection(selection: VerticalSelection) -> PositionKey:
    """Use contract identity, never a tab id or a session-specific contract id."""
    pair = selection.pair
    lower_key = _normalized_position_key(
        (pair.lower.symbol, pair.lower.expiration, pair.right, pair.lower.strike, pair.upper.strike)
    )
    if (
        pair.upper.symbol.strip().upper() != lower_key[0]
        or pair.upper.expiration.strip() != lower_key[1]
        or normalize_right(pair.lower.right) != lower_key[2]
        or normalize_right(pair.upper.right) != lower_key[2]
    ):
        raise ValueError("Both position legs must have the same ticker, expiration, and right.")
    return lower_key


def evaluate_position(
    position: PositionRecord,
    snapshot: MonitoredSideSnapshot | None,
    settings: FormulaSettings,
    *,
    live: bool,
) -> PositionExit:
    """Evaluate an exit using the displayed spread midpoint and current edge.

    The caller supplies live quote eligibility, including freshness. The
    signal reflects whether the current midpoint has reached the target.
    Targets are not clamped: doing so could silently reduce requested profit.
    """
    width = round(position.key[4] - position.key[3], 4)
    edge = fair_value_edge_for_width(width, settings)
    sold = position.side == POSITION_SOLD
    target = round(position.average_price - edge if sold else position.average_price + edge, 4)
    mid: float | None = None
    if live and snapshot is not None:
        try:
            matching = position_key_for_selection(snapshot.selection) == position.key
            if matching and snapshot.synthetic_mid is not None:
                mid = _finite_number(snapshot.synthetic_mid, "Spread midpoint")
        except (TypeError, ValueError, AttributeError):
            mid = None
    profit = (
        round(position.average_price - mid if sold else mid - position.average_price, 4)
        if mid is not None
        else None
    )
    signal: str | None = None
    if not position.armed:
        status = "paused"
    elif mid is None:
        status = "waiting"
    elif (sold and mid <= target) or (not sold and mid >= target):
        status = "ready"
        signal = SIGNAL_BUY if sold else SIGNAL_SELL
    else:
        status = "monitoring"
    return PositionExit(target, edge, mid, profit, signal, status)


def position_store_path(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    appdata = (env.get("APPDATA") or "").strip()
    if appdata:
        return Path(appdata) / "OptionsMatrix" / "positions.json"
    return (home or Path.home()) / ".options_matrix_positions.json"


def load_positions(path: Path | None = None) -> dict[PositionKey, PositionRecord]:
    """Recover valid entries while ignoring malformed or unsupported records."""
    source = Path(path) if path is not None else position_store_path()
    try:
        with source.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(payload, Mapping)
        or type(payload.get("version")) is not int
        or payload.get("version") != POSITION_STORE_VERSION
        or not isinstance(payload.get("positions"), list)
    ):
        return {}
    positions: dict[PositionKey, PositionRecord] = {}
    for entry in payload["positions"]:
        if not isinstance(entry, Mapping):
            continue
        try:
            record = PositionRecord(
                key=_normalized_position_key(entry.get("key")),
                side=entry.get("side"),
                average_price=entry.get("average_price"),
                armed=entry.get("armed", True),
            )
        except (TypeError, ValueError, OverflowError):
            continue
        positions[record.key] = record
    return positions


def save_positions(
    records: Mapping[PositionKey, PositionRecord],
    path: Path | None = None,
) -> None:
    """Atomically replace the independent position store, including an empty one."""
    entries: list[dict[str, object]] = []
    for key, record in sorted(records.items()):
        if not isinstance(record, PositionRecord) or key != record.key:
            raise ValueError("Position store keys must match their records.")
        entries.append(
            {
                "key": list(record.key),
                "side": record.side,
                "average_price": record.average_price,
                "armed": record.armed,
            }
        )
    destination = Path(path) if path is not None else position_store_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump({"version": POSITION_STORE_VERSION, "positions": entries}, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
