"""Buffered Parquet persistence for the built-in recorder.

Writes the same 42-column Parquet format the separate Matrix Recorder used:
one-second snapshot rows, buffered and flushed off the Tk thread, with parts
that rotate by time or row count. Column names, the session-status type and
the ticker-label rules come from ``recorder`` so there is one definition.
"""

from __future__ import annotations

import queue
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .recorder import (
    MARKET_TIMEZONE,
    SNAPSHOT_COLUMNS,
    RecorderStatus,
    normalize_recording_ticker_label,
)

_STRING_COLUMNS = {
    "snapshot_id",
    "session_id",
    "timestamp_local",
    "ticker",
    "expiration",
    "option_type/right",
    "signal_method",
    "time_bucket",
    "session_period",
}
_INT_COLUMNS = {"dte", "market_data_type", "minute_from_open"}
_BOOL_COLUMNS = {"bid_ask_timestamp_mismatch"}
_TIMESTAMP_COLUMNS = {"timestamp_utc"}

_UNKNOWN_RECORDING_TICKER_LABEL = "UNKNOWN"


class RecorderBacklogError(RuntimeError):
    """Raised when accepting another snapshot would exceed recorder capacity."""


class RecorderStopTimeoutError(RuntimeError):
    """Raised when a writer is still draining after the requested wait."""


@dataclass(frozen=True)
class _WriterSessionContext:
    session_id: str
    session_dir: Path
    work_queue: queue.Queue[list[dict[str, Any]]]
    stop_requested: threading.Event
    completed: threading.Event


def default_recording_root() -> Path:
    """Store recordings in this project's ``recordings`` folder by default."""
    return Path(__file__).resolve().parent.parent / "recordings"


def recording_session_folder_name(
    started_at: datetime,
    ticker_label: object,
    session_id: str,
) -> str:
    started_utc = _to_utc_datetime(started_at) or datetime.now(timezone.utc)
    market_time = started_utc.astimezone(MARKET_TIMEZONE)
    timestamp = market_time.strftime("%Y-%m-%d_%H-%M-%S")
    normalized_ticker = normalize_recording_ticker_label(ticker_label)
    session8 = re.sub(r"[^A-Za-z0-9]+", "", str(session_id or "")).lower()[:8]
    if not session8:
        session8 = uuid.uuid4().hex[:8]
    return f"{timestamp}_ET__{normalized_ticker}__{session8}"


def build_pyarrow_schema():
    pa, _pq = _require_pyarrow()
    fields = []
    for column in SNAPSHOT_COLUMNS:
        if column in _STRING_COLUMNS:
            field_type = pa.string()
        elif column in _INT_COLUMNS:
            field_type = pa.int64()
        elif column in _BOOL_COLUMNS:
            field_type = pa.bool_()
        elif column in _TIMESTAMP_COLUMNS:
            field_type = pa.timestamp("us", tz="UTC")
        else:
            field_type = pa.float64()
        fields.append(pa.field(column, field_type))
    return pa.schema(fields)


def _require_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ModuleNotFoundError as exc:
        if exc.name != "pyarrow":
            raise
        raise RuntimeError(
            "Missing dependency `pyarrow`. Install project dependencies with `pip install -e .` "
            "before starting the recorder."
        ) from exc
    return pa, pq


