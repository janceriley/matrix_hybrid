"""Schwab Trader API market-data service.

Replaces the IBKR/TWS data layer while keeping the same public surface that
``app.py`` consumes:

- REST discovery: ``GET /expirationchain`` for expirations and ``GET /chains``
  for the option contracts of one expiration.
- Live quotes: Schwab Streamer WebSocket with ``LEVELONE_OPTIONS`` for legs
  and ``LEVELONE_EQUITIES`` for the underlying.
- Vertical spread quotes are synthesized from the two leg quotes in the
  models layer (Schwab has no BAG-style streaming combo quote); this service
  only streams legs and the underlying.

Credentials are never hard-coded. They are read from a JSON credentials file
(pointed at by the ``SCHWAB_TOKEN_PATH`` environment variable) holding
``{"app_key": ..., "app_secret": ..., "refresh_token": ...}``, with the
``SCHWAB_APP_KEY`` / ``SCHWAB_APP_SECRET`` / ``SCHWAB_REFRESH_TOKEN``
environment variables taking precedence when set. Placeholder values are
rejected with an instructive error. Keep the credentials file out of source
control (it is gitignored).

This module does not place, stage, or route orders.
"""

from __future__ import annotations

import base64
import json
import math
import os
import queue
import threading
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

try:
    import websocket  # type: ignore  # provided by the `websocket-client` package
except ModuleNotFoundError as exc:
    if exc.name != "websocket":
        raise
    websocket = None
    _WEBSOCKET_IMPORT_ERROR = exc
else:
    _WEBSOCKET_IMPORT_ERROR = None

from .models import (
    LIVE_MARKET_DATA_TYPE,
    OptionChainDefinition,
    OptionContractRecord,
    QUOTE_KIND_LEG,
    QUOTE_KIND_UNDERLYING,
    QuoteSnapshot,
    SearchResult,
    UnderlyingContract,
    VerticalSelection,
    normalize_right,
)

MARKET_DATA_BASE_URL = "https://api.schwabapi.com/marketdata/v1"
OAUTH_TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
USER_PREFERENCE_URL = "https://api.schwabapi.com/trader/v1/userPreference"

ENV_APP_KEY = "SCHWAB_APP_KEY"
ENV_APP_SECRET = "SCHWAB_APP_SECRET"
ENV_REFRESH_TOKEN = "SCHWAB_REFRESH_TOKEN"
ENV_TOKEN_PATH = "SCHWAB_TOKEN_PATH"

_PLACEHOLDER_VALUES = {
    "",
    "YOUR_SCHWAB_APP_KEY",
    "YOUR_SCHWAB_APP_SECRET",
    "YOUR_SCHWAB_REFRESH_TOKEN",
}

DELAYED_MARKET_DATA_TYPE = 3
REST_TIMEOUT_SECONDS = 20.0
STREAMER_LOGIN_TIMEOUT_SECONDS = 15.0
ACCESS_TOKEN_REFRESH_MARGIN_SECONDS = 120.0
REST_MAX_ATTEMPTS = 4
REST_RETRY_BASE_SECONDS = 0.5
REST_RETRY_MAX_SECONDS = 30.0
REST_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})

STREAMER_MAX_SYMBOLS_PER_REQUEST = 500
STREAMER_MAX_RECONNECT_ATTEMPTS = 5
STREAMER_MAX_RECONNECT_DELAY_SECONDS = 30.0
STREAMER_RECONNECT_STABILITY_SECONDS = 30.0

EXPIRATION_CACHE_MAX_ENTRIES = 32

SERVICE_LEVELONE_OPTIONS = "LEVELONE_OPTIONS"
SERVICE_LEVELONE_EQUITIES = "LEVELONE_EQUITIES"
SERVICE_ADMIN = "ADMIN"

# LEVELONE_OPTIONS streamer fields (see Schwab Streamer API docs):
# 0 Symbol, 2 Bid, 3 Ask, 4 Last, 8 Total Volume, 9 Open Interest,
# 10 Volatility, 16 Bid Size, 17 Ask Size, 34 Theoretical Option Value,
# 37 Mark, 38 Quote Time.
LEVELONE_OPTIONS_FIELDS = "0,2,3,4,8,9,10,16,17,34,37,38"

# LEVELONE_EQUITIES streamer fields:
# 0 Symbol, 1 Bid, 2 Ask, 3 Last, 4 Bid Size, 5 Ask Size, 8 Total Volume,
# 33 Mark, 34 Quote Time.
LEVELONE_EQUITIES_FIELDS = "0,1,2,3,4,5,8,33,34"

_OPTION_FIELD_MAP = {
    "2": "bid",
    "3": "ask",
    "4": "last",
    "8": "volume",
    "9": "open_interest",
    "16": "bid_size",
    "17": "ask_size",
    "37": "mark",
}
_EQUITY_FIELD_MAP = {
    "1": "bid",
    "2": "ask",
    "3": "last",
    "4": "bid_size",
    "5": "ask_size",
    "8": "volume",
    "33": "mark",
}


class SchwabConfigurationError(RuntimeError):
    """Raised when Schwab credentials are missing or still placeholders."""


class SchwabRequestError(RuntimeError):
    """Raised when a Schwab REST or streamer request fails."""


@dataclass(frozen=True)
class SubscriptionDiagnostic:
    """Read-only state for one desired or server-confirmed stream symbol."""

    service: str
    symbol: str
    state: str
    desired: bool
    error: str | None = None
    pending_command: str | None = None
    confirmed: bool = False
    contract_con_id: int | None = None
    quote_kind: str | None = None
    generation: int | None = None

    @property
    def is_underlying(self) -> bool:
        return self.service == SERVICE_LEVELONE_EQUITIES

    @property
    def failed(self) -> bool:
        return self.error is not None


@dataclass(frozen=True)
class _QuoteEventMarker:
    """Private queue token; consumers always receive its current quote event."""


class CoalescingEventQueue:
    """Queue-compatible event channel that coalesces quote bursts by contract.

    At most one physical quote marker is queued.  The latest cumulative quote
    for each dirty contract is retained in insertion order and yielded one at a
    time.  Non-quote events use the ordinary FIFO unchanged, so status and
    subscription-state events are never coalesced or discarded.
    """

    def __init__(self) -> None:
        self.maxsize = 0
        self._queue: queue.Queue[Any] = queue.Queue()
        self._quote_lock = threading.Lock()
        self._pending_quotes: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
        self._quote_marker_queued = False

    def put(
        self,
        item: dict[str, Any],
        block: bool = True,
        timeout: float | None = None,
    ) -> None:
        quote_key = self._quote_key(item)
        if quote_key is None:
            self._queue.put(item, block=block, timeout=timeout)
            return
        enqueue_marker = False
        with self._quote_lock:
            self._pending_quotes[quote_key] = item
            if not self._quote_marker_queued:
                self._quote_marker_queued = True
                enqueue_marker = True
        if enqueue_marker:
            # The backing queue is unbounded. Quote memory is bounded by the
            # number of distinct dirty contracts, never by message rate.
            self._queue.put(_QuoteEventMarker())

    def put_nowait(self, item: dict[str, Any]) -> None:
        self.put(item, block=False)

    def get(self, block: bool = True, timeout: float | None = None) -> dict[str, Any]:
        item = self._queue.get(block=block, timeout=timeout)
        if not isinstance(item, _QuoteEventMarker):
            return item

        requeue_marker = False
        with self._quote_lock:
            _key, event = self._pending_quotes.popitem(last=False)
            if self._pending_quotes:
                requeue_marker = True
            else:
                self._quote_marker_queued = False
        if requeue_marker:
            # Rotate behind queued status events to keep lifecycle information
            # responsive even during a broad market-data burst.
            self._queue.put(_QuoteEventMarker())
        return event

    def get_nowait(self) -> dict[str, Any]:
        return self.get(block=False)

    def empty(self) -> bool:
        return self.qsize() == 0

    def full(self) -> bool:
        return False

    def qsize(self) -> int:
        with self._quote_lock:
            pending_quote_count = len(self._pending_quotes)
            marker_count = 1 if self._quote_marker_queued else 0
        return self._queue.qsize() - marker_count + pending_quote_count

    def task_done(self) -> None:
        self._queue.task_done()

    def join(self) -> None:
        self._queue.join()

    @staticmethod
    def _quote_key(item: Mapping[str, Any]) -> tuple[Any, ...] | None:
        if item.get("type") != "quote":
            return None
        quote = item.get("quote")
        if not isinstance(quote, QuoteSnapshot):
            return None
        return (item.get("generation"), quote.kind, quote.contract_con_id, quote.label)


def build_option_symbol(root: str, expiration: str, right: str, strike: float) -> str:
    """Build a Schwab-standard option symbol: RRRRRRYYMMDDsWWWWWddd.

    ``expiration`` is YYYYMMDD, ``right`` is CALL/PUT/C/P, strike is dollars.
    Example: ``AAPL  251219C00200000``.
    """
    padded_root = f"{root.strip().upper():<6}"
    raw_expiration = expiration.strip()
    if len(raw_expiration) < 8 or not raw_expiration[:8].isdigit():
        raise ValueError(f"Unsupported option expiration: {expiration!r}")
    yymmdd = raw_expiration[2:8]
    side = "C" if normalize_right(right) == "CALL" else "P"
    strike_thousandths = int(round(float(strike) * 1000))
    if strike_thousandths < 0 or strike_thousandths > 99999999:
        raise ValueError(f"Unsupported option strike: {strike!r}")
    return f"{padded_root}{yymmdd}{side}{strike_thousandths:08d}"


