"""Messages between the mini PC (studio) and work PC viewers.

Transport: length-prefixed UTF-8 JSON over TCP on the home network. The
password never travels: both sides prove they know it with HMAC answers to
random challenges. Traffic is not encrypted, so keep it on a trusted home
network or a private VPN such as Tailscale, and never forward the port on a
router.

Receive-time stamps are sent as ages ("received 120 ms before this message
was sent") and rebuilt on the viewer's own clock, so a clock difference
between the two PCs cannot make old prices look fresh.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import socket
import struct
import threading
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    OptionChainDefinition,
    OptionContractRecord,
    QuoteSnapshot,
    SearchResult,
    UnderlyingContract,
)
from .schwab_client import SubscriptionDiagnostic

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 32 * 1024 * 1024
NONCE_BYTES = 16

# Files whose code decides plans, fair values and signals. Both PCs must run
# identical copies for the work PC's numbers to match the mini PC's exactly.
MATH_SOURCE_FILES = ("models.py", "app.py")


class ProtocolError(ValueError):
    pass


def math_fingerprint(package_dir: Path | None = None) -> str:
    """Short hash of the code that computes the matrix numbers."""
    directory = package_dir or Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in MATH_SOURCE_FILES:
        digest.update(name.encode("utf-8"))
        # Normalize line endings so a git checkout setting cannot cause a mismatch.
        digest.update((directory / name).read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:16]


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------
def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"Unsupported message value: {type(value).__name__}")


def encode_frame(message: Mapping[str, Any]) -> bytes:
    payload = json.dumps(
        message, default=_json_default, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("Message exceeds the size limit.")
    return struct.pack("!I", len(payload)) + payload


class FramedSocket:
    """Send and receive whole messages; partial frames survive timeouts."""

    def __init__(self, connection: socket.socket) -> None:
        self.socket = connection
        self._buffer = bytearray()
        self._send_lock = threading.Lock()

    def send(self, message: Mapping[str, Any]) -> None:
        frame = encode_frame(message)
        with self._send_lock:
            self.socket.sendall(frame)

    def receive(self) -> dict[str, Any] | None:
        """Return one message, or None if the read timed out first."""
        while True:
            if len(self._buffer) >= 4:
                size = struct.unpack("!I", self._buffer[:4])[0]
                if not 0 < size <= MAX_FRAME_BYTES:
                    raise ProtocolError("Invalid message size.")
                if len(self._buffer) >= size + 4:
                    payload = bytes(self._buffer[4 : size + 4])
                    del self._buffer[: size + 4]
                    try:
                        message = json.loads(payload)
                    except (ValueError, UnicodeError) as exc:
                        raise ProtocolError("Invalid message JSON.") from exc
                    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                        raise ProtocolError("Messages must have a type.")
                    return message
            try:
                chunk = self.socket.recv(262144)
            except socket.timeout:
                return None
            if not chunk:
                raise EOFError("Connection closed.")
            self._buffer.extend(chunk)

    def close(self) -> None:
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.socket.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Password proofs (mutual: the viewer proves first, then the mini PC)
# --------------------------------------------------------------------------
def new_nonce() -> str:
    return secrets.token_hex(NONCE_BYTES)


def _proof(password: str, role: str, first: str, second: str) -> str:
    message = f"options-matrix-share:{role}:{first}:{second}".encode("utf-8")
    return hmac.new(password.encode("utf-8"), message, hashlib.sha256).hexdigest()


def viewer_proof(password: str, server_nonce: str, client_nonce: str) -> str:
    return _proof(password, "viewer", server_nonce, client_nonce)


def studio_proof(password: str, server_nonce: str, client_nonce: str) -> str:
    return _proof(password, "studio", client_nonce, server_nonce)


def proofs_match(expected: str, received: object) -> bool:
    return isinstance(received, str) and hmac.compare_digest(expected, received)


def valid_nonce(value: object) -> bool:
    if not isinstance(value, str) or len(value) != NONCE_BYTES * 2:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------
# Codecs
# --------------------------------------------------------------------------
def encode_record(record: OptionContractRecord) -> list[Any]:
    return [
        record.con_id, record.symbol, record.expiration, record.strike, record.right,
        record.exchange, record.currency, record.trading_class, record.multiplier,
        record.local_symbol,
    ]


def decode_record(value: Sequence[Any]) -> OptionContractRecord:
    if not isinstance(value, Sequence) or len(value) != 10:
        raise ProtocolError("Invalid option contract.")
    con_id, symbol, expiration, strike, right, exchange, currency, trading_class, multiplier, local = value
    if type(con_id) is not int or not isinstance(strike, (int, float)):
        raise ProtocolError("Invalid option contract.")
    return OptionContractRecord(
        con_id=con_id, symbol=str(symbol), expiration=str(expiration), strike=float(strike),
        right=str(right), exchange=str(exchange), currency=str(currency),
        trading_class=str(trading_class), multiplier=str(multiplier), local_symbol=str(local),
    )


def encode_search_result(result: SearchResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    underlying, chain = result.underlying, result.chain
    return {
        "underlying": [underlying.symbol, underlying.con_id, underlying.exchange,
                       underlying.primary_exchange, underlying.currency],
        "chain": [chain.symbol, chain.underlying_con_id, chain.exchange, chain.trading_class,
                  chain.multiplier, list(chain.expirations), list(chain.strikes)],
    }


def decode_search_result(value: Mapping[str, Any] | None) -> SearchResult | None:
    if value is None:
        return None
    try:
        symbol, con_id, exchange, primary, currency = value["underlying"]
        c_symbol, c_con_id, c_exchange, trading_class, multiplier, expirations, strikes = value["chain"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError("Invalid ticker details.") from exc
    return SearchResult(
        underlying=UnderlyingContract(str(symbol), int(con_id), str(exchange), str(primary), str(currency)),
        chain=OptionChainDefinition(
            symbol=str(c_symbol), underlying_con_id=int(c_con_id), exchange=str(c_exchange),
            trading_class=str(trading_class), multiplier=str(multiplier),
            expirations=tuple(str(item) for item in expirations),
            strikes=tuple(float(item) for item in strikes),
        ),
    )


_AGE_FIELDS = (
    "updated_at",
    "bid_update_received_at",
    "ask_update_received_at",
    "bid_confirmed_at",
    "ask_confirmed_at",
)
_NUMBER_FIELDS = (
    "bid", "ask", "last", "mark", "bid_size", "ask_size", "implied_volatility",
    "model_option_price", "volume", "open_interest",
)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _age_ms(value: datetime | None, now_utc: datetime, now_local: datetime) -> list[int] | None:
    """[age in ms, 1 if timezone-aware else 0]; ages are never negative."""
    if value is None:
        return None
    if value.tzinfo is None:
        delta = now_local - value
        aware = 0
    else:
        delta = now_utc - value.astimezone(timezone.utc)
        aware = 1
    return [max(0, int(delta.total_seconds() * 1000)), aware]


def _from_age(value: Any, now_utc: datetime, now_local: datetime) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, Sequence) or len(value) != 2:
        raise ProtocolError("Invalid quote time.")
    age, aware = value
    if type(age) is not int or age < 0:
        raise ProtocolError("Invalid quote time.")
    offset = timedelta(milliseconds=age)
    return (now_utc if aware else now_local) - offset


def encode_quote(quote: QuoteSnapshot, *, now_utc: datetime, now_local: datetime) -> list[Any]:
    quote_time = quote.quote_time
    return [
        quote.label,
        quote.kind,
        quote.contract_con_id,
        [_number(getattr(quote, name)) for name in _NUMBER_FIELDS],
        quote.market_data_type,
        None if quote_time is None else int(quote_time.astimezone(timezone.utc).timestamp() * 1000),
        [_age_ms(getattr(quote, name), now_utc, now_local) for name in _AGE_FIELDS],
    ]


def decode_quote(value: Sequence[Any], *, now_utc: datetime, now_local: datetime) -> QuoteSnapshot:
    try:
        label, kind, con_id, numbers, market_data_type, quote_time_ms, ages = value
    except (TypeError, ValueError) as exc:
        raise ProtocolError("Invalid quote.") from exc
    if con_id is not None and type(con_id) is not int:
        raise ProtocolError("Invalid quote contract.")
    if len(numbers) != len(_NUMBER_FIELDS) or len(ages) != len(_AGE_FIELDS):
        raise ProtocolError("Invalid quote fields.")
    values: dict[str, Any] = {name: _number(number) for name, number in zip(_NUMBER_FIELDS, numbers)}
    for name, age in zip(_AGE_FIELDS, ages):
        values[name] = _from_age(age, now_utc, now_local)
    return QuoteSnapshot(
        label=str(label),
        kind=str(kind),
        contract_con_id=con_id,
        market_data_type=market_data_type if type(market_data_type) is int else None,
        quote_time=(
            None if quote_time_ms is None
            else datetime.fromtimestamp(float(quote_time_ms) / 1000.0, tz=timezone.utc)
        ),
        **values,
    )


def encode_diagnostic(diagnostic: SubscriptionDiagnostic) -> list[Any]:
    return [
        diagnostic.service, diagnostic.symbol, diagnostic.state, diagnostic.desired,
        diagnostic.error, diagnostic.pending_command, diagnostic.confirmed,
        diagnostic.contract_con_id, diagnostic.quote_kind,
    ]


def decode_diagnostic(value: Sequence[Any], *, generation: int) -> SubscriptionDiagnostic:
    try:
        service, symbol, state, desired, error, pending, confirmed, con_id, quote_kind = value
    except (TypeError, ValueError) as exc:
        raise ProtocolError("Invalid subscription status.") from exc
    return SubscriptionDiagnostic(
        service=str(service), symbol=str(symbol), state=str(state), desired=bool(desired),
        error=None if error is None else str(error),
        pending_command=None if pending is None else str(pending),
        confirmed=bool(confirmed),
        contract_con_id=con_id if type(con_id) is int else None,
        quote_kind=None if quote_kind is None else str(quote_kind),
        generation=generation,
    )


def encode_matrix_input(matrix_input: Any) -> dict[str, Any]:
    return {
        "display_expiration": matrix_input.display_expiration,
        "raw_expiration": matrix_input.raw_expiration,
        "side": matrix_input.side,
        "low_strike": matrix_input.low_strike,
        "high_strike": matrix_input.high_strike,
        "widths": list(matrix_input.widths),
        "ticker_symbol": matrix_input.ticker_symbol,
        "matrix_mode": matrix_input.matrix_mode,
        "width_bands": None if matrix_input.width_bands is None else list(matrix_input.width_bands),
    }


def matrix_input_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validated keyword arguments for ``MatrixInputState``."""
    try:
        return {
            "display_expiration": str(value["display_expiration"]),
            "raw_expiration": str(value["raw_expiration"]),
            "side": str(value["side"]),
            "low_strike": float(value["low_strike"]),
            "high_strike": float(value["high_strike"]),
            "widths": tuple(float(width) for width in value["widths"]),
            "ticker_symbol": str(value["ticker_symbol"]),
            "matrix_mode": str(value["matrix_mode"]),
            "width_bands": (
                None if value.get("width_bands") is None
                else tuple(float(band) for band in value["width_bands"])
            ),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError("Invalid matrix tab settings.") from exc


def records_key(symbol: str, expiration: str) -> str:
    return f"{symbol}|{expiration}"


def encode_records(records: Iterable[OptionContractRecord]) -> list[list[Any]]:
    return [encode_record(record) for record in records]


def dataclass_dict(value: Any) -> dict[str, Any]:
    return {field.name: getattr(value, field.name) for field in fields(value)}