class ParquetSnapshotRecorder:
    def __init__(
        self,
        output_root: Path,
        *,
        ticker_label: str = _UNKNOWN_RECORDING_TICKER_LABEL,
        roll_seconds: int = 300,
        max_rows_per_part: int = 50_000,
        flush_seconds: int = 30,
        flush_rows: int = 5_000,
        max_queue_batches: int = 120,
        max_queue_rows: int = 100_000,
    ) -> None:
        self.output_root = Path(output_root)
        self.ticker_label = normalize_recording_ticker_label(ticker_label)
        self.roll_seconds = max(1, int(roll_seconds))
        self.max_rows_per_part = max(1, int(max_rows_per_part))
        self.flush_seconds = max(1, int(flush_seconds))
        self.flush_rows = max(1, int(flush_rows))
        self.max_queue_batches = max(1, int(max_queue_batches))
        self.max_queue_rows = max(1, int(max_queue_rows))
        self._session_id: str | None = None
        self._session_dir: Path | None = None
        self._writer_context: _WriterSessionContext | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._stopping = False
        self._part_index = 0
        self._current_part_path: Path | None = None
        self._rows_enqueued = 0
        self._rows_written = 0
        self._last_snapshot_rows = 0
        self._started_at: datetime | None = None
        self._last_write_at: datetime | None = None
        self._error: str | None = None
        self._backlog_error: str | None = None
        self._lock = threading.Lock()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def session_dir(self) -> Path | None:
        return self._session_dir

    @property
    def current_part_path(self) -> Path | None:
        with self._lock:
            return self._current_part_path

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def status(self) -> RecorderStatus:
        with self._lock:
            context = self._writer_context
            thread = self._thread
            return RecorderStatus(
                running=self._running,
                session_id=self._session_id,
                session_dir=self._session_dir,
                current_part_path=self._current_part_path,
                rows_enqueued=self._rows_enqueued,
                rows_written=self._rows_written,
                last_snapshot_rows=self._last_snapshot_rows,
                started_at=self._started_at,
                last_write_at=self._last_write_at,
                error=self._error,
                stopping=self._stopping,
                writer_alive=thread is not None and thread.is_alive(),
                flush_complete=context is None or context.completed.is_set(),
                backlog_batches=context.work_queue.qsize() if context is not None else 0,
                backlog_capacity_batches=self.max_queue_batches,
                backlog_capacity_rows=self.max_queue_rows,
                backlog_error=self._backlog_error,
            )

    def start(
        self,
        *,
        started_at: datetime | None = None,
        session_id: str | None = None,
    ) -> str:
        with self._lock:
            prior_context = self._writer_context
            prior_thread = self._thread
            if self._running:
                raise RuntimeError("Recorder is already running.")
            if (
                self._stopping
                or (prior_context is not None and not prior_context.completed.is_set())
                or (prior_thread is not None and prior_thread.is_alive())
            ):
                raise RuntimeError("Previous recorder writer is still stopping.")
        _require_pyarrow()
        self.output_root.mkdir(parents=True, exist_ok=True)
        started_at_utc = _to_utc_datetime(started_at) or datetime.now(timezone.utc)
        new_session_id = session_id or str(uuid.uuid4())
        folder_name = recording_session_folder_name(started_at_utc, self.ticker_label, new_session_id)
        session_dir = self.output_root / folder_name
        session_dir.mkdir(parents=False, exist_ok=False)
        context = _WriterSessionContext(
            session_id=new_session_id,
            session_dir=session_dir,
            work_queue=queue.Queue(maxsize=self.max_queue_batches),
            stop_requested=threading.Event(),
            completed=threading.Event(),
        )
        thread = threading.Thread(
            target=self._writer_loop,
            args=(context,),
            name="MatrixRecorderParquetWriter",
            daemon=True,
        )
        with self._lock:
            self._session_id = new_session_id
            self._session_dir = session_dir
            self._writer_context = context
            self._thread = thread
            self._running = True
            self._stopping = False
            self._part_index = 0
            self._current_part_path = None
            self._rows_enqueued = 0
            self._rows_written = 0
            self._last_snapshot_rows = 0
            self._started_at = started_at_utc
            self._last_write_at = None
            self._error = None
            self._backlog_error = None
        thread.start()
        return new_session_id

    def record_rows(self, rows: Sequence[Mapping[str, Any]]) -> None:
        normalized_rows = [
            {column: row.get(column) for column in SNAPSHOT_COLUMNS}
            for row in rows
        ]
        with self._lock:
            self._last_snapshot_rows = len(normalized_rows)
            if self._error is not None:
                raise RuntimeError(self._error)
            context = self._writer_context
            if not self._running or self._stopping or context is None:
                raise RuntimeError("Recorder is not running.")
            if not normalized_rows:
                return
            pending_rows = max(0, self._rows_enqueued - self._rows_written)
            if pending_rows + len(normalized_rows) > self.max_queue_rows:
                message = (
                    "Recorder backlog row capacity exceeded: "
                    f"{pending_rows} rows are pending, the next batch has {len(normalized_rows)} rows, "
                    f"and the configured limit is {self.max_queue_rows}."
                )
                self._backlog_error = message
                self._error = message
                raise RecorderBacklogError(message)
            try:
                context.work_queue.put_nowait(normalized_rows)
            except queue.Full as exc:
                message = (
                    "Recorder backlog batch capacity exceeded: "
                    f"the configured limit is {self.max_queue_batches} queued batches."
                )
                self._backlog_error = message
                self._error = message
                raise RecorderBacklogError(message) from exc
            self._rows_enqueued += len(normalized_rows)

    def request_stop(self) -> None:
        """Request a drain-and-stop without blocking the caller."""
        with self._lock:
            context = self._writer_context
            if context is None or context.completed.is_set():
                self._running = False
                self._stopping = False
                return
            self._running = False
            self._stopping = True
            context.stop_requested.set()

    def wait_for_stop(
        self,
        timeout: float | None = 0.0,
        *,
        raise_on_error: bool = True,
    ) -> bool:
        """Wait for the current writer, returning False when the timeout expires."""
        with self._lock:
            context = self._writer_context
            thread = self._thread
        if context is None:
            if raise_on_error:
                self._raise_if_error()
            return True
        if not context.completed.wait(timeout=timeout):
            return False
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        if raise_on_error:
            self._raise_if_error()
        return True

    def stop(self, timeout: float | None = 15.0) -> None:
        """Request stop and wait up to timeout while retaining timed-out state."""
        self.request_stop()
        if not self.wait_for_stop(timeout=timeout, raise_on_error=False):
            timeout_label = "indefinitely" if timeout is None else f"{timeout:g} seconds"
            raise RecorderStopTimeoutError(
                "Recorder writer is still draining accepted rows after waiting "
                f"{timeout_label}; poll status().flush_complete or wait_for_stop()."
            )
        self._raise_if_error()

    def _raise_if_error(self) -> None:
        with self._lock:
            error = self._error
        if error is not None:
            raise RuntimeError(error)

    def _writer_loop(self, context: _WriterSessionContext) -> None:
        """Buffer incoming snapshot batches and write one row group per flush.

        Rows are flushed when the buffer is old enough (flush_seconds), big
        enough (flush_rows), or on stop — not per 1-second tick — so part
        files hold a few large row groups instead of hundreds of tiny ones.
        """
        writer = None
        part_started_at: datetime | None = None
        rows_in_part = 0
        inprogress_path: Path | None = None
        final_path: Path | None = None
        buffered_rows: list[dict[str, Any]] = []
        buffer_started_at: datetime | None = None
        try:
            pa, pq = _require_pyarrow()
            schema = build_pyarrow_schema()

            def close_part() -> None:
                nonlocal writer, inprogress_path, final_path
                if writer is None:
                    return
                writer.close()
                writer = None
                if inprogress_path is not None and final_path is not None:
                    inprogress_path.replace(final_path)
                    with self._lock:
                        if self._writer_context is context:
                            self._current_part_path = final_path

            def open_part(now: datetime) -> None:
                nonlocal writer, part_started_at, rows_in_part
                nonlocal inprogress_path, final_path
                self._part_index += 1
                final_path = context.session_dir / f"part-{self._part_index:04d}.parquet"
                inprogress_path = Path(str(final_path) + ".inprogress")
                writer = pq.ParquetWriter(inprogress_path, schema)
                part_started_at = now
                rows_in_part = 0
                with self._lock:
                    if self._writer_context is context:
                        self._current_part_path = inprogress_path

            def flush_buffer() -> None:
                nonlocal writer, part_started_at, rows_in_part
                nonlocal inprogress_path, final_path, buffered_rows, buffer_started_at
                if not buffered_rows:
                    return
                rows_to_write = buffered_rows
                buffered_rows = []
                buffer_started_at = None
                offset = 0
                while offset < len(rows_to_write):
                    now = datetime.now(timezone.utc)
                    should_rotate = (
                        writer is None
                        or rows_in_part >= self.max_rows_per_part
                        or (
                            part_started_at is not None
                            and (now - part_started_at).total_seconds() >= self.roll_seconds
                        )
                    )
                    if should_rotate:
                        close_part()
                        open_part(now)
                    available_rows = self.max_rows_per_part - rows_in_part
                    chunk = rows_to_write[offset : offset + available_rows]
                    table = pa.Table.from_pylist(chunk, schema=schema)
                    writer.write_table(table)
                    chunk_size = len(chunk)
                    rows_in_part += chunk_size
                    offset += chunk_size
                    last_write_at = datetime.now(timezone.utc)
                    with self._lock:
                        self._rows_written += chunk_size
                        self._last_write_at = last_write_at

            def buffer_is_due() -> bool:
                if not buffered_rows:
                    return False
                if len(buffered_rows) >= self.flush_rows:
                    return True
                return (
                    buffer_started_at is not None
                    and (datetime.now(timezone.utc) - buffer_started_at).total_seconds() >= self.flush_seconds
                )

            while True:
                try:
                    rows = context.work_queue.get(timeout=0.25)
                except queue.Empty:
                    if buffer_is_due():
                        flush_buffer()
                    if context.stop_requested.is_set() and context.work_queue.empty():
                        break
                    continue
                if rows:
                    if buffer_started_at is None:
                        buffer_started_at = datetime.now(timezone.utc)
                    buffered_rows.extend(rows)
                if buffer_is_due():
                    flush_buffer()
                if context.stop_requested.is_set() and context.work_queue.empty():
                    break
            flush_buffer()
            close_part()
        except Exception as exc:
            with self._lock:
                self._error = str(exc)
        finally:
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
            with self._lock:
                if self._writer_context is context:
                    self._running = False
                    self._stopping = False
                context.completed.set()


def _to_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc)
