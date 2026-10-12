from __future__ import annotations

import json
import io
import queue
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from options_matrix.models import (
    DIRECTION_DEBIT,
    LIVE_MARKET_DATA_TYPE,
    OptionChainDefinition,
    OptionContractRecord,
    QUOTE_KIND_LEG,
    QUOTE_KIND_UNDERLYING,
    RIGHT_CALL,
    SearchResult,
    UnderlyingContract,
    VerticalPair,
    resolve_vertical_selection,
)
from options_matrix.schwab_client import (
    SERVICE_ADMIN,
    SERVICE_LEVELONE_EQUITIES,
    SERVICE_LEVELONE_OPTIONS,
    STREAMER_MAX_SYMBOLS_PER_REQUEST,
    STREAMER_RECONNECT_STABILITY_SECONDS,
    SchwabConfigurationError,
    SchwabCredentials,
    SchwabMarketDataService,
    SchwabRequestError,
    SchwabRestClient,
    SchwabStreamerClient,
    SchwabTokenManager,
    StreamerInfo,
    SubscriptionDiagnostic,
    build_option_symbol,
    default_credentials_path,
    expiration_to_compact,
    expiration_to_iso,
    parse_chain_records,
    parse_option_symbol,
    refresh_token_issued_at,
)


def _chain_contract(right: str, symbol: str, strike: float) -> dict:
    return {"putCall": right, "symbol": symbol, "strikePrice": strike, "multiplier": 100.0}


def _spy_search_result(service: SchwabMarketDataService) -> SearchResult:
    underlying = UnderlyingContract(
        symbol="SPY",
        con_id=service._symbol_id("EQ:SPY"),
        exchange="NASDAQ",
        primary_exchange="NASDAQ",
        currency="USD",
    )
    chain = OptionChainDefinition(
        symbol="SPY",
        underlying_con_id=underlying.con_id,
        exchange="NASDAQ",
        trading_class="SPY",
        multiplier="100",
        expirations=("20251219",),
        strikes=(),
    )
    return SearchResult(underlying=underlying, chain=chain)


class _FakeStreamer:
    def __init__(self) -> None:
        self.logged_in = True
        self.subscribed: dict[str, set[str]] = {}
        self.unsubscribed: dict[str, set[str]] = {}
        self.subscribe_calls: list[tuple[str, tuple[str, ...]]] = []
        self.unsubscribe_calls: list[tuple[str, tuple[str, ...]]] = []
        self.cleared = False
        self.fail_subscribe_count = 0
        self.fail_unsubscribe_count = 0

    def subscribe(self, service: str, symbols, fields: str) -> None:
        if self.fail_subscribe_count:
            self.fail_subscribe_count -= 1
            raise SchwabRequestError("injected subscribe failure")
        self.subscribed.setdefault(service, set()).update(symbols)
        self.subscribe_calls.append((service, tuple(symbols)))

    def unsubscribe(self, service: str, symbols) -> None:
        if self.fail_unsubscribe_count:
            self.fail_unsubscribe_count -= 1
            raise SchwabRequestError("injected unsubscribe failure")
        self.unsubscribed.setdefault(service, set()).update(symbols)
        self.unsubscribe_calls.append((service, tuple(symbols)))

    def clear_subscriptions(self) -> None:
        self.cleared = True

    def stop(self) -> None:
        self.logged_in = False


class _LifecycleStreamer:
    def __init__(
        self,
        _tokens,
        _streamer_info,
        *,
        on_data,
        on_status,
        on_subscription_state,
        start_error: Exception | None = None,
    ) -> None:
        self.on_data = on_data
        self.on_status = on_status
        self.on_subscription_state = on_subscription_state
        self.start_error = start_error
        self.logged_in = False
        self.start_calls = 0
        self.stop_calls = 0
        self.clear_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        self.logged_in = True

    def stop(self) -> None:
        self.stop_calls += 1
        self.logged_in = False

    def clear_subscriptions(self) -> None:
        self.clear_calls += 1

    def subscription_diagnostics(self) -> tuple[SubscriptionDiagnostic, ...]:
        return ()


def _streamer_preferences() -> dict[str, object]:
    return {
        "streamerInfo": [
            {
                "streamerSocketUrl": "wss://streamer.example",
                "schwabClientCustomerId": "customer",
                "schwabClientCorrelId": "correl",
                "schwabClientChannel": "N9",
                "schwabClientFunctionId": "APIAPP",
            }
        ]
    }


class _FakeRestClient:
    """Offline stand-in for SchwabRestClient (only the methods the service uses)."""

    def __init__(self, chain_payload: dict | None = None, server_time_value=None) -> None:
        self.chain_payload = chain_payload or {}
        self.server_time_value = server_time_value
        self.chain_calls: list[tuple[str, str | None, str | None]] = []

    def get_chain(self, symbol: str, *, from_date=None, to_date=None, contract_type="ALL"):
        self.chain_calls.append((symbol, from_date, to_date))
        return self.chain_payload

    def server_time(self):
        return self.server_time_value


class _FakeTokenManager:
    def __init__(self) -> None:
        self.access_calls: list[bool] = []
        self.invalidations = 0

    def access_token(self, *, force_refresh: bool = False) -> str:
        self.access_calls.append(force_refresh)
        return "access-token"

    def invalidate(self) -> None:
        self.invalidations += 1


class _SendingApp:
    def __init__(self) -> None:
        self.sock = SimpleNamespace(connected=True)
        self.sent: list[str] = []
        self.fail_send_count = 0
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(payload)
        if self.fail_send_count:
            self.fail_send_count -= 1
            raise OSError("injected socket send failure")

    def close(self) -> None:
        self.closed = True


class _UrlResponse:
    def __init__(self, body: str = "{}", headers: dict | None = None) -> None:
        self._body = body.encode("utf-8")
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _streamer_client(*, statuses: list[str] | None = None) -> SchwabStreamerClient:
    captured_statuses = [] if statuses is None else statuses
    with patch("options_matrix.schwab_client.websocket", SimpleNamespace()):
        return SchwabStreamerClient(
            _FakeTokenManager(),
            StreamerInfo("wss://streamer.example", "customer", "correl", "N9", "APIAPP"),
            on_data=lambda _service, _item: None,
            on_status=captured_statuses.append,
        )


def _sent_requests(app: _SendingApp) -> list[dict]:
    return [
        request
        for payload in app.sent
        for request in json.loads(payload).get("requests", [])
    ]


def _acknowledge(client: SchwabStreamerClient, request: dict, code: int = 0) -> None:
    client._handle_response(
        {
            "service": request["service"],
            "command": request["command"],
            "requestid": request["requestid"],
            "content": {"code": code, "msg": "ok" if code == 0 else "injected failure"},
        }
    )