def parse_option_symbol(symbol: str) -> tuple[str, str, str, float]:
    """Parse a Schwab option symbol into (root, expiration YYYYMMDD, right, strike)."""
    if len(symbol) != 21:
        raise ValueError(f"Unsupported option symbol: {symbol!r}")
    root = symbol[:6].strip()
    yymmdd = symbol[6:12]
    side = symbol[12]
    strike_text = symbol[13:21]
    if not yymmdd.isdigit() or side not in {"C", "P"} or not strike_text.isdigit():
        raise ValueError(f"Unsupported option symbol: {symbol!r}")
    expiration = f"20{yymmdd}"
    right = "CALL" if side == "C" else "PUT"
    return root, expiration, right, int(strike_text) / 1000.0


def expiration_to_iso(expiration: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD (pass through when already ISO)."""
    raw = (expiration or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def expiration_to_compact(expiration: str) -> str:
    """YYYY-MM-DD -> YYYYMMDD (pass through when already compact)."""
    raw = (expiration or "").strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return raw.replace("-", "")
    return raw


@dataclass(frozen=True)
class SchwabCredentials:
    app_key: str
    app_secret: str
    refresh_token: str

    @staticmethod
    def from_environment(environ: Mapping[str, str] | None = None) -> "SchwabCredentials":
        env = os.environ if environ is None else environ

        token_path = (env.get(ENV_TOKEN_PATH) or "").strip()
        default_path = default_credentials_path()
        configured_path = Path(token_path) if token_path else None
        if (
            configured_path is not None
            and _paths_match(
                configured_path,
                default_path.parent.parent / "schwab_token.json",
            )
            and not configured_path.expanduser().is_file()
            and default_path.is_file()
        ):
            # Migrate the former root-level location after the authorization
            # files moved into schwab_auth/.
            token_path = str(default_path)
        if not token_path and environ is None:
            # No explicit override: fall back to the project-local
            # schwab_auth/schwab_token.json so no environment setup is needed.
            # Skipped when a test passes an explicit environ mapping.
            if default_path.is_file():
                token_path = str(default_path)
        file_values = _read_credentials_file(Path(token_path)) if token_path else {}

        app_key = (env.get(ENV_APP_KEY) or "").strip() or file_values.get("app_key", "")
        app_secret = (env.get(ENV_APP_SECRET) or "").strip() or file_values.get("app_secret", "")
        refresh_token = (env.get(ENV_REFRESH_TOKEN) or "").strip() or file_values.get("refresh_token", "")

        missing = [
            name
            for name, value in (
                ("app_key", app_key),
                ("app_secret", app_secret),
                ("refresh_token", refresh_token),
            )
            if value in _PLACEHOLDER_VALUES
        ]
        if missing:
            file_hint = token_path or str(default_credentials_path())
            raise SchwabConfigurationError(
                f"Schwab credentials are not configured (missing: {', '.join(missing)}). "
                f"Fill in {file_hint} with your app_key, app_secret, and refresh_token "
                f"(or set the {ENV_APP_KEY}/{ENV_APP_SECRET}/{ENV_REFRESH_TOKEN} environment "
                "variables). Create an app at developer.schwab.com and complete the one-time "
                "OAuth authorization to obtain a refresh token. Never commit these values."
            )
        return SchwabCredentials(app_key=app_key, app_secret=app_secret, refresh_token=refresh_token)


def default_credentials_path() -> Path:
    """Project-local Schwab authorization token file."""
    return Path(__file__).resolve().parent.parent / "schwab_auth" / "schwab_token.json"


def _paths_match(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve(strict=False) == right.resolve(strict=False)
    except (OSError, ValueError):
        return False


def refresh_token_issued_at(path: Path | None = None) -> datetime | None:
    """UTC timestamp authorize_schwab.py wrote with the refresh token, if any.

    Schwab refresh tokens expire after 7 days; the app uses this to warn ahead
    of the weekly re-authorization. Returns None when the file or key is
    missing (e.g. env-var credentials or a token saved before this was added).
    """
    token_path = path or default_credentials_path()
    try:
        with token_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("refresh_token_issued_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_credentials_file(path: Path) -> dict[str, str]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    values: dict[str, str] = {}
    for key in ("app_key", "app_secret", "refresh_token"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            values[key] = value.strip()
    return values


class SchwabTokenManager:
    """Caches the OAuth access token and refreshes it with the refresh token."""

    def __init__(
        self,
        credentials: SchwabCredentials | None = None,
        *,
        token_url: str = OAUTH_TOKEN_URL,
        timeout: float = REST_TIMEOUT_SECONDS,
    ) -> None:
        self._credentials = credentials
        self._token_url = token_url
        self._timeout = timeout
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @property
    def credentials(self) -> SchwabCredentials:
        if self._credentials is None:
            self._credentials = SchwabCredentials.from_environment()
        return self._credentials

    def access_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            now = _time.monotonic()
            if (
                not force_refresh
                and self._access_token
                and now < self._expires_at - ACCESS_TOKEN_REFRESH_MARGIN_SECONDS
            ):
                return self._access_token
            payload = self._request_token()
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise SchwabRequestError("Schwab token endpoint returned no access_token.")
            expires_in = payload.get("expires_in")
            try:
                lifetime = float(expires_in)
            except (TypeError, ValueError):
                lifetime = 1800.0
            self._access_token = token
            self._expires_at = _time.monotonic() + lifetime
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._access_token = None
            self._expires_at = 0.0

    def _request_token(self) -> dict[str, Any]:
        credentials = self.credentials
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": credentials.refresh_token,
            }
        ).encode("ascii")
        basic = base64.b64encode(
            f"{credentials.app_key}:{credentials.app_secret}".encode("ascii")
        ).decode("ascii")
        request = urllib.request.Request(
            self._token_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        for attempt in range(REST_MAX_ATTEMPTS):
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in REST_RETRYABLE_HTTP_CODES and attempt + 1 < REST_MAX_ATTEMPTS:
                    _time.sleep(_retry_after_seconds(exc.headers, attempt))
                    continue
                detail = _read_http_error(exc)
                raise SchwabRequestError(
                    f"Schwab OAuth token refresh failed (HTTP {exc.code}): {detail}. "
                    "If the refresh token expired (7-day lifetime), redo the one-time "
                    "OAuth authorization and update the environment variables."
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt + 1 < REST_MAX_ATTEMPTS:
                    _time.sleep(_retry_after_seconds(None, attempt))
                    continue
                raise SchwabRequestError(f"Schwab OAuth token refresh failed: {exc}") from exc
        raise SchwabRequestError("Schwab OAuth token refresh failed: retry limit exhausted.")


def _read_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read()
        if (exc.headers.get("Content-Encoding") or "").lower() == "gzip" or body[:2] == b"\x1f\x8b":
            import gzip

            body = gzip.decompress(body)
        return body.decode("utf-8", errors="replace")[:400]
    except Exception:
        return exc.reason if isinstance(exc.reason, str) else str(exc.reason)


def _retry_after_seconds(headers: Any, attempt: int) -> float:
    """Bounded Retry-After delay, falling back to exponential backoff."""
    fallback = min(REST_RETRY_MAX_SECONDS, REST_RETRY_BASE_SECONDS * (2.0**attempt))
    if headers is None:
        return fallback
    raw = headers.get("Retry-After")
    if raw is None:
        return fallback
    text = str(raw).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(text)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return fallback
    return min(REST_RETRY_MAX_SECONDS, max(0.0, seconds))


class SchwabRestClient:
    """Thin JSON client for the Schwab market-data REST endpoints."""

    def __init__(
        self,
        token_manager: SchwabTokenManager,
        *,
        base_url: str = MARKET_DATA_BASE_URL,
        user_preference_url: str = USER_PREFERENCE_URL,
        timeout: float = REST_TIMEOUT_SECONDS,
    ) -> None:
        self._tokens = token_manager
        self._base_url = base_url.rstrip("/")
        self._user_preference_url = user_preference_url
        self._timeout = timeout

    def get_expiration_chain(self, symbol: str) -> dict[str, Any]:
        return self._get_json(f"{self._base_url}/expirationchain", {"symbol": symbol})

    def get_chain(
        self,
        symbol: str,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        contract_type: str = "ALL",
    ) -> dict[str, Any]:
        params: dict[str, str] = {
            "symbol": symbol,
            "contractType": contract_type,
            "strategy": "SINGLE",
            "includeUnderlyingQuote": "true",
        }
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date
        return self._get_json(f"{self._base_url}/chains", params)

    def get_quotes(self, symbols: Sequence[str], *, fields: str = "quote,reference") -> dict[str, Any]:
        return self._get_json(
            f"{self._base_url}/quotes",
            {"symbols": ",".join(symbols), "fields": fields},
        )

    def get_user_preferences(self) -> dict[str, Any]:
        return self._get_json(self._user_preference_url, None)

    def server_time(self) -> datetime:
        """Schwab server time from the HTTP Date header of a lightweight request."""
        _, headers = self._get(f"{self._base_url}/markets", {"markets": "equity"})
        date_header = headers.get("Date") if headers is not None else None
        if date_header:
            try:
                parsed = parsedate_to_datetime(date_header)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except (TypeError, ValueError):
                pass
        return datetime.now(timezone.utc)

    def _get_json(self, url: str, params: Mapping[str, str] | None) -> dict[str, Any]:
        body, _headers = self._get(url, params)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SchwabRequestError(f"Schwab returned a non-JSON response from {url}.") from exc
        if not isinstance(payload, dict):
            raise SchwabRequestError(f"Schwab returned an unexpected payload from {url}.")
        return payload

    def _get(self, url: str, params: Mapping[str, str] | None) -> tuple[str, Any]:
        full_url = url if not params else f"{url}?{urllib.parse.urlencode(params)}"
        force_refresh = False
        refreshed_after_unauthorized = False
        for attempt in range(REST_MAX_ATTEMPTS):
            token = self._tokens.access_token(force_refresh=force_refresh)
            force_refresh = False
            request = urllib.request.Request(
                full_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    return response.read().decode("utf-8"), response.headers
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and not refreshed_after_unauthorized:
                    self._tokens.invalidate()
                    refreshed_after_unauthorized = True
                    force_refresh = True
                    continue
                if exc.code in REST_RETRYABLE_HTTP_CODES and attempt + 1 < REST_MAX_ATTEMPTS:
                    _time.sleep(_retry_after_seconds(exc.headers, attempt))
                    continue
                detail = _read_http_error(exc)
                raise SchwabRequestError(f"Schwab request failed (HTTP {exc.code}) for {url}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt + 1 < REST_MAX_ATTEMPTS:
                    _time.sleep(_retry_after_seconds(None, attempt))
                    continue
                raise SchwabRequestError(f"Schwab request failed for {url}: {exc}") from exc
        raise SchwabRequestError(f"Schwab request failed for {url}: retry limit exhausted.")


@dataclass(frozen=True)
class StreamerInfo:
    socket_url: str
    customer_id: str
    correl_id: str
    channel: str
    function_id: str

    @staticmethod
    def from_user_preferences(payload: Mapping[str, Any]) -> "StreamerInfo":
        entries = payload.get("streamerInfo")
        if isinstance(entries, Sequence) and entries and isinstance(entries[0], Mapping):
            entry = entries[0]
            socket_url = str(entry.get("streamerSocketUrl") or "")
            customer_id = str(entry.get("schwabClientCustomerId") or "")
            correl_id = str(entry.get("schwabClientCorrelId") or "")
            channel = str(entry.get("schwabClientChannel") or "N9")
            function_id = str(entry.get("schwabClientFunctionId") or "APIAPP")
            if socket_url and customer_id and correl_id:
                return StreamerInfo(
                    socket_url=socket_url,
                    customer_id=customer_id,
                    correl_id=correl_id,
                    channel=channel,
                    function_id=function_id,
                )
        raise SchwabRequestError(
            "Schwab user preferences did not include streamer connection info "
            "(streamerInfo[0].streamerSocketUrl / schwabClientCustomerId / schwabClientCorrelId)."
        )


@dataclass(frozen=True)
class _PendingSubscriptionRequest:
    service: str
    command: str
    symbols: tuple[str, ...]


class SchwabStreamerClient:
    """Threaded Schwab Streamer WebSocket session (LOGIN + LEVELONE subscriptions)."""

    def __init__(
        self,
        token_manager: SchwabTokenManager,
        streamer_info: StreamerInfo,
        *,
        on_data: Callable[[str, Mapping[str, Any]], None],
        on_status: Callable[[str], None],
        on_subscription_state: Callable[[tuple[SubscriptionDiagnostic, ...]], None] | None = None,
    ) -> None:
        if websocket is None:
            raise ModuleNotFoundError(
                "No module named 'websocket'. Install the `websocket-client` package "
                "(`pip install -e .` or `pip install websocket-client`).",
                name="websocket",
            ) from _WEBSOCKET_IMPORT_ERROR
        self._tokens = token_manager
        self._info = streamer_info
        self._on_data = on_data
        self._on_status = on_status
        self._on_subscription_state = on_subscription_state
        self._app: Any = None
        self._thread: threading.Thread | None = None
        self._request_counter = 0
        self._counter_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._logged_in = threading.Event()
        self._login_error: str | None = None
        self._shutdown_requested = False
        self._connection_stopping = False
        self._lifecycle_lock = threading.Lock()
        self._connection_generation = 0
        self._reconnect_scheduled = False
        self._last_lost_app: Any = None

        # Desired intent is intentionally separate from what is pending or
        # confirmed by the server. A failed send must never masquerade as an
        # active subscription or suppress a later retry.
        self._desired_subscriptions: dict[str, set[str]] = {
            SERVICE_LEVELONE_OPTIONS: set(),
            SERVICE_LEVELONE_EQUITIES: set(),
        }
        # Backward-compatible private alias used by older diagnostics/tests.
        self._subscriptions = self._desired_subscriptions
        self._confirmed_subscriptions: dict[str, set[str]] = {
            SERVICE_LEVELONE_OPTIONS: set(),
            SERVICE_LEVELONE_EQUITIES: set(),
        }
        self._pending_additions: dict[str, set[str]] = {
            SERVICE_LEVELONE_OPTIONS: set(),
            SERVICE_LEVELONE_EQUITIES: set(),
        }
        self._pending_removals: dict[str, set[str]] = {
            SERVICE_LEVELONE_OPTIONS: set(),
            SERVICE_LEVELONE_EQUITIES: set(),
        }
        self._subscription_failures: dict[str, dict[str, str]] = {
            SERVICE_LEVELONE_OPTIONS: {},
            SERVICE_LEVELONE_EQUITIES: {},
        }
        self._pending_subscription_requests: dict[str, _PendingSubscriptionRequest] = {}
        self._fields_by_service: dict[str, str] = {
            SERVICE_LEVELONE_OPTIONS: LEVELONE_OPTIONS_FIELDS,
            SERVICE_LEVELONE_EQUITIES: LEVELONE_EQUITIES_FIELDS,
        }
        self._subscriptions_lock = threading.Lock()
        self._reconnect_attempts = 0
        self._login_established_monotonic: float | None = None

    @property
    def logged_in(self) -> bool:
        return self._logged_in.is_set()

    def start(self, timeout: float = STREAMER_LOGIN_TIMEOUT_SECONDS) -> None:
        with self._lifecycle_lock:
            self._shutdown_requested = False
            self._connection_generation += 1
            self._reconnect_scheduled = False
            self._reconnect_attempts = 0
            self._login_established_monotonic = None
        self._start_connection(timeout)

    def _start_connection(self, timeout: float = STREAMER_LOGIN_TIMEOUT_SECONDS) -> None:
        with self._lifecycle_lock:
            if self._shutdown_requested:
                raise SchwabRequestError("Schwab streamer shutdown was requested.")
            # Socket assignment and thread start are atomic with respect to a
            # deliberate stop, preventing a reconnect attempt from reviving a
            # connection after shutdown wins the race.
            self._login_error = None
            self._logged_in.clear()
            self._login_established_monotonic = None
            app = websocket.WebSocketApp(
                self._info.socket_url,
                on_open=self._handle_open,
                on_message=self._handle_message,
                on_error=self._handle_error,
                on_close=self._handle_close,
            )
            thread = threading.Thread(
                target=self._run_forever,
                args=(app,),
                name="SchwabStreamer",
                daemon=True,
            )
            self._app = app
            self._thread = thread
            self._last_lost_app = None
            thread.start()
        if not self._logged_in.wait(timeout):
            self._close_connection(send_logout=False)
            raise SchwabRequestError(
                self._login_error or "Timed out waiting for the Schwab streamer LOGIN response."
            )
        if self._login_error:
            error = self._login_error
            self._close_connection(send_logout=False)
            raise SchwabRequestError(error)

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._shutdown_requested = True
            self._connection_generation += 1
            self._reconnect_scheduled = False
            self._login_established_monotonic = None
        self._close_connection(send_logout=True)

    def _close_connection(self, *, send_logout: bool) -> None:
        with self._lifecycle_lock:
            self._connection_stopping = True
        app = self._app
        if app is not None:
            if send_logout:
                try:
                    self._send(
                        [self._build_request(SERVICE_ADMIN, "LOGOUT", {})],
                    )
                except Exception:
                    pass
            try:
                app.close()
            except Exception:
                pass
        thread = self._thread
        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=3.0)
        if self._thread is thread:
            self._thread = None
        if self._app is app:
            self._app = None
        self._logged_in.clear()
        self._mark_subscriptions_disconnected()
        with self._lifecycle_lock:
            self._connection_stopping = False

    def subscribe(self, service: str, symbols: Sequence[str], fields: str) -> None:
        normalized = tuple(dict.fromkeys(str(symbol) for symbol in symbols if str(symbol)))
        if not normalized:
            return
        with self._subscriptions_lock:
            desired = self._desired_subscriptions.setdefault(service, set())
            confirmed = self._confirmed_subscriptions.setdefault(service, set())
            pending_additions = self._pending_additions.setdefault(service, set())
            pending_removals = self._pending_removals.setdefault(service, set())
            failures = self._subscription_failures.setdefault(service, {})
            desired.update(normalized)
            self._fields_by_service[service] = fields
            for symbol in normalized:
                if symbol in pending_removals:
                    failures.pop(symbol, None)
            added = sorted(
                symbol
                for symbol in normalized
                if symbol not in confirmed
                and symbol not in pending_additions
                and symbol not in pending_removals
            )
        self._emit_subscription_diagnostics()
        self._send_subscription_chunks(service, "ADD", added, fields=fields)

    def set_subscriptions(self, service: str, symbols: Sequence[str], fields: str) -> None:
        """Reconcile one service to an exact desired symbol set.

        Unlike a caller-computed delta, this also clears symbols left behind by
        an earlier failed operation. Repeating the same target retries failed
        work while confirmed and in-flight symbols remain de-duplicated.
        """
        normalized = tuple(dict.fromkeys(str(symbol) for symbol in symbols if str(symbol)))
        target = set(normalized)
        with self._subscriptions_lock:
            tracked = (
                self._desired_subscriptions.setdefault(service, set())
                | self._confirmed_subscriptions.setdefault(service, set())
                | self._pending_additions.setdefault(service, set())
                | self._pending_removals.setdefault(service, set())
                | set(self._subscription_failures.setdefault(service, {}))
            )
            removed = sorted(tracked - target)
        if removed:
            self.unsubscribe(service, removed)
        if normalized:
            self.subscribe(service, normalized, fields)

    def unsubscribe(self, service: str, symbols: Sequence[str]) -> None:
        normalized = tuple(dict.fromkeys(str(symbol) for symbol in symbols if str(symbol)))
        if not normalized:
            return
        with self._subscriptions_lock:
            desired = self._desired_subscriptions.setdefault(service, set())
            confirmed = self._confirmed_subscriptions.setdefault(service, set())
            pending_additions = self._pending_additions.setdefault(service, set())
            pending_removals = self._pending_removals.setdefault(service, set())
            failures = self._subscription_failures.setdefault(service, {})
            desired.difference_update(normalized)
            for symbol in normalized:
                if symbol not in confirmed and symbol not in pending_additions:
                    failures.pop(symbol, None)
            removed = sorted(
                symbol
                for symbol in normalized
                if (symbol in confirmed or symbol in pending_additions)
                and symbol not in pending_removals
            )
        self._emit_subscription_diagnostics()
        self._send_subscription_chunks(service, "UNSUBS", removed)

    def clear_subscriptions(self) -> None:
        with self._subscriptions_lock:
            snapshot = {
                service: sorted(
                    symbols
                    | self._confirmed_subscriptions.setdefault(service, set())
                    | self._pending_additions.setdefault(service, set())
                )
                for service, symbols in self._desired_subscriptions.items()
            }
            for symbols in self._desired_subscriptions.values():
                symbols.clear()
        self._emit_subscription_diagnostics()
        for service, symbols in snapshot.items():
            if not symbols:
                continue
            try:
                self._send_subscription_chunks(service, "UNSUBS", symbols)
            except SchwabRequestError as exc:
                self._on_status(f"Schwab streamer unsubscribe failed: {exc}")

    def subscription_diagnostics(self) -> tuple[SubscriptionDiagnostic, ...]:
        """Immutable, per-symbol desired/pending/confirmed/failed state."""
        with self._subscriptions_lock:
            services = set(self._desired_subscriptions)
            services.update(self._confirmed_subscriptions)
            services.update(self._pending_additions)
            services.update(self._pending_removals)
            services.update(self._subscription_failures)
            diagnostics: list[SubscriptionDiagnostic] = []
            for service in sorted(services):
                desired = self._desired_subscriptions.setdefault(service, set())
                confirmed = self._confirmed_subscriptions.setdefault(service, set())
                pending_additions = self._pending_additions.setdefault(service, set())
                pending_removals = self._pending_removals.setdefault(service, set())
                failures = self._subscription_failures.setdefault(service, {})
                symbols = desired | confirmed | pending_additions | pending_removals | set(failures)
                for symbol in sorted(symbols):
                    error = failures.get(symbol)
                    if symbol in pending_removals:
                        state = "pending_unsubscribe"
                        pending_command = "UNSUBS"
                    elif symbol in pending_additions:
                        state = "pending_subscribe"
                        pending_command = "ADD"
                    elif error:
                        state = "failed"
                        pending_command = None
                    elif symbol in confirmed:
                        state = "subscribed"
                        pending_command = None
                    else:
                        state = "desired"
                        pending_command = None
                    diagnostics.append(
                        SubscriptionDiagnostic(
                            service=service,
                            symbol=symbol,
                            state=state,
                            desired=symbol in desired,
                            error=error,
                            pending_command=pending_command,
                            confirmed=symbol in confirmed,
                        )
                    )
        return tuple(diagnostics)

    def _run_forever(self, app: Any) -> None:
        loop_error: Exception | None = None
        try:
            app.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as exc:
            loop_error = exc
            self._on_status(f"Schwab streamer loop error: {exc}")
        finally:
            with self._lifecycle_lock:
                ignore_close = (
                    self._shutdown_requested
                    or self._connection_stopping
                    or app is self._last_lost_app
                )
            if ignore_close or app is not self._app:
                return
            if self._logged_in.is_set() and self._login_error is None:
                reason = str(loop_error) if loop_error is not None else "stream loop ended"
                self._handle_connection_loss(app, None, reason)
            else:
                self._login_error = self._login_error or (
                    f"Schwab streamer connection ended before LOGIN completed: "
                    f"{loop_error or 'stream loop ended'}"
                )
                self._logged_in.set()

    def _next_request_id(self) -> str:
        with self._counter_lock:
            self._request_counter += 1
            return str(self._request_counter)

    def _build_request(self, service: str, command: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "service": service,
            "command": command,
            "requestid": self._next_request_id(),
            "SchwabClientCustomerId": self._info.customer_id,
            "SchwabClientCorrelId": self._info.correl_id,
            "parameters": dict(parameters),
        }

    def _send(self, requests: Sequence[Mapping[str, Any]]) -> None:
        app = self._app
        if app is None or app.sock is None or not getattr(app.sock, "connected", False):
            raise SchwabRequestError("Schwab streamer is not connected.")
        payload = json.dumps({"requests": list(requests)})
        try:
            with self._send_lock:
                app.send(payload)
        except Exception as exc:
            raise SchwabRequestError(f"Schwab streamer send failed: {exc}") from exc

    def _send_subscription_chunks(
        self,
        service: str,
        command: str,
        symbols: Sequence[str],
        *,
        fields: str | None = None,
    ) -> None:
        for offset in range(0, len(symbols), STREAMER_MAX_SYMBOLS_PER_REQUEST):
            chunk = tuple(symbols[offset : offset + STREAMER_MAX_SYMBOLS_PER_REQUEST])
            self._send_subscription_request(service, command, chunk, fields=fields)

    def _send_subscription_request(
        self,
        service: str,
        command: str,
        symbols: tuple[str, ...],
        *,
        fields: str | None = None,
    ) -> None:
        if not symbols:
            return
        parameters: dict[str, Any] = {"keys": ",".join(symbols)}
        if command in {"ADD", "SUBS"}:
            parameters["fields"] = fields or self._fields_by_service.get(service, "0")
        request = self._build_request(service, command, parameters)
        request_id = str(request["requestid"])
        pending_request = _PendingSubscriptionRequest(service, command, symbols)
        with self._subscriptions_lock:
            self._pending_subscription_requests[request_id] = pending_request
            pending = (
                self._pending_removals.setdefault(service, set())
                if command == "UNSUBS"
                else self._pending_additions.setdefault(service, set())
            )
            pending.update(symbols)
            failures = self._subscription_failures.setdefault(service, {})
            for symbol in symbols:
                failures.pop(symbol, None)
        self._emit_subscription_diagnostics()
        try:
            self._send([request])
        except SchwabRequestError as exc:
            self._fail_pending_subscription_request(request_id, str(exc))
            raise

    def _fail_pending_subscription_request(self, request_id: str, error: str) -> None:
        with self._subscriptions_lock:
            pending_request = self._pending_subscription_requests.pop(request_id, None)
            if pending_request is None:
                return
            service = pending_request.service
            pending = (
                self._pending_removals.setdefault(service, set())
                if pending_request.command == "UNSUBS"
                else self._pending_additions.setdefault(service, set())
            )
            pending.difference_update(pending_request.symbols)
            desired = self._desired_subscriptions.setdefault(service, set())
            failures = self._subscription_failures.setdefault(service, {})
            for symbol in pending_request.symbols:
                failed_current_intent = (
                    symbol not in desired
                    if pending_request.command == "UNSUBS"
                    else symbol in desired
                )
                if failed_current_intent:
                    failures[symbol] = error
        self._emit_subscription_diagnostics()

    def _complete_subscription_request(
        self,
        request_id: str,
        *,
        success: bool,
        error: str,
    ) -> bool:
        with self._subscriptions_lock:
            pending_request = self._pending_subscription_requests.pop(request_id, None)
            if pending_request is None:
                return False
            service = pending_request.service
            is_removal = pending_request.command == "UNSUBS"
            pending = (
                self._pending_removals.setdefault(service, set())
                if is_removal
                else self._pending_additions.setdefault(service, set())
            )
            pending.difference_update(pending_request.symbols)
            desired = self._desired_subscriptions.setdefault(service, set())
            confirmed = self._confirmed_subscriptions.setdefault(service, set())
            failures = self._subscription_failures.setdefault(service, {})
            for symbol in pending_request.symbols:
                if success:
                    if is_removal:
                        confirmed.discard(symbol)
                    else:
                        confirmed.add(symbol)
                    failures.pop(symbol, None)
                    continue
                failed_current_intent = symbol not in desired if is_removal else symbol in desired
                if failed_current_intent:
                    failures[symbol] = error
        self._emit_subscription_diagnostics()
        if success:
            self._reconcile_subscriptions(service, pending_request.symbols)
        return True

    def _reconcile_subscriptions(self, service: str, symbols: Sequence[str]) -> None:
        with self._subscriptions_lock:
            desired = self._desired_subscriptions.setdefault(service, set())
            confirmed = self._confirmed_subscriptions.setdefault(service, set())
            pending_additions = self._pending_additions.setdefault(service, set())
            pending_removals = self._pending_removals.setdefault(service, set())
            additions = sorted(
                symbol
                for symbol in symbols
                if symbol in desired
                and symbol not in confirmed
                and symbol not in pending_additions
                and symbol not in pending_removals
            )
            removals = sorted(
                symbol
                for symbol in symbols
                if symbol not in desired
                and symbol in confirmed
                and symbol not in pending_removals
            )
            fields = self._fields_by_service.get(service, "0")
        try:
            self._send_subscription_chunks(service, "ADD", additions, fields=fields)
            self._send_subscription_chunks(service, "UNSUBS", removals)
        except SchwabRequestError as exc:
            self._on_status(f"Schwab streamer subscription reconciliation failed: {exc}")

    def _mark_subscriptions_disconnected(self) -> None:
        with self._subscriptions_lock:
            for symbols in self._confirmed_subscriptions.values():
                symbols.clear()
            for symbols in self._pending_additions.values():
                symbols.clear()
            for symbols in self._pending_removals.values():
                symbols.clear()
            for failures in self._subscription_failures.values():
                failures.clear()
            self._pending_subscription_requests.clear()
        self._emit_subscription_diagnostics()

    def _emit_subscription_diagnostics(self) -> None:
        callback = self._on_subscription_state
        if callback is not None:
            callback(self.subscription_diagnostics())

    def _handle_open(self, _app: Any) -> None:
        try:
            login = self._build_request(
                SERVICE_ADMIN,
                "LOGIN",
                {
                    "Authorization": self._tokens.access_token(),
                    "SchwabClientChannel": self._info.channel,
                    "SchwabClientFunctionId": self._info.function_id,
                },
            )
            self._send([login])
        except Exception as exc:
            self._login_error = f"Schwab streamer LOGIN send failed: {exc}"
            self._logged_in.set()

    def _handle_message(self, _app: Any, message: str | bytes) -> None:
        with self._lifecycle_lock:
            if _app is not None and (
                _app is not self._app
                or _app is self._last_lost_app
                or self._shutdown_requested
                or self._connection_stopping
            ):
                return
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, Mapping):
            return

        for response in payload.get("response") or []:
            if isinstance(response, Mapping):
                self._handle_response(response)
        for data in payload.get("data") or []:
            if not isinstance(data, Mapping):
                continue
            service = str(data.get("service") or "")
            for item in data.get("content") or []:
                if isinstance(item, Mapping):
                    self._on_data(service, item)
        # Heartbeat notifications keep the socket alive; nothing to do.

    def _handle_response(self, response: Mapping[str, Any]) -> None:
        service = str(response.get("service") or "")
        command = str(response.get("command") or "")
        request_id = str(response.get("requestid") or "")
        content = response.get("content")
        code: int | None = None
        has_code = False
        message = ""
        if isinstance(content, Mapping):
            has_code = "code" in content
            raw_code = content.get("code")
            if isinstance(raw_code, (int, float)):
                code = int(raw_code)
            elif isinstance(raw_code, str):
                try:
                    code = int(raw_code.strip())
                except ValueError:
                    code = None
            message = str(content.get("msg") or "")

        if service == SERVICE_ADMIN and command == "LOGIN":
            if code == 0:
                self._login_error = None
                with self._lifecycle_lock:
                    self._login_established_monotonic = _time.monotonic()
                self._resubscribe_all()
                self._logged_in.set()
            else:
                with self._lifecycle_lock:
                    self._login_established_monotonic = None
                self._login_error = f"Schwab streamer login denied: {message or code}"
                self._logged_in.set()
            return

        success = (not has_code) or code in (0, 26, 27, 28, 29)
        error = f"Schwab streamer {service} {command} failed ({code}): {message}"
        if request_id:
            self._complete_subscription_request(
                request_id,
                success=success,
                error=error,
            )
        if not success:
            self._on_status(f"Schwab streamer {service} {command} failed ({code}): {message}")

    def _resubscribe_all(self) -> None:
        with self._subscriptions_lock:
            snapshot = {
                service: sorted(symbols)
                for service, symbols in self._desired_subscriptions.items()
            }
        for service, symbols in snapshot.items():
            if not symbols:
                continue
            fields = self._fields_by_service.get(service, "0")
            for offset in range(0, len(symbols), STREAMER_MAX_SYMBOLS_PER_REQUEST):
                chunk = tuple(symbols[offset : offset + STREAMER_MAX_SYMBOLS_PER_REQUEST])
                command = "SUBS" if offset == 0 else "ADD"
                try:
                    self._send_subscription_request(service, command, chunk, fields=fields)
                except SchwabRequestError as exc:
                    self._on_status(f"Schwab streamer resubscribe failed: {exc}")

    def _handle_error(self, _app: Any, error: Any) -> None:
        with self._lifecycle_lock:
            report = not self._shutdown_requested and not self._connection_stopping
        if report:
            self._on_status(f"Schwab streamer error: {error}")

    def _handle_close(self, _app: Any, status_code: Any, reason: Any) -> None:
        with self._lifecycle_lock:
            ignore_close = self._shutdown_requested or self._connection_stopping
        if ignore_close or _app is not self._app:
            return
        if not self._logged_in.is_set() or self._login_error is not None:
            self._login_error = self._login_error or (
                f"Schwab streamer connection closed before LOGIN completed "
                f"({status_code}): {reason or 'no reason given'}."
            )
            self._logged_in.set()
            return
        self._handle_connection_loss(_app, status_code, reason)

    def _handle_connection_loss(self, app: Any, status_code: Any, reason: Any) -> None:
        with self._lifecycle_lock:
            if (
                self._shutdown_requested
                or self._connection_stopping
                or app is not self._app
                or app is self._last_lost_app
            ):
                return
            self._last_lost_app = app
            established_at = self._login_established_monotonic
            if (
                established_at is not None
                and _time.monotonic() - established_at
                >= STREAMER_RECONNECT_STABILITY_SECONDS
            ):
                self._reconnect_attempts = 0
            self._login_established_monotonic = None
        self._logged_in.clear()
        self._mark_subscriptions_disconnected()
        self._on_status(
            f"Schwab streamer connection closed ({status_code}): {reason or 'no reason given'}."
        )
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        with self._lifecycle_lock:
            if self._shutdown_requested or self._reconnect_scheduled:
                return
            if self._reconnect_attempts >= STREAMER_MAX_RECONNECT_ATTEMPTS:
                exhausted = True
                attempt = self._reconnect_attempts
                delay = 0.0
                generation = self._connection_generation
            else:
                exhausted = False
                self._reconnect_attempts += 1
                attempt = self._reconnect_attempts
                delay = min(
                    STREAMER_MAX_RECONNECT_DELAY_SECONDS,
                    2.0**attempt,
                )
                generation = self._connection_generation
                self._reconnect_scheduled = True
        if exhausted:
            self._on_status(
                "Schwab streamer reconnect attempts exhausted. Use DISCONNECT/CONNECT to retry."
            )
            return
        self._on_status(
            f"Schwab streamer reconnecting in {delay:.0f}s "
            f"(attempt {attempt}/{STREAMER_MAX_RECONNECT_ATTEMPTS}) ..."
        )

        def reconnect() -> None:
            _time.sleep(delay)
            with self._lifecycle_lock:
                if self._shutdown_requested or generation != self._connection_generation:
                    self._reconnect_scheduled = False
                    return
                self._reconnect_scheduled = False
            try:
                self._tokens.access_token(force_refresh=True)
                self._start_connection()
                self._on_status("Schwab streamer reconnected.")
            except Exception as exc:
                self._on_status(f"Schwab streamer reconnect failed: {exc}")
                self._schedule_reconnect()

        threading.Thread(target=reconnect, name="SchwabStreamerReconnect", daemon=True).start()


@dataclass
class _LegQuoteState:
    con_id: int
    symbol: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    mark: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    implied_volatility: float | None = None
    model_option_price: float | None = None
    market_data_type: int = LIVE_MARKET_DATA_TYPE
    quote_time: datetime | None = None
    updated_at: datetime | None = None
    bid_update_received_at: datetime | None = None
    ask_update_received_at: datetime | None = None
    bid_confirmed_at: datetime | None = None
    ask_confirmed_at: datetime | None = None

    def snapshot(self) -> QuoteSnapshot:
        return QuoteSnapshot(
            label=str(self.con_id),
            kind=QUOTE_KIND_LEG,
            contract_con_id=self.con_id,
            bid=self.bid,
            ask=self.ask,
            last=self.last,
            mark=self.mark,
            bid_size=self.bid_size,
            ask_size=self.ask_size,
            implied_volatility=self.implied_volatility,
            model_option_price=self.model_option_price,
            volume=self.volume,
            open_interest=self.open_interest,
            market_data_type=self.market_data_type,
            quote_time=self.quote_time,
            updated_at=self.updated_at,
            bid_update_received_at=self.bid_update_received_at,
            ask_update_received_at=self.ask_update_received_at,
            bid_confirmed_at=self.bid_confirmed_at,
            ask_confirmed_at=self.ask_confirmed_at,
        )


@dataclass
class _UnderlyingQuoteState:
    symbol: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    mark: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    volume: float | None = None
    market_data_type: int = LIVE_MARKET_DATA_TYPE
    quote_time: datetime | None = None
    updated_at: datetime | None = None

    def snapshot(self, con_id: int) -> QuoteSnapshot:
        return QuoteSnapshot(
            label=self.symbol,
            kind=QUOTE_KIND_UNDERLYING,
            contract_con_id=con_id,
            bid=self.bid,
            ask=self.ask,
            last=self.last,
            mark=self.mark,
            bid_size=self.bid_size,
            ask_size=self.ask_size,
            volume=self.volume,
            market_data_type=self.market_data_type,
            quote_time=self.quote_time,
            updated_at=self.updated_at,
        )


class SchwabMarketDataService:
    """Market-data service backed by the Schwab Trader API.

    Streams leg and underlying quotes; vertical spread quotes are synthesized
    from the leg quotes in the models layer.
    """

    def __init__(
        self,
        *,
        credentials: SchwabCredentials | None = None,
        token_manager: SchwabTokenManager | None = None,
        rest_client: SchwabRestClient | None = None,
    ) -> None:
        self.events = CoalescingEventQueue()
        self._tokens = token_manager or SchwabTokenManager(credentials)
        self._rest = rest_client or SchwabRestClient(self._tokens)
        self._streamer: SchwabStreamerClient | None = None
        self._streamer_callback_generation = 0
        self._operation_lock = threading.RLock()
        self._quotes_lock = threading.Lock()

        # Stable per-session integer ids for Schwab symbols (Schwab has no conId).
        self._symbol_ids: dict[str, int] = {}
        self._next_symbol_id = 1

        self._leg_states: dict[int, _LegQuoteState] = {}
        self._legs_by_symbol: dict[str, int] = {}
        self._underlying_state: _UnderlyingQuoteState | None = None
        self._underlying_con_id: int | None = None
        self._quote_generation = 0

        self._expiration_records_cache: OrderedDict[
            tuple[int, str], list[OptionContractRecord]
        ] = OrderedDict()
        self._chain_underlying_prices: dict[tuple[int, str], float] = {}
        self._subscription_diagnostics: tuple[SubscriptionDiagnostic, ...] = ()
        self._subscription_diagnostics_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        streamer = self._streamer
        return streamer is not None and streamer.logged_in

    @property
    def connection_active(self) -> bool:
        """Whether a streamer lifecycle exists, including auto-reconnect."""
        return self._streamer is not None

    def subscription_diagnostics(self) -> tuple[SubscriptionDiagnostic, ...]:
        """Latest immutable streamer state, including the equity underlying."""
        with self._subscription_diagnostics_lock:
            return self._subscription_diagnostics

    def subscription_diagnostics_by_con_id(self) -> Mapping[int, SubscriptionDiagnostic]:
        """Immutable lookup suitable for matrix-cell subscription outlines."""
        return MappingProxyType(
            {
                diagnostic.contract_con_id: diagnostic
                for diagnostic in self.subscription_diagnostics()
                if diagnostic.contract_con_id is not None
            }
        )

    def subscription_state_for_con_id(self, con_id: int) -> SubscriptionDiagnostic | None:
        return self.subscription_diagnostics_by_con_id().get(con_id)

    @property
    def quote_generation(self) -> int:
        with self._quotes_lock:
            return self._quote_generation

    def advance_quote_generation(self) -> int:
        """Advance the app-visible plan generation and re-emit diagnostics.

        Quote events already waiting in ``events`` retain their older
        generation, allowing app.py to reject them after a ticker/workspace
        transition without altering the QuoteSnapshot public model.
        """
        with self._quotes_lock:
            self._quote_generation += 1
            generation = self._quote_generation
        streamer = self._streamer
        diagnostics_method = getattr(streamer, "subscription_diagnostics", None)
        diagnostics = tuple(diagnostics_method()) if callable(diagnostics_method) else ()
        self._handle_subscription_diagnostics(diagnostics)
        return generation

    def connect(self, *_legacy_args: Any) -> None:
        """Authenticate with Schwab and open the streamer WebSocket."""
        with self._operation_lock:
            if self.connection_active:
                self.disconnect()
            self._expiration_records_cache.clear()
            self._chain_underlying_prices.clear()
            # Fail fast with a clear message when credentials are placeholders.
            self._tokens.access_token()
            preferences = self._rest.get_user_preferences()
            streamer_info = StreamerInfo.from_user_preferences(preferences)
            self._streamer_callback_generation += 1
            callback_generation = self._streamer_callback_generation

            def callback_is_current() -> bool:
                return callback_generation == self._streamer_callback_generation

            def on_data(service: str, item: Mapping[str, Any]) -> None:
                if callback_is_current():
                    self._handle_stream_data(service, item)

            def on_status(message: str) -> None:
                if callback_is_current():
                    self._emit_status(message)

            def on_subscription_state(
                diagnostics: tuple[SubscriptionDiagnostic, ...],
            ) -> None:
                if callback_is_current():
                    self._handle_subscription_diagnostics(diagnostics)

            streamer = SchwabStreamerClient(
                self._tokens,
                streamer_info,
                on_data=on_data,
                on_status=on_status,
                on_subscription_state=on_subscription_state,
            )
            self._streamer = streamer
            try:
                streamer.start()
            except Exception:
                if callback_generation == self._streamer_callback_generation:
                    self._streamer_callback_generation += 1
                if self._streamer is streamer:
                    self._streamer = None
                try:
                    streamer.stop()
                except Exception:
                    pass
                self._handle_subscription_diagnostics(())
                raise
            self._emit_status("Connected to the Schwab streamer.")

    def disconnect(self) -> None:
        with self._operation_lock:
            self._streamer_callback_generation += 1
            self._cancel_active_quotes()
            self._expiration_records_cache.clear()
            self._chain_underlying_prices.clear()
            streamer = self._streamer
            self._streamer = None
            if streamer is not None:
                streamer.stop()
            self._handle_subscription_diagnostics(())
            self._emit_status("Disconnected.")

    def clear_active_quotes(self) -> None:
        with self._operation_lock:
            self._cancel_active_quotes()

    def request_current_time(self, timeout: float = 10.0) -> datetime:
        del timeout  # REST client applies its own timeout.
        return self._rest.server_time()

    # ------------------------------------------------------------------
    # Discovery (REST /expirationchain + /chains)
    # ------------------------------------------------------------------
    def search_ticker(self, symbol: str) -> SearchResult:
        with self._operation_lock:
            self._ensure_connected()
            normalized_symbol = symbol.strip().upper()
            if not normalized_symbol:
                raise ValueError("Enter a ticker symbol before searching.")

            payload = self._rest.get_expiration_chain(normalized_symbol)
            expirations = tuple(
                sorted(
                    expiration_to_compact(str(entry.get("expirationDate") or entry.get("expiration") or ""))
                    for entry in payload.get("expirationList") or []
                    if isinstance(entry, Mapping)
                    and (entry.get("expirationDate") or entry.get("expiration"))
                )
            )
            if not expirations:
                raise RuntimeError(f"No option expirations returned for {normalized_symbol}.")

            exchange = ""
            primary_exchange = ""
            try:
                quotes = self._rest.get_quotes([normalized_symbol], fields="reference")
                entry = quotes.get(normalized_symbol)
                if isinstance(entry, Mapping):
                    reference = entry.get("reference")
                    if isinstance(reference, Mapping):
                        exchange = str(reference.get("exchangeName") or reference.get("exchange") or "")
                        primary_exchange = exchange
            except SchwabRequestError:
                pass  # Exchange metadata is cosmetic; discovery already succeeded.

            underlying = UnderlyingContract(
                symbol=normalized_symbol,
                con_id=self._symbol_id(f"EQ:{normalized_symbol}"),
                exchange=exchange,
                primary_exchange=primary_exchange,
                currency="USD",
            )
            chain = OptionChainDefinition(
                symbol=normalized_symbol,
                underlying_con_id=underlying.con_id,
                exchange=exchange,
                trading_class=normalized_symbol,
                multiplier="100",
                expirations=expirations,
                strikes=(),
            )
            return SearchResult(underlying=underlying, chain=chain)

    def load_option_records(self, search_result: SearchResult, expiration: str) -> list[OptionContractRecord]:
        with self._operation_lock:
            self._ensure_connected()
            expiration_key = (search_result.underlying.con_id, expiration)
            records = self._expiration_records_cache.get(expiration_key)
            if records is None:
                records = self._load_expiration_option_records(search_result, expiration)
                self._expiration_records_cache[expiration_key] = records
                while len(self._expiration_records_cache) > EXPIRATION_CACHE_MAX_ENTRIES:
                    obsolete_key, _obsolete_records = self._expiration_records_cache.popitem(last=False)
                    self._chain_underlying_prices.pop(obsolete_key, None)
            else:
                self._expiration_records_cache.move_to_end(expiration_key)
            return list(records)

    def chain_underlying_price(self, search_result: SearchResult, expiration: str) -> float | None:
        """Underlying price captured from the last /chains pull for this expiration.

        REST-time hint so matrices can build before the first live
        underlying tick arrives; the app prefers a live streamer quote when one
        exists. Plain dict read on purpose: the UI thread calls this and must
        not block behind a worker holding _operation_lock across a REST call.
        """
        return self._chain_underlying_prices.get((search_result.underlying.con_id, expiration))

    def _load_expiration_option_records(
        self,
        search_result: SearchResult,
        expiration: str,
    ) -> list[OptionContractRecord]:
        iso_date = expiration_to_iso(expiration)
        payload = self._rest.get_chain(
            search_result.underlying.symbol,
            from_date=iso_date,
            to_date=iso_date,
            contract_type="ALL",
        )
        records = parse_chain_records(
            payload,
            expiration=expiration_to_compact(expiration),
            symbol_id_for=self._symbol_id,
        )
        if not records:
            raise RuntimeError(
                f"Schwab returned no option contracts for {search_result.underlying.symbol} "
                f"expiring {iso_date}."
            )
        price = _chain_underlying_price(payload)
        if price is not None:
            self._chain_underlying_prices[(search_result.underlying.con_id, expiration)] = price
        return records

    # ------------------------------------------------------------------
    # Quote subscriptions (Streamer LEVELONE_OPTIONS / LEVELONE_EQUITIES)
    # ------------------------------------------------------------------
    def subscribe_matrix_quotes(
        self,
        search_result: SearchResult,
        selections: Sequence[VerticalSelection],
    ) -> None:
        with self._operation_lock:
            self._ensure_connected()
            streamer = self._require_streamer()

            desired_leg_records: dict[int, OptionContractRecord] = {}
            for selection in selections:
                for leg in selection.legs:
                    desired_leg_records.setdefault(leg.contract.con_id, leg.contract)

            desired_leg_ids = set(desired_leg_records.keys())
            want_underlying = bool(desired_leg_ids)

            with self._quotes_lock:
                previous_leg_states = dict(self._leg_states)
                previous_legs_by_symbol = dict(self._legs_by_symbol)
                removed_leg_symbols = [
                    state.symbol
                    for con_id, state in self._leg_states.items()
                    if con_id not in desired_leg_ids
                ]

                added_leg_symbols: list[str] = []
                for con_id, record in desired_leg_records.items():
                    if con_id in self._leg_states:
                        continue
                    option_symbol = record.local_symbol or build_option_symbol(
                        record.symbol, record.expiration, record.right, record.strike
                    )
                    self._leg_states[con_id] = _LegQuoteState(con_id=con_id, symbol=option_symbol)
                    self._legs_by_symbol[option_symbol] = con_id
                    added_leg_symbols.append(option_symbol)

                for con_id in list(self._leg_states.keys()):
                    if con_id not in desired_leg_ids:
                        state = self._leg_states.pop(con_id)
                        self._legs_by_symbol.pop(state.symbol, None)

            try:
                set_subscriptions = getattr(streamer, "set_subscriptions", None)
                if callable(set_subscriptions):
                    desired_leg_symbols = sorted(
                        state.symbol for state in self._leg_states.values()
                    )
                    set_subscriptions(
                        SERVICE_LEVELONE_OPTIONS,
                        desired_leg_symbols,
                        LEVELONE_OPTIONS_FIELDS,
                    )
                else:
                    if removed_leg_symbols:
                        streamer.unsubscribe(SERVICE_LEVELONE_OPTIONS, removed_leg_symbols)
                    if added_leg_symbols:
                        streamer.subscribe(
                            SERVICE_LEVELONE_OPTIONS,
                            added_leg_symbols,
                            LEVELONE_OPTIONS_FIELDS,
                        )

                if want_underlying:
                    self._subscribe_underlying_locked(search_result)
                else:
                    self._unsubscribe_underlying_locked()
            except Exception:
                # Keep local routing state transactional with synchronous send
                # success. A second identical call must calculate the same
                # delta and actually retry it.
                with self._quotes_lock:
                    self._leg_states = previous_leg_states
                    self._legs_by_symbol = previous_legs_by_symbol
                raise

    def subscribe_underlying_quote(self, search_result: SearchResult) -> None:
        with self._operation_lock:
            self._ensure_connected()
            self._subscribe_underlying_locked(search_result)

    def _subscribe_underlying_locked(self, search_result: SearchResult) -> None:
        streamer = self._require_streamer()
        symbol = search_result.underlying.symbol
        con_id = search_result.underlying.con_id
        with self._quotes_lock:
            current = self._underlying_state
            if current is not None and current.symbol == symbol:
                return
            previous_state = current
            previous_con_id = self._underlying_con_id
            previous_symbol = current.symbol if current is not None else None
            self._underlying_state = _UnderlyingQuoteState(symbol=symbol)
            self._underlying_con_id = con_id
        try:
            set_subscriptions = getattr(streamer, "set_subscriptions", None)
            if callable(set_subscriptions):
                set_subscriptions(
                    SERVICE_LEVELONE_EQUITIES,
                    [symbol],
                    LEVELONE_EQUITIES_FIELDS,
                )
            else:
                if previous_symbol:
                    streamer.unsubscribe(SERVICE_LEVELONE_EQUITIES, [previous_symbol])
                streamer.subscribe(SERVICE_LEVELONE_EQUITIES, [symbol], LEVELONE_EQUITIES_FIELDS)
        except Exception:
            with self._quotes_lock:
                self._underlying_state = previous_state
                self._underlying_con_id = previous_con_id
            raise

    def _unsubscribe_underlying_locked(self) -> None:
        streamer = self._streamer
        with self._quotes_lock:
            current = self._underlying_state
            current_con_id = self._underlying_con_id
            self._underlying_state = None
            self._underlying_con_id = None
        if current is not None and streamer is not None:
            try:
                set_subscriptions = getattr(streamer, "set_subscriptions", None)
                if callable(set_subscriptions):
                    set_subscriptions(
                        SERVICE_LEVELONE_EQUITIES,
                        [],
                        LEVELONE_EQUITIES_FIELDS,
                    )
                else:
                    streamer.unsubscribe(SERVICE_LEVELONE_EQUITIES, [current.symbol])
            except Exception:
                with self._quotes_lock:
                    self._underlying_state = current
                    self._underlying_con_id = current_con_id
                raise

    def _cancel_active_quotes(self) -> None:
        streamer = self._streamer
        with self._quotes_lock:
            invalidated = [
                state.snapshot()
                for state in self._leg_states.values()
                if _clear_quote_confirmations(state)
            ]
            generation = self._quote_generation
            self._leg_states.clear()
            self._legs_by_symbol.clear()
            self._underlying_state = None
            self._underlying_con_id = None
        for snapshot in invalidated:
            self.events.put({"type": "quote", "quote": snapshot, "generation": generation})
        if streamer is not None:
            try:
                streamer.clear_subscriptions()
            except SchwabRequestError:
                pass

    # ------------------------------------------------------------------
    # Streamer message handling
    # ------------------------------------------------------------------
    def _handle_stream_data(self, service: str, item: Mapping[str, Any]) -> None:
        if service == SERVICE_LEVELONE_OPTIONS:
            self._handle_option_data(item)
        elif service == SERVICE_LEVELONE_EQUITIES:
            self._handle_equity_data(item)

    def _handle_option_data(self, item: Mapping[str, Any]) -> None:
        symbol = str(item.get("key") or "")
        received_at = datetime.now()
        with self._quotes_lock:
            con_id = self._legs_by_symbol.get(symbol)
            if con_id is None:
                return
            state = self._leg_states.get(con_id)
            if state is None:
                return

            bid_touched, ask_touched, fields_applied = _apply_stream_fields(state, item, _OPTION_FIELD_MAP)
            applied = fields_applied
            volatility = _stream_number(item.get("10"))
            if volatility is not None and volatility > 0:
                # Streamer volatility is a percentage (e.g. 32.89); store a decimal.
                normalized_volatility = round(volatility / 100.0, 6)
                if state.implied_volatility != normalized_volatility:
                    state.implied_volatility = normalized_volatility
                    applied = True
            theoretical_value = _stream_number(item.get("34"))
            if (
                theoretical_value is not None
                and theoretical_value > 0
                and state.model_option_price != theoretical_value
            ):
                state.model_option_price = theoretical_value
                applied = True
            quote_time = _stream_epoch_ms(item.get("38"))
            if quote_time is not None and state.quote_time != quote_time:
                state.quote_time = quote_time
                applied = True
            delayed = item.get("delayed")
            if isinstance(delayed, bool):
                market_data_type = (
                    DELAYED_MARKET_DATA_TYPE if delayed else LIVE_MARKET_DATA_TYPE
                )
                if state.market_data_type != market_data_type:
                    state.market_data_type = market_data_type
                    applied = True

            # Sentinel-only messages apply nothing; freshness stays honest.
            if applied:
                state.updated_at = received_at
            if bid_touched:
                state.bid_update_received_at = received_at
            if ask_touched:
                state.ask_update_received_at = received_at
            confirmations_changed = _confirm_stream_prices(state, item, received_at)
            if not applied and not confirmations_changed:
                return
            leg_snapshot = state.snapshot()
            generation = self._quote_generation

        self.events.put({"type": "quote", "quote": leg_snapshot, "generation": generation})

    def _handle_equity_data(self, item: Mapping[str, Any]) -> None:
        symbol = str(item.get("key") or "")
        received_at = datetime.now()
        with self._quotes_lock:
            state = self._underlying_state
            con_id = self._underlying_con_id
            if state is None or con_id is None or state.symbol != symbol:
                return
            _bid_touched, _ask_touched, applied = _apply_stream_fields(state, item, _EQUITY_FIELD_MAP)
            quote_time = _stream_epoch_ms(item.get("34"))
            if quote_time is not None and state.quote_time != quote_time:
                state.quote_time = quote_time
                applied = True
            delayed = item.get("delayed")
            if isinstance(delayed, bool):
                market_data_type = (
                    DELAYED_MARKET_DATA_TYPE if delayed else LIVE_MARKET_DATA_TYPE
                )
                if state.market_data_type != market_data_type:
                    state.market_data_type = market_data_type
                    applied = True
            if applied:
                state.updated_at = received_at
            else:
                return
            snapshot = state.snapshot(con_id)
            generation = self._quote_generation
        self.events.put({"type": "quote", "quote": snapshot, "generation": generation})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _symbol_id(self, symbol: str) -> int:
        existing = self._symbol_ids.get(symbol)
        if existing is not None:
            return existing
        assigned = self._next_symbol_id
        self._next_symbol_id += 1
        self._symbol_ids[symbol] = assigned
        return assigned

    def _ensure_connected(self) -> None:
        if not self.connected:
            raise RuntimeError("Connect to Schwab first.")

    def _require_streamer(self) -> SchwabStreamerClient:
        streamer = self._streamer
        if streamer is None:
            raise RuntimeError("Connect to Schwab first.")
        return streamer

    def _emit_status(self, message: str) -> None:
        self.events.put(
            {
                "type": "status",
                "message": message,
                "connected": self.connected,
                "connection_active": self.connection_active,
            }
        )

    def _handle_subscription_diagnostics(
        self,
        diagnostics: tuple[SubscriptionDiagnostic, ...],
    ) -> None:
        with self._quotes_lock:
            generation = self._quote_generation
            enriched: list[SubscriptionDiagnostic] = []
            for diagnostic in diagnostics:
                if diagnostic.service == SERVICE_LEVELONE_OPTIONS:
                    con_id = self._symbol_ids.get(diagnostic.symbol)
                    quote_kind = QUOTE_KIND_LEG
                elif diagnostic.service == SERVICE_LEVELONE_EQUITIES:
                    con_id = self._symbol_ids.get(f"EQ:{diagnostic.symbol}")
                    quote_kind = QUOTE_KIND_UNDERLYING
                else:
                    con_id = None
                    quote_kind = None
                enriched.append(
                    SubscriptionDiagnostic(
                        service=diagnostic.service,
                        symbol=diagnostic.symbol,
                        state=diagnostic.state,
                        desired=diagnostic.desired,
                        error=diagnostic.error,
                        pending_command=diagnostic.pending_command,
                        confirmed=diagnostic.confirmed,
                        contract_con_id=con_id,
                        quote_kind=quote_kind,
                        generation=generation,
                    )
                )
            confirmed_symbols = {
                diagnostic.symbol
                for diagnostic in diagnostics
                if diagnostic.service == SERVICE_LEVELONE_OPTIONS
                and diagnostic.state == "subscribed"
                and diagnostic.confirmed
            }
            invalidated = [
                state.snapshot()
                for state in self._leg_states.values()
                if state.symbol not in confirmed_symbols and _clear_quote_confirmations(state)
            ]
        snapshot = tuple(enriched)
        with self._subscription_diagnostics_lock:
            self._subscription_diagnostics = snapshot
        for quote in invalidated:
            self.events.put({"type": "quote", "quote": quote, "generation": generation})
        self.events.put(
            {
                "type": "subscription_status",
                "generation": generation,
                "subscriptions": snapshot,
            }
        )


def parse_chain_records(
    payload: Mapping[str, Any],
    *,
    expiration: str,
    symbol_id_for: Callable[[str], int],
) -> list[OptionContractRecord]:
    """Flatten a Schwab /chains response into OptionContractRecords.

    The maps are keyed "YYYY-MM-DD:daysToExpiration" -> strike -> contract(s);
    the per-strike value is a list in practice (a single object per the spec).
    """
    records: list[OptionContractRecord] = []
    underlying_symbol = str(payload.get("symbol") or "")
    for map_name in ("callExpDateMap", "putExpDateMap"):
        expiration_map = payload.get(map_name)
        if not isinstance(expiration_map, Mapping):
            continue
        for date_key, strikes_map in expiration_map.items():
            date_part = str(date_key).split(":", 1)[0]
            if expiration_to_compact(date_part) != expiration:
                continue
            if not isinstance(strikes_map, Mapping):
                continue
            for contract in _iter_chain_contracts(strikes_map):
                record = _record_from_chain_contract(
                    contract,
                    underlying_symbol=underlying_symbol,
                    expiration=expiration,
                    symbol_id_for=symbol_id_for,
                )
                if record is not None:
                    records.append(record)
    records.sort(key=lambda record: (record.right, record.strike))
    return records


def _chain_underlying_price(payload: Mapping[str, Any]) -> float | None:
    """Best usable underlying price in a /chains response (None when absent)."""
    candidates: list[Any] = [payload.get("underlyingPrice")]
    underlying = payload.get("underlying")
    if isinstance(underlying, Mapping):
        candidates.extend((underlying.get("mark"), underlying.get("last"), underlying.get("close")))
    for candidate in candidates:
        value = _stream_number(candidate)
        if value is not None and value > 0:
            return value
    return None


def _iter_chain_contracts(strikes_map: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    contracts: list[Mapping[str, Any]] = []
    for value in strikes_map.values():
        if isinstance(value, Mapping):
            contracts.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            contracts.extend(entry for entry in value if isinstance(entry, Mapping))
    return contracts


def _record_from_chain_contract(
    contract: Mapping[str, Any],
    *,
    underlying_symbol: str,
    expiration: str,
    symbol_id_for: Callable[[str], int],
) -> OptionContractRecord | None:
    try:
        right = normalize_right(str(contract.get("putCall") or ""))
        strike = float(contract.get("strikePrice"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(strike):
        return None

    option_symbol = str(contract.get("symbol") or "").strip()
    if not option_symbol:
        option_symbol = build_option_symbol(underlying_symbol, expiration, right, strike)

    raw_multiplier = contract.get("multiplier")
    try:
        multiplier_value = float(raw_multiplier)
        multiplier = str(int(multiplier_value)) if multiplier_value.is_integer() else str(multiplier_value)
    except (TypeError, ValueError):
        multiplier = "100"

    return OptionContractRecord(
        con_id=symbol_id_for(option_symbol),
        symbol=underlying_symbol,
        expiration=expiration,
        strike=strike,
        right=right,
        exchange=str(contract.get("exchangeName") or ""),
        currency="USD",
        trading_class=str(contract.get("optionRoot") or underlying_symbol),
        multiplier=multiplier,
        local_symbol=option_symbol,
    )


def _stream_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _apply_stream_fields(
    state: Any,
    item: Mapping[str, Any],
    field_map: Mapping[str, str],
) -> tuple[bool, bool, bool]:
    """Merge changed streamer fields into a quote state (Change delivery type).

    Returns (bid_changed, ask_changed, any_changed) so callers can stamp
    received-at times only when cumulative state actually changed.
    """
    bid_touched = False
    ask_touched = False
    any_applied = False
    for field_key, attribute in field_map.items():
        if field_key not in item:
            continue
        numeric = _stream_number(item[field_key])
        if numeric is None:
            continue
        if numeric < 0:
            # Schwab streams negative sentinels when a side has no live quote;
            # keep the last good value so synthetic spread quotes stay usable
            # (stale) instead of dropping out of the fair-value curve.
            continue
        if getattr(state, attribute) == numeric:
            continue
        setattr(state, attribute, numeric)
        any_applied = True
        if attribute in {"bid", "bid_size"}:
            bid_touched = True
        elif attribute in {"ask", "ask_size"}:
            ask_touched = True
    return bid_touched, ask_touched, any_applied


def _clear_quote_confirmations(state: _LegQuoteState) -> bool:
    changed = state.bid_confirmed_at is not None or state.ask_confirmed_at is not None
    state.bid_confirmed_at = None
    state.ask_confirmed_at = None
    return changed


def _confirm_stream_prices(
    state: _LegQuoteState,
    item: Mapping[str, Any],
    received_at: datetime,
) -> bool:
    """Track explicit price confirmations without refreshing recorded change times."""
    previous = (state.bid_confirmed_at, state.ask_confirmed_at)
    confirmed_at = received_at.astimezone(timezone.utc)
    for price_key, size_key, attribute in (
        ("2", "16", "bid_confirmed_at"),
        ("3", "17", "ask_confirmed_at"),
    ):
        if price_key in item:
            price = _stream_number(item[price_key])
            setattr(state, attribute, confirmed_at if price is not None and price >= 0 else None)
        if size_key in item:
            size = _stream_number(item[size_key])
            if size is None or size < 0:
                setattr(state, attribute, None)
    if (
        state.market_data_type != LIVE_MARKET_DATA_TYPE
        or (state.bid is not None and state.ask is not None and state.bid > state.ask)
    ):
        _clear_quote_confirmations(state)
    return previous != (state.bid_confirmed_at, state.ask_confirmed_at)


def _stream_epoch_ms(value: Any) -> datetime | None:
    """Schwab streamer quote-time fields are milliseconds since the epoch."""
    numeric = _stream_number(value)
    if numeric is None or numeric <= 0:
        return None
    return datetime.fromtimestamp(numeric / 1000.0, tz=timezone.utc)


def exchange_authorization_code(
    authorization_code: str,
    redirect_uri: str,
    *,
    app_key: str | None = None,
    app_secret: str | None = None,
    token_url: str = OAUTH_TOKEN_URL,
    timeout: float = REST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One-time helper: exchange an OAuth authorization code for tokens.

    Visit
    ``https://api.schwabapi.com/v1/oauth/authorize?response_type=code&client_id=<YOUR_APP_KEY>&scope=readonly&redirect_uri=<YOUR_REDIRECT_URI>``
    in a browser, approve access, and copy the ``code`` query parameter from
    the redirect. Store the returned ``refresh_token`` in the
    ``SCHWAB_REFRESH_TOKEN`` environment variable (or a token file referenced
    by ``SCHWAB_TOKEN_PATH``). Do not commit tokens to source control.
    """
    key = (app_key or os.environ.get(ENV_APP_KEY) or "").strip()
    secret = (app_secret or os.environ.get(ENV_APP_SECRET) or "").strip()
    if key in _PLACEHOLDER_VALUES or secret in _PLACEHOLDER_VALUES:
        raise SchwabConfigurationError(
            f"Set {ENV_APP_KEY} and {ENV_APP_SECRET} before exchanging an authorization code."
        )
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": redirect_uri,
        }
    ).encode("ascii")
    basic = base64.b64encode(f"{key}:{secret}".encode("ascii")).decode("ascii")
    request = urllib.request.Request(
        token_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _read_http_error(exc)
        raise SchwabRequestError(
            f"Authorization-code exchange failed (HTTP {exc.code}): {detail}"
        ) from exc
