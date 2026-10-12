"""Storage integration checks using real Parquet files and controlled slow writers."""

from datetime import datetime, timezone
from pathlib import Path
import threading
import time

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from options_matrix.storage import (
    SNAPSHOT_COLUMNS,
    ParquetSnapshotRecorder,
    RecorderBacklogError,
    RecorderStopTimeoutError,
    build_pyarrow_schema,
    default_recording_root,
)


CAPTURED_AT = datetime(2026, 9, 4, 15, 0, 1, 123456, tzinfo=timezone.utc)


def sample_rows(count=1, session_id="test-session"):
    return [
        {
            "snapshot_id": f"snapshot-{index}",
            "session_id": session_id,
            "timestamp_utc": CAPTURED_AT,
            "timestamp_local": CAPTURED_AT.isoformat(),
            "ticker": "SPY",
            "expiration": "20260911",
            "option_type/right": "C",
            "lower_strike": 620.0,
            "higher_strike": 625.0,
            "dte": 7,
            "lower_bid": 4.1,
            "lower_ask": 4.2,
            "market_data_type": 1,
            "bid_ask_timestamp_mismatch": True,
            "synthetic_spread_mid": 2.125 + index,
            "signal_method": "curve",
            "time_bucket": "11:00",
            "minute_from_open": 90,
            "session_period": "REGULAR",
            "unrecorded_extra_field": "must not change the schema",
        }
        for index in range(count)
    ]


def saved_rows(recorder):
    return [
        row
        for part in sorted(recorder.session_dir.glob("*.parquet"))
        for row in pq.read_table(part).to_pylist()
    ]


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Writer did not reach the expected state")
        time.sleep(0.01)


def test_parquet_roundtrip_preserves_original_42_columns_and_types(tmp_path):
    recorder = ParquetSnapshotRecorder(tmp_path, ticker_label="SPY")
    assert recorder.start(started_at=CAPTURED_AT, session_id="test-session") == "test-session"
    rows = sample_rows(3)
    recorder.record_rows(rows)
    recorder.stop()

    paths = list(recorder.session_dir.glob("*.parquet"))
    assert len(paths) == 1
    table = pq.read_table(paths[0])
    assert len(SNAPSHOT_COLUMNS) == 42
    assert table.schema.names == [
        "snapshot_id", "session_id", "timestamp_utc", "timestamp_local", "ticker",
        "expiration", "option_type/right", "lower_strike", "higher_strike", "dte",
        "underlying_price", "underlying_volume", "lower_bid", "lower_ask", "lower_mid",
        "higher_bid", "higher_ask", "higher_mid", "lower_bid_size", "lower_ask_size",
        "higher_bid_size", "higher_ask_size", "lower_volume", "higher_volume",
        "lower_open_interest", "higher_open_interest", "lower_implied_vol",
        "higher_implied_vol", "lower_model_option_price", "higher_model_option_price",
        "market_data_type", "quote_age_seconds", "bid_ask_timestamp_mismatch",
        "synthetic_spread_bid", "synthetic_spread_ask", "synthetic_spread_mid",
        "synthetic_spread_bid_size", "synthetic_spread_ask_size", "signal_method",
        "time_bucket", "minute_from_open", "session_period",
    ]
    assert table.schema.field("timestamp_utc").type == pa.timestamp("us", tz="UTC")
    assert table.schema.field("dte").type == pa.int64()
    assert table.schema.field("lower_bid").type == pa.float64()
    assert table.schema.field("bid_ask_timestamp_mismatch").type == pa.bool_()
    assert table.schema == build_pyarrow_schema()
    assert table.to_pylist() == [{column: row.get(column) for column in SNAPSHOT_COLUMNS} for row in rows]
    assert not list(recorder.session_dir.glob("*.inprogress"))
    status = recorder.status()
    assert status.rows_written == status.rows_enqueued == 3
    assert status.pending_rows == 0
    assert status.flush_complete and not status.writer_alive and not status.running