def _acknowledge_login(client: SchwabStreamerClient) -> None:
    client._handle_response(
        {
            "service": SERVICE_ADMIN,
            "command": "LOGIN",
            "requestid": "login",
            "content": {"code": 0, "msg": "ok"},
        }
    )


def _drain_quotes(events: queue.Queue) -> list:
    quotes = []
    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            return quotes
        if event.get("type") == "quote":
            quotes.append(event["quote"])


class OptionSymbolTests(unittest.TestCase):
    def test_build_option_symbol_pads_root_and_strike(self) -> None:
        self.assertEqual(
            build_option_symbol("AAPL", "20251219", RIGHT_CALL, 200.0),
            "AAPL  251219C00200000",
        )

    def test_build_option_symbol_fractional_strike(self) -> None:
        self.assertEqual(
            build_option_symbol("SPY", "20250620", "PUT", 452.5),
            "SPY   250620P00452500",
        )

    def test_parse_option_symbol_round_trip(self) -> None:
        symbol = build_option_symbol("F", "20260116", "CALL", 12.5)
        root, expiration, right, strike = parse_option_symbol(symbol)
        self.assertEqual(root, "F")
        self.assertEqual(expiration, "20260116")
        self.assertEqual(right, "CALL")
        self.assertEqual(strike, 12.5)

    def test_expiration_conversions(self) -> None:
        self.assertEqual(expiration_to_iso("20251219"), "2025-12-19")
        self.assertEqual(expiration_to_iso("2025-12-19"), "2025-12-19")
        self.assertEqual(expiration_to_compact("2025-12-19"), "20251219")
        self.assertEqual(expiration_to_compact("20251219"), "20251219")


