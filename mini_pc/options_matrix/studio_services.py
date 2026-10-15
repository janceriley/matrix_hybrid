"""Mini PC background services: sharing with viewers, and the autopilot.

Everything here runs on the Tk thread through ``root.after`` ticks, reading
the app's state and handing it to the share hub. The hub and server do the
network work on their own threads.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

from .models import QUOTE_KIND_UNDERLYING, QuoteSnapshot
from .one_desk import SHARING_GLYPH
from .recorder import MARKET_TIMEZONE
from .share_protocol import (
    PROTOCOL_VERSION,
    encode_diagnostic,
    encode_matrix_input,
    encode_records,
    encode_search_result,
    math_fingerprint,
    records_key,
)
from .share_server import ShareHub, ShareServer, ViewerInfo
from .studio_settings import generate_share_password, valid_share_password

if TYPE_CHECKING:
    from .app import OptionsMatrixApp

LOG = logging.getLogger(__name__)

# Advanced: listen on one address only, e.g. a Tailscale address or 127.0.0.1.
SHARE_HOST_ENVIRONMENT_VARIABLE = "OPTIONS_MATRIX_SHARE_HOST"

QUOTE_FLUSH_MS = 100
STATE_CHECK_MS = 250
HEALTH_MS = 1000
AUTOPILOT_MS = 5000
CONNECT_BACKOFF_SECONDS = (15, 30, 60, 120, 300)
RECORD_RETRY_SECONDS = 120
RECORD_ERROR_RETRY_SECONDS = 300
RELOAD_MAX_FAILURES = 3


def local_ipv4_addresses() -> list[str]:
    """This PC's IPv4 addresses, the one used for normal traffic first."""
    addresses: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Connecting a UDP socket sends nothing; it only picks the outgoing route.
        probe.connect(("192.0.2.1", 9))
        addresses.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in addresses:
                addresses.append(address)
    except OSError:
        pass
    return [
        address
        for address in addresses
        if not address.startswith("127.") and not address.startswith("169.254.") and address != "0.0.0.0"
    ]


