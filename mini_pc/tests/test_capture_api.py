from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from options_matrix.app import (
    CachedMatrixTab,
    CachedMatrixWorkspace,
    OptionsMatrixApp,
    _StartRecordingResult,
    load_formula_settings,
    load_matrix_workspace_cache,
    save_formula_settings,
    save_matrix_workspace_cache,
)
from options_matrix.models import DEFAULT_FORMULA_SETTINGS, DEFAULT_SIGNAL_METHOD, RIGHT_CALL
from options_matrix.recorder import MarketSessionStatus
from options_matrix.storage import ParquetSnapshotRecorder, default_recording_root
from options_matrix.studio_settings import StudioSettings, load_studio_settings


class _Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


@contextmanager
def _headless_app(**kwargs):
    """Run the real constructor without a Tk interpreter or broker worker."""
    with ExitStack() as stack:
        stack.enter_context(patch("options_matrix.app.tk.Tk", return_value=Mock()))
        stack.enter_context(patch("options_matrix.app.tk.StringVar", _Value))
        stack.enter_context(patch("options_matrix.app.tk.BooleanVar", _Value))
        stack.enter_context(patch("options_matrix.app.tkfont.Font", return_value=object()))
        stack.enter_context(patch("options_matrix.app.SchwabMarketDataService", return_value=SimpleNamespace(connected=False, quote_generation=0)))
        for method in ("_build_ui", "_load_cached_workspace_as_stale_tabs", "_set_connected_state"):
            stack.enter_context(patch.object(OptionsMatrixApp, method))
        yield OptionsMatrixApp(**kwargs)


def _capture_app(output_root: Path | None = None) -> OptionsMatrixApp:
    app = OptionsMatrixApp.__new__(OptionsMatrixApp)
    app.root = Mock()
    app.service = SimpleNamespace(connected=True, disconnect=Mock())
    app.matrix_tabs = [SimpleNamespace(is_stale=False)]
    app.studio_settings = StudioSettings(recording_folder=str(output_root) if output_root else "")
    app.recorder = None
    app._shutdown_in_progress = False
    app._shutdown_finalized = False
    app._close_after_recorder_stop = False
    app._recorder_after_id = None
    app._recorder_stop_inflight = False
    app._recorder_start_inflight = False
    app._recorder_snapshot_inflight = False
    app._recorder_armed = False
    app._recorder_error_message = None
    app._recorder_market_status_message = "NYSE market is open."
    app.recorder_status_var = _Value("Recorder idle.")
    app.recorder_indicator_var = _Value()
    app._set_status = Mock()
    app._recorder_subscribed_spread_count = Mock(return_value=2)
    return app


def _open_session() -> MarketSessionStatus:
    now = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)
    return MarketSessionStatus(now_utc=now, reason="open", is_open=True,
                               market_open_utc=now, market_close_utc=now,
                               message="NYSE market is open until 4:00 PM ET.")


def _row(session_id: str) -> dict:
    return {"snapshot_id": "s1", "session_id": session_id,
            "timestamp_utc": datetime(2026, 10, 13, 15, 0, 1, tzinfo=timezone.utc),
            "ticker": "SPY", "expiration": "20261016", "option_type/right": "CALL",
            "lower_strike": 600.0, "higher_strike": 601.0}


