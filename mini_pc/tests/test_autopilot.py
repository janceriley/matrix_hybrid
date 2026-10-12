from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from options_matrix.app import MatrixInputState
from options_matrix.studio_services import CONNECT_BACKOFF_SECONDS, RELOAD_MAX_FAILURES, Autopilot
from options_matrix.studio_settings import StudioSettings


def _tab(tab_id: int, expiration: str, stale: bool) -> SimpleNamespace:
    matrix_input = MatrixInputState(
        display_expiration=expiration, raw_expiration=expiration, side="CALL",
        low_strike=600.0, high_strike=605.0, widths=(1.0,), ticker_symbol="SPY",
    )
    return SimpleNamespace(tab_id=tab_id, label=f"tab {tab_id}", matrix_input=matrix_input, is_stale=stale)


def _session(reason: str) -> SimpleNamespace:
    return SimpleNamespace(is_open=reason == "open", reason=reason)


def _app(**settings) -> SimpleNamespace:
    app = SimpleNamespace(
        studio_settings=StudioSettings(
            connect_at_startup=settings.get("connect", False),
            reload_tabs_after_connect=settings.get("reload", False),
            record_every_trading_day=settings.get("record", False),
        ),
        service=SimpleNamespace(connected=False),
        _shutdown_in_progress=False,
        _connect_inflight=False,
        _disconnect_inflight=False,
        _connection_active=False,
        _connection_status_message="",
        _restore_workspace_inflight=False,
        matrix_tabs=[],
        active_tab_id=None,
        recorder=None,
        _recorder_error_message=None,
        _connect=Mock(),
        _disconnect=Mock(),
        _set_status=Mock(),
        _activate_matrix_tab=Mock(),
        _reload_active_matrix_tab=Mock(),
        start_capture=Mock(),
    )
    app.capture_active = Mock(return_value=False)
    app._has_live_matrix_tabs = Mock(return_value=True)
    app._build_market_session_guard = Mock(return_value=SimpleNamespace(status_at=lambda _now: _session("open")))
    return app


class AutopilotTests(unittest.TestCase):
    def test_does_nothing_when_switched_off(self):
        app = _app()
        pilot = Autopilot(app)
        pilot.tick(now=0)
        self.assertEqual(pilot.status_text, "off")
        app._connect.assert_not_called()
        app.start_capture.assert_not_called()

    def test_connects_with_growing_waits_between_attempts(self):
        app = _app(connect=True)
        pilot = Autopilot(app)
        pilot.tick(now=0)
        app._connect.assert_called_once()
        pilot.tick(now=CONNECT_BACKOFF_SECONDS[0] - 1)
        self.assertEqual(app._connect.call_count, 1)
        pilot.tick(now=CONNECT_BACKOFF_SECONDS[0] + 1)
        self.assertEqual(app._connect.call_count, 2)
        pilot.tick(now=CONNECT_BACKOFF_SECONDS[0] + CONNECT_BACKOFF_SECONDS[1])
        self.assertEqual(app._connect.call_count, 2)
        app.service.connected = True
        pilot.tick(now=1000)
        self.assertEqual(pilot._connect_failures, 0)

    def test_waits_while_schwab_reconnects_and_restarts_after_it_gives_up(self):
        app = _app(connect=True)
        app._connection_active = True
        pilot = Autopilot(app)
        pilot.tick(now=0)
        app._connect.assert_not_called()
        app._disconnect.assert_not_called()
        app._connection_status_message = "Schwab streamer reconnect attempts exhausted. Use DISCONNECT/CONNECT to retry."
        pilot.tick(now=5)
        app._disconnect.assert_called_once()

    def test_reloads_stale_tabs_one_at_a_time_and_gives_up_on_broken_ones(self):
        app = _app(reload=True)
        app.service.connected = True
        app.matrix_tabs = [_tab(1, "20261016", stale=False), _tab(2, "20261023", stale=True)]
        app.active_tab_id = 1
        pilot = Autopilot(app)
        pilot.tick(now=0)
        app._activate_matrix_tab.assert_called_once_with(2)
        app._reload_active_matrix_tab.assert_called_once()
        for _ in range(RELOAD_MAX_FAILURES + 2):
            pilot.tick(now=0)
        self.assertEqual(app._reload_active_matrix_tab.call_count, RELOAD_MAX_FAILURES)

    def test_starts_recording_on_a_trading_day_and_retries_after_a_wait(self):
        app = _app(record=True)
        app.service.connected = True
        pilot = Autopilot(app)
        pilot.tick(now=0)
        app.start_capture.assert_called_once()
        pilot.tick(now=10)
        app.start_capture.assert_called_once()
        pilot.tick(now=500)
        self.assertEqual(app.start_capture.call_count, 2)

    def test_arms_before_the_open_but_not_after_the_close(self):
        for reason, expected in (("pre_open", 1), ("after_close", 0), ("no_session", 0)):
            with self.subTest(reason=reason):
                app = _app(record=True)
                app.service.connected = True
                app._build_market_session_guard.return_value = SimpleNamespace(status_at=lambda _now, r=reason: _session(r))
                Autopilot(app).tick(now=0)
                self.assertEqual(app.start_capture.call_count, expected)

    def test_recording_waits_for_data_and_a_live_tab(self):
        app = _app(record=True)
        pilot = Autopilot(app)
        pilot.tick(now=0)
        app.start_capture.assert_not_called()
        app.service.connected = True
        app._has_live_matrix_tabs.return_value = False
        pilot.tick(now=0)
        app.start_capture.assert_not_called()
        self.assertIn("live tab", pilot.status_text)

    def test_manual_stop_is_respected_for_the_rest_of_the_day(self):
        app = _app(record=True)
        app.service.connected = True
        pilot = Autopilot(app)
        pilot.user_stopped_recording()
        pilot.tick(now=0)
        app.start_capture.assert_not_called()
        self.assertIn("stopped by you", pilot.status_text)
        later = datetime(2099, 1, 2, 15, 0, tzinfo=timezone.utc)
        with patch("options_matrix.studio_services.datetime") as clock:
            clock.now.return_value = later
            pilot.tick(now=1000)
        app.start_capture.assert_called_once()

    def test_start_errors_are_reported_without_raising(self):
        app = _app(record=True)
        app.service.connected = True
        app.start_capture.side_effect = RuntimeError("Disk is full.")
        pilot = Autopilot(app)
        pilot.tick(now=0)
        self.assertIn("Disk is full", pilot.status_text)


if __name__ == "__main__":
    unittest.main()