class StudioServices:
    # Listen on every network connection so the work PC can reach this PC.
    # Tests set ``app.share_bind_host = "127.0.0.1"`` to stay on this PC.
    default_bind_host = "0.0.0.0"

    def __init__(self, app: OptionsMatrixApp) -> None:
        self.app = app
        self.hub = ShareHub()
        self.server: ShareServer | None = None
        self.share_error: str | None = None
        self.autopilot = Autopilot(app)
        self._after_ids: dict[str, str] = {}
        self._pending_quotes: dict[tuple[str, Any], QuoteSnapshot] = {}
        self._tabs_signature: object = None
        self._settings_signature: object = None
        self._fingerprint = math_fingerprint()
        self._started = False
        # Set by server threads; read on the Tk thread (never call Tk from them).
        self._viewers_changed = False

    # Lifecycle -------------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._ensure_password()
        self._apply_share_settings()
        self._schedule("quotes", QUOTE_FLUSH_MS, QUOTE_FLUSH_MS, self._flush_quotes)
        self._schedule("state", STATE_CHECK_MS, STATE_CHECK_MS, self._check_state)
        self._schedule("health", HEALTH_MS, HEALTH_MS, self._publish_health)
        self._schedule("autopilot", 1000, AUTOPILOT_MS, self._autopilot_tick)

    def stop(self) -> None:
        self._started = False
        for after_id in self._after_ids.values():
            try:
                self.app.root.after_cancel(after_id)
            except Exception:
                pass
        self._after_ids.clear()
        self._stop_server()

    def settings_changed(self) -> None:
        if self._started:
            self._apply_share_settings()
            self.autopilot.settings_changed()

    # Hooks called by the app ----------------------------------------------
    def on_quote(self, quote: QuoteSnapshot) -> None:
        if quote.kind == QUOTE_KIND_UNDERLYING:
            key: tuple[str, Any] = (quote.kind, str(quote.label).upper())
        else:
            key = (quote.kind, quote.contract_con_id)
        self._pending_quotes[key] = quote

    def on_diagnostics(self, diagnostics: Iterable[Any]) -> None:
        self.hub.set_diagnostics({
            "type": "diagnostics",
            "items": [
                encode_diagnostic(diagnostic)
                for diagnostic in diagnostics
                if getattr(diagnostic, "contract_con_id", None) is not None
            ],
        })

    def user_stopped_recording(self) -> None:
        self.autopilot.user_stopped_recording()

    # Sharing ---------------------------------------------------------------
    def _ensure_password(self) -> None:
        if not valid_share_password(self.app.studio_settings.share_password):
            self.app._update_studio_settings(share_password=generate_share_password())

    def _apply_share_settings(self) -> None:
        settings = self.app.studio_settings
        server = self.server
        if not settings.share_enabled:
            self._stop_server()
            self.share_error = None
            return
        if server is not None and server.running and server.port == settings.share_port:
            if server._password != settings.share_password:
                server.set_password(settings.share_password)
                self.hub.disconnect_all()
            return
        self._stop_server()
        server = ShareServer(
            self.hub,
            host=str(
                getattr(self.app, "share_bind_host", None)
                or os.environ.get(SHARE_HOST_ENVIRONMENT_VARIABLE, "").strip()
                or self.default_bind_host
            ),
            port=settings.share_port,
            password=settings.share_password,
            welcome={
                "studio_name": socket.gethostname(),
                "fingerprint": self._fingerprint,
                "demo": bool(getattr(self.app, "_demo_mode", False)),
            },
            on_viewers_changed=self._viewers_changed_threadsafe,
        )
        try:
            server.start()
        except OSError as exc:
            self.share_error = (
                f"Sharing could not start on port {settings.share_port}: {exc}. "
                "Choose another port in Mini PC settings."
            )
            LOG.warning(self.share_error)
            self.app._set_status(self.share_error)
            self.server = None
        else:
            self.server = server
            self.share_error = None
            # Make sure a new server hands viewers the full current state.
            self._tabs_signature = None
            self._settings_signature = None
            self._check_state()
        self._refresh_share_indicator()

    def _stop_server(self) -> None:
        server = self.server
        self.server = None
        if server is not None:
            server.stop()
        self._refresh_share_indicator()

    def regenerate_password(self) -> str:
        password = generate_share_password()
        self.app._update_studio_settings(share_password=password)
        return password

    def viewers(self) -> list[ViewerInfo]:
        return self.hub.viewers()

    def share_status_text(self) -> str:
        settings = self.app.studio_settings
        if not settings.share_enabled:
            return "Sharing is off."
        if self.share_error:
            return self.share_error
        server = self.server
        if server is None or not server.running:
            return "Sharing is starting..."
        viewers = self.viewers()
        if not viewers:
            return f"Sharing on port {server.port}. No viewers connected."
        names = ", ".join(viewer.name or viewer.address.split(":")[0] for viewer in viewers)
        noun = "viewer" if len(viewers) == 1 else "viewers"
        return f"Sharing on port {server.port}. {len(viewers)} {noun} connected: {names}."

    def share_indicator_text(self) -> str:
        if not self.app.studio_settings.share_enabled:
            return f"{SHARING_GLYPH} SHARING OFF"
        if self.share_error or self.server is None:
            return f"{SHARING_GLYPH} SHARING ERROR"
        count = len(self.viewers())
        return f"{SHARING_GLYPH} {count} VIEWER" if count == 1 else f"{SHARING_GLYPH} {count} VIEWERS"

    def _viewers_changed_threadsafe(self) -> None:
        self._viewers_changed = True

    def _refresh_share_indicator(self) -> None:
        variable = getattr(self.app, "share_indicator_var", None)
        if variable is not None:
            variable.set(self.share_indicator_text())

    # Publishing ------------------------------------------------------------
    def _schedule(self, name: str, delay_ms: int, repeat_ms: int, callback: Any) -> None:
        """Run ``callback`` after ``delay_ms`` and then every ``repeat_ms``."""
        if not self._started:
            return

        def run() -> None:
            self._after_ids.pop(name, None)
            if not self._started:
                return
            try:
                callback()
            except Exception:
                LOG.exception("Mini PC service %s failed", name)
            self._schedule(name, repeat_ms, repeat_ms, callback)

        self._after_ids[name] = self.app.root.after(delay_ms, run)

    def _flush_quotes(self) -> None:
        if self._viewers_changed:
            self._viewers_changed = False
            self._refresh_share_indicator()
        if not self._pending_quotes:
            return
        quotes = list(self._pending_quotes.values())
        self._pending_quotes.clear()
        self.hub.update_quotes(quotes)

    def _check_state(self) -> None:
        app = self.app
        settings_signature = (app.formula_settings, app.signal_method)
        if settings_signature != self._settings_signature:
            self._settings_signature = settings_signature
            from .app import formula_settings_to_dict

            self.hub.set_settings({
                "type": "settings",
                "formula": formula_settings_to_dict(app.formula_settings),
                "signal_method": app.signal_method,
            })
        tabs_signature = self._current_tabs_signature()
        if tabs_signature != self._tabs_signature:
            self._tabs_signature = tabs_signature
            message, keep_ids, underlying = self._tabs_message()
            self.hub.set_tabs(message, keep_con_ids=keep_ids, keep_underlying=underlying)
            # Quotes for newly shared tabs must reach the viewers too.
            for con_id in keep_ids:
                quote = app.leg_quote_cache.get(con_id)
                if quote is not None:
                    self.on_quote(quote)
            if app.underlying_quote is not None:
                self.on_quote(app.underlying_quote)

    def _current_tabs_signature(self) -> object:
        app = self.app
        search = app.search_result
        return (
            None if search is None else (search.underlying.symbol, search.underlying.con_id),
            tuple(
                (
                    tab.tab_id,
                    tab.matrix_input,
                    bool(tab.is_stale),
                    tab.underlying_price_hint,
                    tab.leg_quote_ids,
                    (tab.matrix_input.ticker_symbol, tab.matrix_input.raw_expiration)
                    in app.option_records_by_expiration,
                )
                for tab in app.matrix_tabs
            ),
        )

    def _tabs_message(self) -> tuple[dict[str, Any], set[int], str | None]:
        app = self.app
        tabs: list[dict[str, Any]] = []
        records: dict[str, Any] = {}
        keep_ids: set[int] = set()
        for tab in app.matrix_tabs:
            symbol = tab.matrix_input.ticker_symbol
            expiration = tab.matrix_input.raw_expiration
            cached = app.option_records_by_expiration.get((symbol, expiration))
            key = records_key(symbol, expiration)
            if cached is not None and key not in records:
                records[key] = encode_records(cached)
            tabs.append({
                "id": tab.tab_id,
                "label": tab.label,
                "input": encode_matrix_input(tab.matrix_input),
                "stale": bool(tab.is_stale),
                "hint": tab.underlying_price_hint,
                "records": key if cached is not None else None,
            })
            keep_ids.update(tab.leg_quote_ids)
        search = app.search_result
        message = {
            "type": "tabs",
            "tabs": tabs,
            "records": records,
            "search": encode_search_result(search),
            "active": app.active_tab_id,
        }
        return message, keep_ids, None if search is None else search.underlying.symbol

    def _publish_health(self) -> None:
        self.hub.set_health(self.health_message())

    def health_message(self) -> dict[str, Any]:
        app = self.app
        recording = app.capture_status()
        recorder = getattr(app, "recorder", None)
        status = recorder.status() if recorder is not None else None
        return {
            "type": "health",
            "protocol": PROTOCOL_VERSION,
            "schwab": {
                "connected": bool(getattr(app.service, "connected", False)),
                "active": bool(getattr(app, "_connection_active", False)),
                "badge": str(app.connection_var.get()),
                "message": str(getattr(app, "_connection_status_message", "")),
            },
            "auth": {
                "badge": str(app.auth_status_var.get()),
                "detail": str(getattr(app, "_auth_status_detail", "")),
            },
            "recording": {
                "state": recording.get("state"),
                "error": recording.get("error"),
                "rows_written": int(status.rows_written) if status is not None else 0,
                "market": recording.get("market_status") or "",
                "indicator": str(app.recorder_indicator_var.get()),
            },
            "autopilot": self.autopilot.status_text,
            "demo": bool(getattr(app, "_demo_mode", False)),
            "studio_status": str(app.status_var.get()),
        }

    def _autopilot_tick(self) -> None:
        self.autopilot.tick()