class CredentialTests(unittest.TestCase):
    def test_missing_credentials_raise_with_env_names(self) -> None:
        with self.assertRaises(SchwabConfigurationError) as context:
            SchwabCredentials.from_environment({})
        message = str(context.exception)
        self.assertIn("SCHWAB_APP_KEY", message)
        self.assertIn("SCHWAB_REFRESH_TOKEN", message)

    def test_placeholder_credentials_raise(self) -> None:
        with self.assertRaises(SchwabConfigurationError):
            SchwabCredentials.from_environment(
                {
                    "SCHWAB_APP_KEY": "YOUR_SCHWAB_APP_KEY",
                    "SCHWAB_APP_SECRET": "YOUR_SCHWAB_APP_SECRET",
                    "SCHWAB_REFRESH_TOKEN": "YOUR_SCHWAB_REFRESH_TOKEN",
                }
            )

    def test_configured_credentials_pass(self) -> None:
        credentials = SchwabCredentials.from_environment(
            {
                "SCHWAB_APP_KEY": "key",
                "SCHWAB_APP_SECRET": "secret",
                "SCHWAB_REFRESH_TOKEN": "token",
            }
        )
        self.assertEqual(credentials.app_key, "key")

    def test_credentials_file_supplies_all_three_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schwab_token.json"
            path.write_text(
                json.dumps(
                    {
                        "app_key": "file-key",
                        "app_secret": "file-secret",
                        "refresh_token": "file-token",
                    }
                ),
                encoding="utf-8",
            )
            credentials = SchwabCredentials.from_environment({"SCHWAB_TOKEN_PATH": str(path)})
        self.assertEqual(credentials.app_key, "file-key")
        self.assertEqual(credentials.app_secret, "file-secret")
        self.assertEqual(credentials.refresh_token, "file-token")

    def test_environment_variables_take_precedence_over_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schwab_token.json"
            path.write_text(
                json.dumps(
                    {
                        "app_key": "file-key",
                        "app_secret": "file-secret",
                        "refresh_token": "file-token",
                    }
                ),
                encoding="utf-8",
            )
            credentials = SchwabCredentials.from_environment(
                {
                    "SCHWAB_TOKEN_PATH": str(path),
                    "SCHWAB_APP_KEY": "env-key",
                }
            )
        self.assertEqual(credentials.app_key, "env-key")
        self.assertEqual(credentials.app_secret, "file-secret")

    def test_stale_legacy_token_path_migrates_to_schwab_auth_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            legacy_path = project_root / "schwab_token.json"
            current_path = project_root / "schwab_auth" / "schwab_token.json"
            current_path.parent.mkdir()
            current_path.write_text(
                json.dumps(
                    {
                        "app_key": "file-key",
                        "app_secret": "file-secret",
                        "refresh_token": "file-token",
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "options_matrix.schwab_client.default_credentials_path",
                return_value=current_path,
            ):
                credentials = SchwabCredentials.from_environment(
                    {"SCHWAB_TOKEN_PATH": str(legacy_path)}
                )

        self.assertEqual(credentials.app_key, "file-key")
        self.assertEqual(credentials.app_secret, "file-secret")
        self.assertEqual(credentials.refresh_token, "file-token")

    def test_unrelated_missing_custom_token_path_is_not_redirected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            custom_path = project_root / "custom" / "missing-token.json"
            current_path = project_root / "schwab_auth" / "schwab_token.json"
            current_path.parent.mkdir()
            current_path.write_text(
                json.dumps(
                    {
                        "app_key": "file-key",
                        "app_secret": "file-secret",
                        "refresh_token": "file-token",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch(
                    "options_matrix.schwab_client.default_credentials_path",
                    return_value=current_path,
                ),
                self.assertRaises(SchwabConfigurationError) as context,
            ):
                SchwabCredentials.from_environment(
                    {"SCHWAB_TOKEN_PATH": str(custom_path)}
                )

        self.assertIn(str(custom_path), str(context.exception))

    def test_refresh_token_issued_at_parses_and_tolerates_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "schwab_token.json"

            self.assertIsNone(refresh_token_issued_at(token_path))

            token_path.write_text(json.dumps({"refresh_token": "x"}), encoding="utf-8")
            self.assertIsNone(refresh_token_issued_at(token_path))

            token_path.write_text(
                json.dumps({"refresh_token_issued_at": "not a date"}),
                encoding="utf-8",
            )
            self.assertIsNone(refresh_token_issued_at(token_path))

            issued = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
            token_path.write_text(
                json.dumps({"refresh_token_issued_at": issued.isoformat()}),
                encoding="utf-8",
            )
            self.assertEqual(refresh_token_issued_at(token_path), issued)

    def test_default_credentials_path_is_in_schwab_auth_folder(self) -> None:
        path = default_credentials_path()
        self.assertEqual(path.name, "schwab_token.json")
        self.assertEqual(path.parent.name, "schwab_auth")
        self.assertEqual(path.parent.parent.name, "mini_pc")

    def test_placeholder_file_values_raise_and_name_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schwab_token.json"
            path.write_text(
                json.dumps(
                    {
                        "app_key": "YOUR_SCHWAB_APP_KEY",
                        "app_secret": "YOUR_SCHWAB_APP_SECRET",
                        "refresh_token": "YOUR_SCHWAB_REFRESH_TOKEN",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(SchwabConfigurationError) as context:
                SchwabCredentials.from_environment({"SCHWAB_TOKEN_PATH": str(path)})
        self.assertIn(str(path), str(context.exception))


class StreamerInfoTests(unittest.TestCase):
    def test_from_user_preferences(self) -> None:
        info = StreamerInfo.from_user_preferences(
            {
                "streamerInfo": [
                    {
                        "streamerSocketUrl": "wss://streamer.example",
                        "schwabClientCustomerId": "customer",
                        "schwabClientCorrelId": "correl",
                        "schwabClientChannel": "N9",
                        "schwabClientFunctionId": "APIAPP",
                    }
                ]
            }
        )
        self.assertEqual(info.socket_url, "wss://streamer.example")
        self.assertEqual(info.customer_id, "customer")

    def test_missing_streamer_info_raises(self) -> None:
        with self.assertRaises(Exception):
            StreamerInfo.from_user_preferences({"streamerInfo": []})


class StreamerSubscriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.statuses: list[str] = []
        self.client = _streamer_client(statuses=self.statuses)
        self.app = _SendingApp()
        self.client._app = self.app

    def test_messages_from_retired_or_lost_sockets_cannot_refresh_quotes(self) -> None:
        on_data = Mock()
        self.client._on_data = on_data
        message = json.dumps({"data": [{
            "service": SERVICE_LEVELONE_OPTIONS,
            "content": [{"key": "OPT", "2": 2.0, "3": 2.2}],
        }]})
        self.client._handle_message(_SendingApp(), message)
        on_data.assert_not_called()
        self.client._handle_message(self.app, message)
        self.assertEqual(on_data.call_count, 1)
        self.client._last_lost_app = self.app
        self.client._handle_message(self.app, message)
        self.assertEqual(on_data.call_count, 1)

    def test_explicit_dummy_socket_message_remains_supported(self) -> None:
        on_data = Mock()
        self.client._on_data = on_data
        self.client._handle_message(None, json.dumps({"data": [{
            "service": SERVICE_LEVELONE_OPTIONS,
            "content": [{"key": "OPT", "2": 2.0}],
        }]}))
        on_data.assert_called_once_with(SERVICE_LEVELONE_OPTIONS, {"key": "OPT", "2": 2.0})

    def test_synchronous_subscribe_failure_is_retryable(self) -> None:
        self.app.fail_send_count = 1
        with self.assertRaises(SchwabRequestError):
            self.client.subscribe(SERVICE_LEVELONE_OPTIONS, ["OPT"], "0,2,3")

        failed = self.client.subscription_diagnostics()[0]
        self.assertEqual(failed.state, "failed")
        self.assertTrue(failed.desired)
        self.assertIn("injected socket send failure", failed.error or "")

        self.client.subscribe(SERVICE_LEVELONE_OPTIONS, ["OPT"], "0,2,3")
        self.assertEqual(len(self.app.sent), 2)
        pending = self.client.subscription_diagnostics()[0]
        self.assertEqual(pending.state, "pending_subscribe")
        self.assertEqual(pending.pending_command, "ADD")
        self.assertFalse(pending.confirmed)
        _acknowledge(self.client, _sent_requests(self.app)[-1])
        confirmed = self.client.subscription_diagnostics()[0]
        self.assertEqual(confirmed.state, "subscribed")
        self.assertTrue(confirmed.confirmed)

    def test_server_subscribe_failure_is_visible_and_retryable(self) -> None:
        self.client.subscribe(SERVICE_LEVELONE_OPTIONS, ["OPT"], "0,2,3")
        first_request = _sent_requests(self.app)[-1]
        _acknowledge(self.client, first_request, code=99)

        diagnostic = self.client.subscription_diagnostics()[0]
        self.assertEqual(diagnostic.state, "failed")
        self.assertIn("99", diagnostic.error or "")

        self.client.subscribe(SERVICE_LEVELONE_OPTIONS, ["OPT"], "0,2,3")
        self.assertEqual(len(self.app.sent), 2)

    def test_exact_target_clears_abandoned_failed_intent(self) -> None:
        self.app.fail_send_count = 1
        with self.assertRaises(SchwabRequestError):
            self.client.set_subscriptions(SERVICE_LEVELONE_OPTIONS, ["OPT"], "0,2,3")
        self.assertEqual(self.client.subscription_diagnostics()[0].state, "failed")

        self.client.set_subscriptions(SERVICE_LEVELONE_OPTIONS, [], "0,2,3")
        self.assertEqual(self.client.subscription_diagnostics(), ())

    def test_string_server_failure_code_is_not_treated_as_success(self) -> None:
        self.client.subscribe(SERVICE_LEVELONE_OPTIONS, ["OPT"], "0,2,3")
        request = _sent_requests(self.app)[-1]
        self.client._handle_response(
            {
                "service": request["service"],
                "command": request["command"],
                "requestid": request["requestid"],
                "content": {"code": "99", "msg": "denied"},
            }
        )
        self.assertEqual(self.client.subscription_diagnostics()[0].state, "failed")

    def test_synchronous_unsubscribe_failure_rolls_back_pending_state(self) -> None:
        self.client.subscribe(SERVICE_LEVELONE_OPTIONS, ["OPT"], "0,2,3")
        _acknowledge(self.client, _sent_requests(self.app)[-1])

        self.app.fail_send_count = 1
        with self.assertRaises(SchwabRequestError):
            self.client.unsubscribe(SERVICE_LEVELONE_OPTIONS, ["OPT"])
        diagnostic = self.client.subscription_diagnostics()[0]
        self.assertEqual(diagnostic.state, "failed")
        self.assertFalse(diagnostic.desired)
        self.assertTrue(diagnostic.confirmed)
        self.assertTrue(diagnostic.failed)

        self.client.unsubscribe(SERVICE_LEVELONE_OPTIONS, ["OPT"])
        _acknowledge(self.client, _sent_requests(self.app)[-1])
        self.assertEqual(self.client.subscription_diagnostics(), ())

    def test_large_add_and_unsubscribe_requests_are_chunked(self) -> None:
        symbols = [
            f"OPT-{index:04d}"
            for index in range(STREAMER_MAX_SYMBOLS_PER_REQUEST * 2 + 1)
        ]
        self.client.subscribe(SERVICE_LEVELONE_OPTIONS, symbols, "0,2,3")
        add_requests = _sent_requests(self.app)
        self.assertEqual([request["command"] for request in add_requests], ["ADD", "ADD", "ADD"])
        self.assertEqual(
            [len(request["parameters"]["keys"].split(",")) for request in add_requests],
            [STREAMER_MAX_SYMBOLS_PER_REQUEST, STREAMER_MAX_SYMBOLS_PER_REQUEST, 1],
        )
        for request in add_requests:
            _acknowledge(self.client, request)

        self.app.sent.clear()
        self.client.unsubscribe(SERVICE_LEVELONE_OPTIONS, symbols)
        unsubscribe_requests = _sent_requests(self.app)
        self.assertEqual(
            [request["command"] for request in unsubscribe_requests],
            ["UNSUBS", "UNSUBS", "UNSUBS"],
        )
        self.assertEqual(
            [len(request["parameters"]["keys"].split(",")) for request in unsubscribe_requests],
            [STREAMER_MAX_SYMBOLS_PER_REQUEST, STREAMER_MAX_SYMBOLS_PER_REQUEST, 1],
        )

    def test_reconnect_continues_after_multiple_failed_attempts(self) -> None:
        class _ImmediateThread:
            def __init__(self, *, target, **_kwargs) -> None:
                self._target = target

            def start(self) -> None:
                self._target()

        self.client._start_connection = Mock(
            side_effect=[
                SchwabRequestError("first failure"),
                SchwabRequestError("second failure"),
                None,
            ]
        )
        with patch("options_matrix.schwab_client._time.sleep"), patch(
            "options_matrix.schwab_client.threading.Thread", _ImmediateThread
        ):
            self.client._schedule_reconnect()

        self.assertEqual(self.client._start_connection.call_count, 3)
        self.assertTrue(any("first failure" in status for status in self.statuses))
        self.assertTrue(any("second failure" in status for status in self.statuses))
        self.assertEqual(self.statuses[-1], "Schwab streamer reconnected.")

    def test_rapid_successful_login_then_drop_advances_all_reconnect_attempts(self) -> None:
        class _DormantThread:
            def __init__(self, **_kwargs) -> None:
                return

            def start(self) -> None:
                return

        self.assertEqual(STREAMER_RECONNECT_STABILITY_SECONDS, 30.0)
        with patch(
            "options_matrix.schwab_client._time.monotonic",
            return_value=100.0,
        ), patch(
            "options_matrix.schwab_client.threading.Thread",
            _DormantThread,
        ):
            for attempt in range(1, 6):
                app = object()
                self.client._app = app
                self.client._last_lost_app = None
                _acknowledge_login(self.client)
                self.client._handle_connection_loss(app, 1006, "rapid drop")
                self.assertEqual(self.client._reconnect_attempts, attempt)
                # Model the scheduled reconnect taking ownership before the
                # next short-lived connection is established.
                self.client._reconnect_scheduled = False

        reconnect_statuses = [
            status
            for status in self.statuses
            if "streamer reconnecting" in status
        ]
        self.assertEqual(
            reconnect_statuses,
            [
                "Schwab streamer reconnecting in 2s (attempt 1/5) ...",
                "Schwab streamer reconnecting in 4s (attempt 2/5) ...",
                "Schwab streamer reconnecting in 8s (attempt 3/5) ...",
                "Schwab streamer reconnecting in 16s (attempt 4/5) ...",
                "Schwab streamer reconnecting in 30s (attempt 5/5) ...",
            ],
        )

    def test_stable_logged_in_connection_resets_prior_reconnect_attempts(self) -> None:
        class _DormantThread:
            def __init__(self, **_kwargs) -> None:
                return

            def start(self) -> None:
                return

        self.client._reconnect_attempts = 4
        app = object()
        self.client._app = app
        with patch(
            "options_matrix.schwab_client._time.monotonic",
            side_effect=[100.0, 100.0 + STREAMER_RECONNECT_STABILITY_SECONDS],
        ), patch(
            "options_matrix.schwab_client.threading.Thread",
            _DormantThread,
        ):
            _acknowledge_login(self.client)
            self.client._handle_connection_loss(app, 1006, "stable drop")

        self.assertEqual(self.client._reconnect_attempts, 1)
        self.assertEqual(
            self.statuses[-1],
            "Schwab streamer reconnecting in 2s (attempt 1/5) ...",
        )

    def test_deliberate_stop_prevents_reconnect(self) -> None:
        self.client._login_established_monotonic = 100.0
        self.client.stop()
        with patch("options_matrix.schwab_client.threading.Thread") as thread_class:
            self.client._schedule_reconnect()
        thread_class.assert_not_called()
        self.assertIsNone(self.client._login_established_monotonic)
        self.assertFalse(any("streamer reconnecting" in status for status in self.statuses))


class ServiceConnectionLifecycleTests(unittest.TestCase):
    def _service(self) -> SchwabMarketDataService:
        rest = Mock()
        rest.get_user_preferences.return_value = _streamer_preferences()
        return SchwabMarketDataService(
            token_manager=_FakeTokenManager(),
            rest_client=rest,
        )

    @staticmethod
    def _drain_events(service: SchwabMarketDataService) -> list[dict]:
        events: list[dict] = []
        while True:
            try:
                events.append(service.events.get_nowait())
            except queue.Empty:
                return events

    def test_connect_replaces_nonlogged_streamer_and_scopes_callbacks_to_replacement(self) -> None:
        service = self._service()
        old = SimpleNamespace(
            logged_in=False,
            clear_subscriptions=Mock(),
            stop=Mock(),
        )
        service._streamer = old
        created: list[_LifecycleStreamer] = []

        def create_streamer(*args, **kwargs) -> _LifecycleStreamer:
            streamer = _LifecycleStreamer(*args, **kwargs)
            created.append(streamer)
            return streamer

        with patch(
            "options_matrix.schwab_client.SchwabStreamerClient",
            side_effect=create_streamer,
        ):
            service.connect()
            first = created[-1]
            service.connect()
            second = created[-1]

        old.stop.assert_called_once_with()
        self.assertEqual(first.stop_calls, 1)
        self.assertIs(service._streamer, second)
        self.assertTrue(service.connected)
        self.assertTrue(service.connection_active)

        self._drain_events(service)
        with patch.object(service, "_handle_stream_data") as handle_data, patch.object(
            service,
            "_handle_subscription_diagnostics",
        ) as handle_diagnostics:
            first.on_data(SERVICE_LEVELONE_OPTIONS, {"key": "ORPHAN"})
            first.on_subscription_state(())
            second.on_data(SERVICE_LEVELONE_OPTIONS, {"key": "CURRENT"})
            second.on_subscription_state(())

        handle_data.assert_called_once_with(
            SERVICE_LEVELONE_OPTIONS,
            {"key": "CURRENT"},
        )
        handle_diagnostics.assert_called_once_with(())

        first.on_status("orphan status")
        self.assertEqual(self._drain_events(service), [])
        second.logged_in = False
        second.on_status("reconnecting")
        self.assertEqual(
            self._drain_events(service),
            [
                {
                    "type": "status",
                    "message": "reconnecting",
                    "connected": False,
                    "connection_active": True,
                }
            ],
        )
        second.logged_in = True
        second.on_status("current status")
        self.assertEqual(
            self._drain_events(service),
            [
                {
                    "type": "status",
                    "message": "current status",
                    "connected": True,
                    "connection_active": True,
                }
            ],
        )

        service.disconnect()
        disconnect_statuses = [
            event
            for event in self._drain_events(service)
            if event.get("type") == "status"
        ]
        self.assertEqual(
            disconnect_statuses[-1],
            {
                "type": "status",
                "message": "Disconnected.",
                "connected": False,
                "connection_active": False,
            },
        )

    def test_failed_replacement_start_is_stopped_detached_and_callbacks_retired(self) -> None:
        service = self._service()
        created: list[_LifecycleStreamer] = []

        def create_streamer(*args, **kwargs) -> _LifecycleStreamer:
            streamer = _LifecycleStreamer(
                *args,
                **kwargs,
                start_error=SchwabRequestError("injected start failure"),
            )
            created.append(streamer)
            return streamer

        with patch(
            "options_matrix.schwab_client.SchwabStreamerClient",
            side_effect=create_streamer,
        ), self.assertRaisesRegex(SchwabRequestError, "injected start failure"):
            service.connect()

        failed = created[0]
        self.assertEqual(failed.stop_calls, 1)
        self.assertIsNone(service._streamer)
        self.assertFalse(service.connected)
        self.assertFalse(service.connection_active)

        self._drain_events(service)
        failed.on_status("orphan after failure")
        self.assertEqual(self._drain_events(service), [])


class ChainParsingTests(unittest.TestCase):
    def _payload(self) -> dict:
        return {
            "symbol": "SPY",
            "callExpDateMap": {
                "2025-12-19:45": {
                    "450.0": [
                        {
                            "putCall": "CALL",
                            "symbol": "SPY   251219C00450000",
                            "strikePrice": 450.0,
                            "multiplier": 100.0,
                            "optionRoot": "SPY",
                            "exchangeName": "OPR",
                        }
                    ],
                    "451.0": [
                        {
                            "putCall": "CALL",
                            "symbol": "SPY   251219C00451000",
                            "strikePrice": 451.0,
                            "multiplier": 100.0,
                        }
                    ],
                },
                "2026-01-16:73": {
                    "450.0": [
                        {
                            "putCall": "CALL",
                            "symbol": "SPY   260116C00450000",
                            "strikePrice": 450.0,
                        }
                    ]
                },
            },
            "putExpDateMap": {
                "2025-12-19:45": {
                    "450.0": {
                        "putCall": "PUT",
                        "symbol": "SPY   251219P00450000",
                        "strikePrice": 450.0,
                    }
                }
            },
        }

    def test_parse_chain_records_filters_expiration(self) -> None:
        ids: dict[str, int] = {}

        def symbol_id_for(symbol: str) -> int:
            return ids.setdefault(symbol, len(ids) + 1)

        records = parse_chain_records(
            self._payload(),
            expiration="20251219",
            symbol_id_for=symbol_id_for,
        )
        self.assertEqual(len(records), 3)
        rights = {record.right for record in records}
        self.assertEqual(rights, {"CALL", "PUT"})
        call_strikes = sorted(record.strike for record in records if record.right == "CALL")
        self.assertEqual(call_strikes, [450.0, 451.0])
        for record in records:
            self.assertEqual(record.expiration, "20251219")
            self.assertEqual(record.symbol, "SPY")
            self.assertTrue(record.local_symbol.startswith("SPY   251219"))
            self.assertEqual(record.multiplier, "100")

    def test_parse_chain_records_stable_ids(self) -> None:
        ids: dict[str, int] = {}

        def symbol_id_for(symbol: str) -> int:
            return ids.setdefault(symbol, len(ids) + 1)

        first = parse_chain_records(self._payload(), expiration="20251219", symbol_id_for=symbol_id_for)
        second = parse_chain_records(self._payload(), expiration="20251219", symbol_id_for=symbol_id_for)
        self.assertEqual(
            {record.local_symbol: record.con_id for record in first},
            {record.local_symbol: record.con_id for record in second},
        )


class StreamQuoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SchwabMarketDataService()
        self.streamer = _FakeStreamer()
        self.service._streamer = self.streamer  # bypass network login for tests
        self.search_result = _spy_search_result(self.service)

        self.lower_symbol = build_option_symbol("SPY", "20251219", RIGHT_CALL, 450.0)
        self.upper_symbol = build_option_symbol("SPY", "20251219", RIGHT_CALL, 451.0)
        self.lower = OptionContractRecord(
            con_id=self.service._symbol_id(self.lower_symbol),
            symbol="SPY",
            expiration="20251219",
            strike=450.0,
            right=RIGHT_CALL,
            exchange="OPR",
            currency="USD",
            trading_class="SPY",
            multiplier="100",
            local_symbol=self.lower_symbol,
        )
        self.upper = OptionContractRecord(
            con_id=self.service._symbol_id(self.upper_symbol),
            symbol="SPY",
            expiration="20251219",
            strike=451.0,
            right=RIGHT_CALL,
            exchange="OPR",
            currency="USD",
            trading_class="SPY",
            multiplier="100",
            local_symbol=self.upper_symbol,
        )
        pair = VerticalPair(right=RIGHT_CALL, lower=self.lower, upper=self.upper)
        self.selection = resolve_vertical_selection(pair, DIRECTION_DEBIT)
        self.service.subscribe_matrix_quotes(self.search_result, [self.selection])
        _drain_quotes(self.service.events)

    def test_subscribe_targets_streamer_services(self) -> None:
        self.assertEqual(
            self.streamer.subscribed[SERVICE_LEVELONE_OPTIONS],
            {self.lower_symbol, self.upper_symbol},
        )
        self.assertEqual(self.streamer.subscribed[SERVICE_LEVELONE_EQUITIES], {"SPY"})

    def test_option_data_emits_leg_quotes(self) -> None:
        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {
                "key": self.lower_symbol,
                "delayed": False,
                "2": 5.0,
                "3": 5.2,
                "8": 1200,
                "9": 350,
                "10": 32.5,
                "16": 12,
                "17": 9,
            },
        )
        quotes = _drain_quotes(self.service.events)
        self.assertEqual(len(quotes), 1)
        leg_quote = quotes[0]
        self.assertEqual(leg_quote.kind, QUOTE_KIND_LEG)
        self.assertEqual(leg_quote.contract_con_id, self.lower.con_id)
        self.assertEqual(leg_quote.bid, 5.0)
        self.assertEqual(leg_quote.ask, 5.2)
        self.assertEqual(leg_quote.volume, 1200)
        self.assertEqual(leg_quote.open_interest, 350)
        self.assertEqual(leg_quote.implied_volatility, 0.325)
        self.assertEqual(leg_quote.market_data_type, LIVE_MARKET_DATA_TYPE)

        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {
                "key": self.upper_symbol,
                "delayed": False,
                "2": 3.0,
                "3": 3.1,
                "16": 7,
                "17": 20,
            },
        )
        quotes = _drain_quotes(self.service.events)
        self.assertEqual(len(quotes), 1)
        upper_quote = quotes[0]
        self.assertEqual(upper_quote.kind, QUOTE_KIND_LEG)
        self.assertEqual(upper_quote.contract_con_id, self.upper.con_id)
        self.assertEqual(upper_quote.bid, 3.0)
        self.assertEqual(upper_quote.ask, 3.1)
        self.assertEqual(upper_quote.bid_size, 7)
        self.assertEqual(upper_quote.ask_size, 20)

    def test_change_delivery_merges_partial_updates(self) -> None:
        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {"key": self.lower_symbol, "2": 5.0, "3": 5.2},
        )
        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {"key": self.lower_symbol, "3": 5.3},
        )
        quotes = _drain_quotes(self.service.events)
        last_leg = [quote for quote in quotes if quote.kind == QUOTE_KIND_LEG][-1]
        self.assertEqual(last_leg.bid, 5.0)  # retained from the first message
        self.assertEqual(last_leg.ask, 5.3)

    def test_negative_sentinels_keep_last_good_quote_for_synthetic_spreads(self) -> None:
        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {"key": self.lower_symbol, "2": 5.0, "3": 5.2, "16": 12, "17": 9},
        )
        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {"key": self.upper_symbol, "2": 3.0, "3": 3.1, "16": 7, "17": 20},
        )
        initial_quotes = _drain_quotes(self.service.events)
        initial_lower = next(
            quote
            for quote in initial_quotes
            if quote.kind == QUOTE_KIND_LEG and quote.contract_con_id == self.lower.con_id
        )
        self.assertIsNotNone(initial_lower.updated_at)

        # Schwab streams negative sentinels when a side drops; the stale values
        # must survive so the synthetic vertical stays quotable, and freshness
        # change timestamps must not be bumped, but live confirmations expire.
        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {"key": self.lower_symbol, "2": -1.0, "3": -1.0, "16": -1, "17": -1},
        )
        invalidated = _drain_quotes(self.service.events)
        self.assertEqual(len(invalidated), 1)
        self.assertIsNone(invalidated[0].bid_confirmed_at)
        self.assertIsNone(invalidated[0].ask_confirmed_at)
        leg = self.service._leg_states[self.lower.con_id].snapshot()
        self.assertEqual(leg.bid, 5.0)
        self.assertEqual(leg.ask, 5.2)
        self.assertEqual(leg.bid_size, 12)
        self.assertEqual(leg.ask_size, 9)
        self.assertEqual(leg.updated_at, initial_lower.updated_at)

    def test_unchanged_real_prices_emit_updated_confirmations(self) -> None:
        payload = {"key": self.lower_symbol, "2": 5.0, "3": 5.2}
        with patch("options_matrix.schwab_client.datetime") as clock:
            clock.now.side_effect = [
                datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 24, 15, 0, 1, tzinfo=timezone.utc),
            ]
            self.service._handle_stream_data(SERVICE_LEVELONE_OPTIONS, payload)
            initial = _drain_quotes(self.service.events)[0]

            self.service._handle_stream_data(SERVICE_LEVELONE_OPTIONS, payload)
            refreshed = _drain_quotes(self.service.events)[0]
        self.assertGreater(refreshed.bid_confirmed_at, initial.bid_confirmed_at)
        self.assertGreater(refreshed.ask_confirmed_at, initial.ask_confirmed_at)
        self.assertEqual(refreshed.updated_at, initial.updated_at)
        self.assertEqual(refreshed.bid_update_received_at, initial.bid_update_received_at)
        self.assertEqual(refreshed.ask_update_received_at, initial.ask_update_received_at)

    def test_market_data_type_only_change_emits_quote(self) -> None:
        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {"key": self.lower_symbol, "2": 5.0, "delayed": False},
        )
        _drain_quotes(self.service.events)

        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {"key": self.lower_symbol, "delayed": True},
        )
        quotes = _drain_quotes(self.service.events)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].market_data_type, 3)

    def test_quote_burst_coalesces_to_latest_cumulative_state(self) -> None:
        for bid in (5.0, 5.1, 5.2, 5.3):
            self.service._handle_stream_data(
                SERVICE_LEVELONE_OPTIONS,
                {"key": self.lower_symbol, "2": bid},
            )
        self.assertEqual(self.service.events.qsize(), 1)
        quotes = _drain_quotes(self.service.events)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].bid, 5.3)

    def test_status_events_survive_and_interleave_with_quote_bursts(self) -> None:
        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {"key": self.lower_symbol, "2": 5.0},
        )
        self.service._emit_status("connection status")
        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {"key": self.lower_symbol, "2": 5.1},
        )

        first = self.service.events.get_nowait()
        second = self.service.events.get_nowait()
        self.assertEqual(first["type"], "quote")
        self.assertEqual(first["quote"].bid, 5.1)
        self.assertEqual(
            second,
            {
                "type": "status",
                "message": "connection status",
                "connected": True,
                "connection_active": True,
            },
        )

    def test_quote_generation_keeps_old_and_new_pending_events_distinct(self) -> None:
        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {"key": self.lower_symbol, "2": 5.0},
        )
        generation = self.service.advance_quote_generation()
        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {"key": self.lower_symbol, "2": 5.1},
        )

        events = []
        while True:
            try:
                events.append(self.service.events.get_nowait())
            except queue.Empty:
                break
        quote_events = [event for event in events if event["type"] == "quote"]
        self.assertEqual([event["generation"] for event in quote_events], [0, generation])
        self.assertEqual(quote_events[-1]["quote"].bid, 5.1)

    def test_quote_time_fields_populate_market_side_timestamps(self) -> None:
        quote_time_ms = 1_766_000_000_000
        expected = datetime.fromtimestamp(quote_time_ms / 1000.0, tz=timezone.utc)

        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {"key": self.lower_symbol, "2": 5.0, "3": 5.2, "38": quote_time_ms},
        )
        option_quotes = _drain_quotes(self.service.events)
        leg = next(quote for quote in option_quotes if quote.kind == QUOTE_KIND_LEG)
        self.assertEqual(leg.quote_time, expected)

        self.service._handle_stream_data(
            SERVICE_LEVELONE_EQUITIES,
            {"key": "SPY", "1": 449.9, "2": 450.1, "34": quote_time_ms},
        )
        equity_quotes = _drain_quotes(self.service.events)
        underlying = next(quote for quote in equity_quotes if quote.kind == QUOTE_KIND_UNDERLYING)
        self.assertEqual(underlying.quote_time, expected)

    def test_delayed_flag_maps_to_market_data_type(self) -> None:
        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {"key": self.lower_symbol, "delayed": True, "2": 5.0, "3": 5.2},
        )
        quotes = _drain_quotes(self.service.events)
        self.assertEqual(quotes[0].market_data_type, 3)

    def test_equity_data_emits_underlying_quote(self) -> None:
        self.service._handle_stream_data(
            SERVICE_LEVELONE_EQUITIES,
            {"key": "SPY", "1": 449.9, "2": 450.1, "3": 450.0, "8": 1000000, "33": 450.05},
        )
        quotes = _drain_quotes(self.service.events)
        self.assertEqual(len(quotes), 1)
        underlying = quotes[0]
        self.assertEqual(underlying.kind, QUOTE_KIND_UNDERLYING)
        self.assertEqual(underlying.label, "SPY")
        self.assertEqual(underlying.mark, 450.05)
        self.assertEqual(underlying.bid, 449.9)
        self.assertEqual(underlying.ask, 450.1)

    def _record(self, strike: float) -> OptionContractRecord:
        symbol = build_option_symbol("SPY", "20251219", RIGHT_CALL, strike)
        return OptionContractRecord(
            con_id=self.service._symbol_id(symbol),
            symbol="SPY",
            expiration="20251219",
            strike=strike,
            right=RIGHT_CALL,
            exchange="OPR",
            currency="USD",
            trading_class="SPY",
            multiplier="100",
            local_symbol=symbol,
        )

    def test_overlapping_selections_reuse_leg_subscriptions(self) -> None:
        third = self._record(452.0)
        second_pair = VerticalPair(right=RIGHT_CALL, lower=self.upper, upper=third)
        second_selection = resolve_vertical_selection(second_pair, DIRECTION_DEBIT)

        # Add an overlapping spread (451/452): only the new 452 leg is ADDed.
        self.service.subscribe_matrix_quotes(
            self.search_result,
            [self.selection, second_selection],
        )
        option_adds = [
            symbols
            for service, symbols in self.streamer.subscribe_calls[1:]
            if service == SERVICE_LEVELONE_OPTIONS
        ]
        self.assertEqual(option_adds, [(third.local_symbol,)])
        self.assertNotIn(SERVICE_LEVELONE_OPTIONS, self.streamer.unsubscribed)

        # Drop the first spread: only its now-unused 450 leg is UNSUBSed.
        self.service.subscribe_matrix_quotes(self.search_result, [second_selection])
        self.assertEqual(
            self.streamer.unsubscribed[SERVICE_LEVELONE_OPTIONS],
            {self.lower_symbol},
        )
        self.assertEqual(
            self.streamer.subscribed[SERVICE_LEVELONE_OPTIONS],
            {self.lower_symbol, self.upper_symbol, third.local_symbol},
        )

    def test_service_rolls_back_failed_add_so_same_plan_retries(self) -> None:
        third = self._record(452.0)
        second_pair = VerticalPair(right=RIGHT_CALL, lower=self.upper, upper=third)
        second_selection = resolve_vertical_selection(second_pair, DIRECTION_DEBIT)
        self.streamer.fail_subscribe_count = 1

        with self.assertRaises(SchwabRequestError):
            self.service.subscribe_matrix_quotes(
                self.search_result,
                [self.selection, second_selection],
            )
        self.assertNotIn(third.con_id, self.service._leg_states)

        self.service.subscribe_matrix_quotes(
            self.search_result,
            [self.selection, second_selection],
        )
        self.assertIn(third.con_id, self.service._leg_states)
        self.assertEqual(
            self.streamer.subscribe_calls[-1],
            (SERVICE_LEVELONE_OPTIONS, (third.local_symbol,)),
        )

    def test_service_rolls_back_failed_unsubscribe_so_clear_retries(self) -> None:
        self.streamer.fail_unsubscribe_count = 1
        with self.assertRaises(SchwabRequestError):
            self.service.subscribe_matrix_quotes(self.search_result, [])
        self.assertEqual(set(self.service._leg_states), {self.lower.con_id, self.upper.con_id})

        self.service.subscribe_matrix_quotes(self.search_result, [])
        self.assertEqual(self.service._leg_states, {})
        self.assertIsNone(self.service._underlying_state)

    def test_subscription_diagnostics_are_generation_tagged_and_keyed_by_con_id(self) -> None:
        generation = self.service.advance_quote_generation()
        self.service._handle_subscription_diagnostics(
            (
                SubscriptionDiagnostic(
                    service=SERVICE_LEVELONE_OPTIONS,
                    symbol=self.lower_symbol,
                    state="pending_subscribe",
                    desired=True,
                ),
                SubscriptionDiagnostic(
                    service=SERVICE_LEVELONE_EQUITIES,
                    symbol="SPY",
                    state="subscribed",
                    desired=True,
                ),
            )
        )

        by_con_id = self.service.subscription_diagnostics_by_con_id()
        lower = by_con_id[self.lower.con_id]
        underlying = by_con_id[self.search_result.underlying.con_id]
        self.assertEqual(lower.state, "pending_subscribe")
        self.assertEqual(lower.quote_kind, QUOTE_KIND_LEG)
        self.assertEqual(lower.generation, generation)
        self.assertTrue(underlying.is_underlying)
        self.assertEqual(underlying.quote_kind, QUOTE_KIND_UNDERLYING)
        self.assertIs(
            self.service.subscription_state_for_con_id(self.lower.con_id),
            lower,
        )
        with self.assertRaises(TypeError):
            by_con_id[self.lower.con_id] = underlying

        subscription_events = []
        while True:
            try:
                event = self.service.events.get_nowait()
            except queue.Empty:
                break
            if event.get("type") == "subscription_status":
                subscription_events.append(event)
        self.assertEqual(subscription_events[-1]["generation"], generation)
        self.assertEqual(subscription_events[-1]["subscriptions"], tuple(by_con_id.values()))

    def test_clearing_selections_unsubscribes_everything(self) -> None:
        self.service.subscribe_matrix_quotes(self.search_result, [])
        self.assertEqual(
            self.streamer.unsubscribed[SERVICE_LEVELONE_OPTIONS],
            {self.lower_symbol, self.upper_symbol},
        )
        self.assertEqual(self.streamer.unsubscribed[SERVICE_LEVELONE_EQUITIES], {"SPY"})
        # Feeding data for the removed legs no longer emits quotes.
        self.service._handle_stream_data(
            SERVICE_LEVELONE_OPTIONS,
            {"key": self.lower_symbol, "2": 5.0, "3": 5.2},
        )
        self.assertEqual(_drain_quotes(self.service.events), [])