def test_rotation_splits_large_batch_without_losing_rows(tmp_path):
    recorder = ParquetSnapshotRecorder(tmp_path, max_rows_per_part=2, flush_rows=1)
    recorder.start(session_id="rotation")
    recorder.record_rows(sample_rows(5, "rotation"))
    recorder.stop()
    assert [pq.read_metadata(part).num_rows for part in sorted(recorder.session_dir.glob("*.parquet"))] == [2, 2, 1]
    assert [row["snapshot_id"] for row in saved_rows(recorder)] == [f"snapshot-{i}" for i in range(5)]


def test_row_backlog_rejects_new_batch_and_drains_accepted_rows(tmp_path):
    recorder = ParquetSnapshotRecorder(tmp_path, max_queue_rows=2, flush_rows=100, flush_seconds=30)
    recorder.start()
    recorder.record_rows(sample_rows(2))
    with pytest.raises(RecorderBacklogError, match="row capacity exceeded"):
        recorder.record_rows(sample_rows())
    recorder.request_stop()
    assert recorder.wait_for_stop(timeout=5, raise_on_error=False)
    assert len(saved_rows(recorder)) == 2
    assert recorder.status().rows_enqueued == recorder.status().rows_written == 2
    assert recorder.status().backlog_error
    with pytest.raises(RuntimeError, match="row capacity exceeded"):
        recorder.wait_for_stop()


def test_batch_backlog_rejects_only_unaccepted_batch(tmp_path):
    release_writer = threading.Event()

    class PausedRecorder(ParquetSnapshotRecorder):
        def _writer_loop(self, context):
            release_writer.wait(timeout=5)
            super()._writer_loop(context)

    recorder = PausedRecorder(tmp_path, max_queue_batches=1)
    try:
        recorder.start()
        recorder.record_rows(sample_rows(2))
        with pytest.raises(RecorderBacklogError, match="batch capacity exceeded"):
            recorder.record_rows(sample_rows())
        recorder.request_stop()
    finally:
        release_writer.set()
        recorder.request_stop()
        assert recorder.wait_for_stop(timeout=5, raise_on_error=False)
    assert len(saved_rows(recorder)) == 2
    assert recorder.status().pending_rows == 0


def test_stop_timeout_retains_session_until_buffer_drains(tmp_path):
    release_writer = threading.Event()

    class PausedRecorder(ParquetSnapshotRecorder):
        def _writer_loop(self, context):
            release_writer.wait(timeout=5)
            super()._writer_loop(context)

    recorder = PausedRecorder(tmp_path)
    try:
        first_session = recorder.start()
        first_directory = recorder.session_dir
        recorder.record_rows(sample_rows(2))
        with pytest.raises(RecorderStopTimeoutError, match="still draining"):
            recorder.stop(timeout=0)
        status = recorder.status()
        assert status.stopping and status.writer_alive and not status.flush_complete
        assert status.session_id == first_session
        assert status.pending_rows == 2
        with pytest.raises(RuntimeError, match="still stopping"):
            recorder.start()
        with pytest.raises(RuntimeError, match="not running"):
            recorder.record_rows(sample_rows())
    finally:
        release_writer.set()
        recorder.request_stop()
        assert recorder.wait_for_stop(timeout=5)
    assert len(saved_rows(recorder)) == 2
    second_session = recorder.start()
    recorder.stop()
    assert second_session != first_session
    assert recorder.session_dir != first_directory


def test_idle_buffer_flushes_on_deadline_and_finishes_on_stop(tmp_path):
    recorder = ParquetSnapshotRecorder(tmp_path, flush_seconds=1, flush_rows=100)
    recorder.start()
    try:
        recorder.record_rows(sample_rows())
        wait_until(lambda: recorder.status().rows_written == 1)
        assert recorder.current_part_path.suffix == ".inprogress"
    finally:
        recorder.stop()
    assert recorder.current_part_path.suffix == ".parquet"
    assert len(saved_rows(recorder)) == 1


def test_default_output_belongs_to_standalone_project():
    assert default_recording_root() == Path(__file__).resolve().parent.parent / "recordings"