class Autopilot:
    """Keeps the mini PC connected, its tabs live and recording each trading day."""

    def __init__(self, app: OptionsMatrixApp) -> None:
        self.app = app
        self.status_text = "off"
        self._next_connect_at = 0.0
        self._connect_failures = 0
        self._reload_failures: dict[tuple[str, str, str], int] = {}
        self._reloading_identity: tuple[str, str, str] | None = None
        self._next_record_attempt_at = 0.0
        self._suppressed_market_date: Any = None
        self._session_guard: Any = None
        self._first_tick = True

    def settings_changed(self) -> None:
        self._next_connect_at = 0.0
        self._next_record_attempt_at = 0.0

    def user_stopped_recording(self) -> None:
        """A manual Stop is respected for the rest of that trading day."""
        self._suppressed_market_date = datetime.now(timezone.utc).astimezone(MARKET_TIMEZONE).date()

    def _guard(self) -> Any:
        if self._session_guard is None:
            self._session_guard = self.app._build_market_session_guard()
        return self._session_guard

    def tick(self, *, now: float | None = None) -> None:
        app = self.app
        settings = app.studio_settings
        monotonic_now = time.monotonic() if now is None else now
        if getattr(app, "_shutdown_in_progress", False):
            return
        if not (settings.connect_at_startup or settings.reload_tabs_after_connect or settings.record_every_trading_day):
            self.status_text = "off"
            return
        messages: list[str] = []
        if settings.connect_at_startup:
            messages.append(self._keep_connected(monotonic_now))
        connected = bool(getattr(app.service, "connected", False))
        if connected and settings.reload_tabs_after_connect:
            reload_message = self._reload_stale_tabs()
            if reload_message:
                messages.append(reload_message)
        if settings.record_every_trading_day:
            messages.append(self._keep_recording(monotonic_now, connected))
        self.status_text = " ".join(message for message in messages if message) or "nothing to do right now."
        self._first_tick = False

    def _keep_connected(self, now: float) -> str:
        app = self.app
        service = app.service
        if bool(getattr(service, "connected", False)):
            self._connect_failures = 0
            self._next_connect_at = 0.0
            return "Connected to market data."
        if getattr(app, "_connect_inflight", False) or getattr(app, "_disconnect_inflight", False):
            return "Connecting..."
        if getattr(app, "_connection_active", False):
            message = str(getattr(app, "_connection_status_message", "")).lower()
            if "exhausted" in message:
                app._disconnect()
                return "Schwab stopped retrying; restarting the connection."
            return "Schwab is reconnecting by itself."
        if now < self._next_connect_at:
            return f"Next connection attempt in {int(self._next_connect_at - now)} s."
        delay = CONNECT_BACKOFF_SECONDS[min(self._connect_failures, len(CONNECT_BACKOFF_SECONDS) - 1)]
        self._connect_failures += 1
        self._next_connect_at = now + delay
        app._set_status("Autopilot: connecting to market data ...")
        app._connect()
        return "Connecting..."

    def _reload_stale_tabs(self) -> str:
        from .app import matrix_tab_identity

        app = self.app
        if getattr(app, "_restore_workspace_inflight", False):
            return "Reloading saved tabs..."
        if self._reloading_identity is not None:
            tab = next(
                (t for t in app.matrix_tabs if matrix_tab_identity(t.matrix_input) == self._reloading_identity),
                None,
            )
            if tab is not None and tab.is_stale:
                self._reload_failures[self._reloading_identity] = (
                    self._reload_failures.get(self._reloading_identity, 0) + 1
                )
            self._reloading_identity = None
        for tab in app.matrix_tabs:
            identity = matrix_tab_identity(tab.matrix_input)
            if not tab.is_stale or self._reload_failures.get(identity, 0) >= RELOAD_MAX_FAILURES:
                continue
            self._reloading_identity = identity
            if app.active_tab_id != tab.tab_id:
                app._activate_matrix_tab(tab.tab_id)
            app._reload_active_matrix_tab()
            return f"Reloading saved tab {tab.label}."
        return ""

    def _keep_recording(self, now: float, connected: bool) -> str:
        app = self.app
        if app.capture_active():
            return "Recording is on." if getattr(app, "recorder", None) is not None else "Recording is armed."
        if not connected:
            return "Recording waits for market data."
        if not app._has_live_matrix_tabs():
            return "Recording waits for a live tab."
        utc_now = datetime.now(timezone.utc)
        market_date = utc_now.astimezone(MARKET_TIMEZONE).date()
        if self._suppressed_market_date == market_date:
            return "Recording was stopped by you today."
        try:
            session = self._guard().status_at(utc_now)
        except Exception as exc:  # calendar data problems should not stop the app
            return f"Market calendar unavailable: {exc}"
        if not session.is_open and session.reason != "pre_open":
            return "Recording resumes on the next trading day."
        if now < self._next_record_attempt_at:
            return "Recording will retry shortly."
        error = getattr(app, "_recorder_error_message", None)
        self._next_record_attempt_at = now + (RECORD_ERROR_RETRY_SECONDS if error else RECORD_RETRY_SECONDS)
        try:
            app.start_capture()
        except RuntimeError as exc:
            return f"Recording could not start: {exc}"
        app._set_status("Autopilot: starting today's recording ...")
        return "Starting today's recording."