class DiscoveryTests(unittest.TestCase):
    def _service_with_chain(self) -> tuple[SchwabMarketDataService, _FakeRestClient]:
        payload = {
            "symbol": "SPY",
            "underlyingPrice": 105.37,
            "callExpDateMap": {
                "2025-12-19:45": {
                    "450.0": [_chain_contract("CALL", "SPY   251219C00450000", 450.0)],
                    "451.0": [_chain_contract("CALL", "SPY   251219C00451000", 451.0)],
                }
            },
            "putExpDateMap": {
                "2025-12-19:45": {
                    "450.0": [_chain_contract("PUT", "SPY   251219P00450000", 450.0)],
                    "451.0": [_chain_contract("PUT", "SPY   251219P00451000", 451.0)],
                }
            },
        }
        rest = _FakeRestClient(chain_payload=payload)
        service = SchwabMarketDataService(rest_client=rest)
        service._streamer = _FakeStreamer()  # bypass network login for tests
        return service, rest

    def test_load_option_records_caches_one_chain_pull_per_expiration(self) -> None:
        service, rest = self._service_with_chain()
        search_result = _spy_search_result(service)

        records = service.load_option_records(search_result, "20251219")
        repeat_records = service.load_option_records(search_result, "20251219")

        self.assertEqual(len(rest.chain_calls), 1)
        self.assertEqual(rest.chain_calls[0], ("SPY", "2025-12-19", "2025-12-19"))
        self.assertEqual(len(records), 4)
        self.assertEqual(records, repeat_records)

    def test_chain_underlying_price_hint_available_after_chain_pull(self) -> None:
        service, _rest = self._service_with_chain()
        search_result = _spy_search_result(service)

        self.assertIsNone(service.chain_underlying_price(search_result, "20251219"))
        service.load_option_records(search_result, "20251219")
        self.assertEqual(service.chain_underlying_price(search_result, "20251219"), 105.37)

    def test_chain_underlying_price_falls_back_to_underlying_mark(self) -> None:
        payload = {
            "symbol": "SPY",
            "underlyingPrice": 0,
            "underlying": {"mark": 104.5},
            "callExpDateMap": {
                "2025-12-19:45": {
                    "450.0": [_chain_contract("CALL", "SPY   251219C00450000", 450.0)],
                }
            },
        }
        service = SchwabMarketDataService(rest_client=_FakeRestClient(chain_payload=payload))
        service._streamer = _FakeStreamer()
        search_result = _spy_search_result(service)

        service.load_option_records(search_result, "20251219")
        self.assertEqual(service.chain_underlying_price(search_result, "20251219"), 104.5)

    def test_request_current_time_delegates_to_rest_client(self) -> None:
        expected = datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc)
        service = SchwabMarketDataService(rest_client=_FakeRestClient(server_time_value=expected))
        self.assertEqual(service.request_current_time(), expected)


class RestRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tokens = _FakeTokenManager()
        self.client = SchwabRestClient(self.tokens, base_url="https://example.test")

    @staticmethod
    def _http_error(code: int, *, retry_after: str | None = None) -> urllib.error.HTTPError:
        headers = {} if retry_after is None else {"Retry-After": retry_after}
        return urllib.error.HTTPError(
            "https://example.test/resource",
            code,
            "injected",
            headers,
            io.BytesIO(b"injected failure"),
        )

    def test_429_honors_retry_after_then_succeeds(self) -> None:
        with patch(
            "options_matrix.schwab_client.urllib.request.urlopen",
            side_effect=[self._http_error(429, retry_after="2.5"), _UrlResponse('{"ok": true}')],
        ) as urlopen, patch("options_matrix.schwab_client._time.sleep") as sleep:
            payload = self.client._get_json("https://example.test/resource", None)

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2.5)

    def test_selected_5xx_uses_bounded_backoff(self) -> None:
        with patch(
            "options_matrix.schwab_client.urllib.request.urlopen",
            side_effect=[self._http_error(503), _UrlResponse()],
        ), patch("options_matrix.schwab_client._time.sleep") as sleep:
            self.client._get_json("https://example.test/resource", None)
        sleep.assert_called_once_with(0.5)

    def test_transport_error_is_retried(self) -> None:
        with patch(
            "options_matrix.schwab_client.urllib.request.urlopen",
            side_effect=[urllib.error.URLError("temporary network failure"), _UrlResponse()],
        ) as urlopen, patch("options_matrix.schwab_client._time.sleep"):
            self.client._get_json("https://example.test/resource", None)
        self.assertEqual(urlopen.call_count, 2)

    def test_transient_retries_stop_at_the_bound(self) -> None:
        with patch(
            "options_matrix.schwab_client.urllib.request.urlopen",
            side_effect=[self._http_error(503) for _index in range(4)],
        ) as urlopen, patch("options_matrix.schwab_client._time.sleep") as sleep:
            with self.assertRaises(SchwabRequestError):
                self.client._get_json("https://example.test/resource", None)
        self.assertEqual(urlopen.call_count, 4)
        self.assertEqual(sleep.call_count, 3)

    def test_401_refreshes_token_without_losing_retry_budget(self) -> None:
        with patch(
            "options_matrix.schwab_client.urllib.request.urlopen",
            side_effect=[self._http_error(401), _UrlResponse()],
        ):
            self.client._get_json("https://example.test/resource", None)
        self.assertEqual(self.tokens.invalidations, 1)
        self.assertEqual(self.tokens.access_calls, [False, True])

    def test_oauth_refresh_retries_transient_failure(self) -> None:
        manager = SchwabTokenManager(SchwabCredentials("key", "secret", "refresh"))
        with patch(
            "options_matrix.schwab_client.urllib.request.urlopen",
            side_effect=[
                self._http_error(503, retry_after="1"),
                _UrlResponse('{"access_token": "new-token", "expires_in": 1800}'),
            ],
        ), patch("options_matrix.schwab_client._time.sleep") as sleep:
            token = manager.access_token()
        self.assertEqual(token, "new-token")
        sleep.assert_called_once_with(1.0)


class RestServerTimeTests(unittest.TestCase):
    class _HeaderRestClient(SchwabRestClient):
        def __init__(self, headers: dict) -> None:
            super().__init__(SchwabTokenManager())
            self._headers = headers

        def _get(self, url, params):
            return "{}", self._headers

    def test_server_time_parses_http_date_header(self) -> None:
        client = self._HeaderRestClient({"Date": "Mon, 06 Jul 2026 14:30:00 GMT"})
        self.assertEqual(
            client.server_time(),
            datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc),
        )

    def test_server_time_falls_back_to_local_clock_without_header(self) -> None:
        client = self._HeaderRestClient({})
        before = datetime.now(timezone.utc)
        value = client.server_time()
        after = datetime.now(timezone.utc)
        self.assertTrue(before <= value <= after)


if __name__ == "__main__":
    unittest.main()