def _wait_for_stop(app: OptionsMatrixApp, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while app.recorder is not None and time.monotonic() < deadline:
        app._poll_recorder_stop()
        time.sleep(0.05)


class CaptureApiTestCase(unittest.TestCase):
    def test_start_requires_open_app_broker_and_live_matrix(self):
        cases = (
            ("_shutdown_in_progress", True, "closing"),
            ("service", SimpleNamespace(connected=False), "Connect Options Matrix"),
            ("matrix_tabs", [SimpleNamespace(is_stale=True)], "live matrix"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                app = _capture_app()
                setattr(app, field, value)
                app._start_recording = Mock()
                with self.assertRaisesRegex(RuntimeError, message):
                    app.start_capture()
                app._start_recording.assert_not_called()
                self.assertIn(message, app.recorder_status_var.get())
                self.assertEqual(app.capture_status()["state"], "Error")
                self.assertEqual(app.recorder_indicator_var.get(), "! REC ERROR")

    def test_start_delegates_after_preconditions_and_propagates_failure(self):
        app = _capture_app()
        app._recorder_error_message = "Previous error"
        app._start_recording = Mock()
        app.start_capture()
        app._start_recording.assert_called_once_with()
        self.assertIsNone(app._recorder_error_message)

        app._start_recording.side_effect = RuntimeError("Disk is not available.")
        with self.assertRaisesRegex(RuntimeError, "Disk"):
            app.start_capture()
        self.assertEqual(app.capture_status()["error"], "Disk is not available.")

    def test_capture_health_reports_lifecycle_and_error_precedence(self):
        app = _capture_app()
        cases = ((None, "Idle"), ("_recorder_start_inflight", "Starting"),
                 ("_recorder_armed", "Armed"), ("_recorder_stop_inflight", "Stopping"))
        for flag, expected in cases:
            with self.subTest(state=expected):
                for field in ("_recorder_start_inflight", "_recorder_armed", "_recorder_stop_inflight"):
                    setattr(app, field, field == flag)
                health = app.capture_status()
                self.assertEqual(health["state"], expected)
                self.assertEqual(health["live_tabs"], 1)
                self.assertEqual(health["spread_count"], 2)
                self.assertTrue(health["broker_connected"])
                self.assertEqual(app.capture_active(), flag is not None)
        app._recorder_stop_inflight = False
        app.recorder = SimpleNamespace(running=True, status=lambda: SimpleNamespace(error=None))
        self.assertEqual(app.capture_status()["state"], "Recording")
        app._recorder_error_message = "Capture failed"
        self.assertEqual(app.capture_status()["state"], "Error")

    def test_recording_folder_defaults_to_project_recordings_or_uses_setting(self):
        app = _capture_app()
        self.assertEqual(app._recording_output_root(), default_recording_root())
        self.assertEqual(default_recording_root().name, "recordings")
        app.studio_settings = StudioSettings(recording_folder="D:/Market data")
        self.assertEqual(app._recording_output_root(), Path("D:/Market data"))

    def test_start_task_accepts_a_new_folder_and_rejects_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            app = _capture_app()
            app.service = SimpleNamespace(
                connected=True,
                request_current_time=lambda: datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc),
            )
            app._build_market_session_guard = Mock(return_value=SimpleNamespace(status_at=lambda _now: _open_session()))
            result = app._start_recording_task(Path(directory) / "new" / "recordings", "SPY")
            self.assertFalse(result.armed)
            blocker = Path(directory) / "file.txt"
            blocker.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "recording folder"):
                app._start_recording_task(blocker, "SPY")

    def test_open_market_session_writes_parquet_locally_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "recordings"
            output.mkdir()
            app = _capture_app(output)
            app._recorder_start_inflight = True
            app._finish_start_recording(_StartRecordingResult(
                output_root=output, ticker_label="SPY", market_session_guard=Mock(),
                market_now_utc=datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc),
                local_now_utc=datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc),
                session_status=_open_session(),
            ))
            recorder = app.recorder
            self.assertIsInstance(recorder, ParquetSnapshotRecorder)
            self.assertTrue(recorder.running)
            self.assertEqual(app.capture_status()["state"], "Recording")

            from options_matrix.app import _RecordSnapshotResult
            app._finish_record_snapshot(_RecordSnapshotResult(
                recorder=recorder, session_id=recorder.session_id,
                captured_at=datetime(2026, 10, 13, 15, 0, 1, tzinfo=timezone.utc),
                rows=[_row(recorder.session_id)],
            ))
            with patch("options_matrix.app.messagebox"):
                app.stop_capture()
                _wait_for_stop(app)
            self.assertIsNone(app.recorder)
            self.assertEqual(app.capture_status()["state"], "Idle")
            parts = list(output.rglob("part-*.parquet"))
            self.assertEqual(len(parts), 1)
            self.assertFalse(list(output.rglob("*.inprogress")))
            self.assertIn("__SPY__", parts[0].parent.name)

    def test_constructor_has_no_writer_and_reads_studio_settings_from_its_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "studio.json").write_text(
                '{"version": 1, "record_every_trading_day": true, "share_port": 50123}',
                encoding="utf-8",
            )
            with patch("options_matrix.app.load_formula_settings", return_value=DEFAULT_FORMULA_SETTINGS), \
                 patch("options_matrix.app.load_signal_method", return_value=DEFAULT_SIGNAL_METHOD), \
                 patch("options_matrix.app.load_matrix_workspace_cache", return_value=None):
                with _headless_app(settings_root=root) as app:
                    self.assertIsNone(app.recorder)
                    self.assertIsNone(app.services)
                    self.assertTrue(app.studio_settings.record_every_trading_day)
                    self.assertEqual(app.studio_settings.share_port, 50123)
                    self.assertTrue(app.record_daily_var.get())

    def test_setting_changes_are_saved_to_studio_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with _headless_app(settings_root=root) as app:
                self.assertTrue(app._update_studio_settings(record_every_trading_day=True))
            self.assertTrue(load_studio_settings(root / "studio.json").record_every_trading_day)

    def test_demo_mode_keeps_its_own_saved_tabs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with _headless_app(settings_root=root, demo=True) as app:
                self.assertEqual(app._workspace_cache_path, root / "demo_matrix_cache.json")
                self.assertTrue(app._demo_mode)

    def test_explicit_settings_root_loads_and_saves_without_touching_default_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            default_path = root / "default.json"
            studio = root / "studio"
            studio_path = studio / "settings.json"
            default = replace(DEFAULT_FORMULA_SETTINGS, fair_value_edge_width_rate=0.11)
            initial = replace(DEFAULT_FORMULA_SETTINGS, fair_value_edge_width_rate=0.12)
            updated = replace(DEFAULT_FORMULA_SETTINGS, fair_value_edge_width_rate=0.13)
            save_formula_settings(default, default_path)
            save_formula_settings(initial, studio_path)
            before = default_path.read_bytes()
            with patch("options_matrix.app.formula_settings_config_path", return_value=default_path), \
                 _headless_app(settings_root=studio) as app:
                self.assertEqual(app.formula_settings, initial)
                self.assertEqual(app._settings_path, studio_path)
                self.assertEqual(app._workspace_cache_path, studio / "matrix_cache.json")
                for method in ("_sync_view_from_active_tab", "_redraw_all", "_refresh_staged_market_data_quote_status"):
                    setattr(app, method, Mock())
                self.assertTrue(app._apply_formula_settings(updated, save=True))
            self.assertEqual(load_formula_settings(studio_path), updated)
            self.assertEqual(default_path.read_bytes(), before)

    def test_explicit_workspace_path_load_save_and_clear_preserve_default_cache(self):
        workspace = CachedMatrixWorkspace(
            ticker_symbol="SPY",
            active_tab_identity=("SPY", "20300117", RIGHT_CALL),
            tabs=(CachedMatrixTab(raw_expiration="20300117", side=RIGHT_CALL,
                                  low_strike=100.0, high_strike=105.0,
                                  widths=(1.0,), selected_cell_key=(100.0, 1.0),
                                  label="SPY calls", row_strikes=(100.0, 101.0)),),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            default_path = root / "default-cache.json"
            studio = root / "studio"
            studio_path = studio / "matrix_cache.json"
            save_matrix_workspace_cache(workspace, default_path)
            save_matrix_workspace_cache(workspace, studio_path)
            before = default_path.read_bytes()
            with patch("options_matrix.app.matrix_workspace_cache_path", return_value=default_path), \
                 _headless_app(settings_root=studio) as app:
                self.assertEqual(app.cached_matrix_workspace, workspace)
                changed = replace(workspace, active_tab_identity=None)
                with patch("options_matrix.app.cached_matrix_workspace_from_tabs", return_value=changed):
                    app._save_current_matrix_cache()
                self.assertEqual(load_matrix_workspace_cache(studio_path), changed)
                app._clear_cached_matrix_workspace()
                self.assertIsNone(app.cached_matrix_workspace)
                self.assertFalse(studio_path.exists())
            self.assertEqual(default_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
