from __future__ import annotations

import json
import math
import os
import queue
import re
import threading
import time
import tkinter as tk
import uuid
from collections.abc import Mapping as MappingABC
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox
from tkinter import ttk
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from .schwab_client import SchwabMarketDataService, refresh_token_issued_at
from .models import (
    QUOTE_KIND_LEG,
    QUOTE_KIND_UNDERLYING,
    RIGHT_CALL,
    RIGHT_PUT,
    SIGNAL_BUY,
    SIGNAL_KIND_ANOMALY,
    SIGNAL_KIND_OPPORTUNITY,
    SIGNAL_SELL,
    DEFAULT_FORMULA_SETTINGS,
    DEFAULT_SIGNAL_METHOD,
    FormulaSettings,
    MatrixSignal,
    MatrixSelectionPlan,
    MonitoredSideSnapshot,
    OptionContractRecord,
    QuoteSnapshot,
    RankedMatrixSignal,
    SearchResult,
    VerticalSelection,
    build_matrix_selection_plan,
    build_matrix_snapshots,
    build_ranked_matrix_signals,
    compute_matrix_signal,
    effective_mid,
    fair_value_edge_for_width,
    flatten_matrix_context,
    format_expiration,
    format_price,
    format_size,
    format_strike,
    is_supported_spread_width,
    is_supported_matrix_strike,
    is_supported_matrix_pair,
    normalize_right,
    normalize_signal_method,
    pair_key_for_pair,
)
from .recorder import (
    MARKET_TIMEZONE,
    MarketSessionGuard,
    MarketSessionStatus,
    RecorderStatus,
    SnapshotRowBuilder,
    recording_ticker_label_from_tabs,
)
from .position_ui import (
    PositionAlertsMixin,
    position_exit_for_selection,
    position_record_for_selection,
    position_price,
)
from .storage import ParquetSnapshotRecorder, default_recording_root
from .studio_settings import (
    STUDIO_SETTINGS_FILENAME,
    StudioSettings,
    load_studio_settings,
    save_studio_settings,
    studio_app_data_dir,
)
# Colors, fonts and the ttk theme come from the One Desk bridge (one_desk.py), outside
# this fingerprinted file, so restyling never makes the work PC say UPDATE NEEDED.
from . import one_desk
from .one_desk import (
    ANOMALY_COLOR,
    ASK_COLOR,
    BID_COLOR,
    BUY_COLOR,
    CONNECTION_FAILED_COLOR,
    CONNECTION_LIVE_COLOR,
    CONNECTION_OFFLINE_COLOR,
    CONNECTION_PENDING_COLOR,
    FAINT,
    FAIR_VALUE_COLOR,
    GRID_BG,
    GRID_LINE,
    HEADER_BG,
    INK,
    INK_2,
    ITM_BG,
    ITM_SELECTED_BG,
    ITM_TEXT,
    LIQUIDITY_OVERFLOW_COLOR,
    LIQUIDITY_TRACK_COLOR,
    LIQUIDITY_WIDTH_COLOR,
    MID_COLOR,
    MISSING_BG,
    MUTED,
    NAVY,
    POSITION_BUY_BG,
    POSITION_ICON_COLOR,
    POSITION_SELL_BG,
    RAISED,
    RECORDER_ACTIVE_COLOR,
    RECORDER_OFF_COLOR,
    RECORDER_PENDING_COLOR,
    RULE,
    SELECTED_BG,
    SELL_COLOR,
    SUBSCRIPTION_CONFIRMED_COLOR,
    SUBSCRIPTION_CONTEXT_COLOR,
    SUBSCRIPTION_FAILED_COLOR,
    SUBSCRIPTION_PENDING_COLOR,
    SUBSCRIPTION_RECONCILING_COLOR,
    SUNKEN,
    SURFACE,
    TOOLTIP_BG,
    TOOLTIP_FG,
    UP_TINT,
)
if TYPE_CHECKING:
    from .studio_services import StudioServices


REQUIRED_WIDTHS = (1.00,)
OPTIONAL_WIDTHS = (2.00, 3.00, 4.00, 5.00)
DEFAULT_WIDTHS = REQUIRED_WIDTHS + OPTIONAL_WIDTHS
MATRIX_WIDTH_BANDS = DEFAULT_WIDTHS
DEFAULT_SELECTED_WIDTH_BANDS = (1.00, 2.00)
MAX_MATRIX_STRIKE_WIDTH = 5.00
MATRIX_MODE_REGULAR = "Regular"
MATRIX_MODE_MODIFIED = "Modified"
MATRIX_MODES = (MATRIX_MODE_REGULAR, MATRIX_MODE_MODIFIED)
DEFAULT_MATRIX_MODE = MATRIX_MODE_MODIFIED
MATRIX_VIEW_BOTH = "BOTH"
MATRIX_VIEWS = (RIGHT_CALL, RIGHT_PUT, MATRIX_VIEW_BOTH)
MatrixCellKey = tuple[float, float]
MatrixViewCellKey = tuple[str, float, float]
SelectedMatrixCellKey = MatrixCellKey | MatrixViewCellKey
SETTINGS_SIGNAL_METHOD_KEY = "signal_method"
MAX_MATRIX_TABS = 3
UI_POLL_INTERVAL_MS = 50
QUOTE_REDRAW_INTERVAL_MS = 75
MATRIX_CACHE_VERSION = 1
CONNECTION_TASK_KINDS = frozenset({"connect", "disconnect"})
LIQUIDITY_GAUGE_MIN_PEERS = 4
LIQUIDITY_GAUGE_SIZE_STEPS = 4
LIQUIDITY_GAUGE_WIDTH_STEPS = 8
LIQUIDITY_GAUGE_MAX_WIDTH = 34.0
LIQUIDITY_GAUGE_MIN_WIDTH = 22.0
LIQUIDITY_COMPACT_SIGNAL_THRESHOLD = 145.0
POSITION_ICON_LANE_WIDTH = 12.0
POSITION_ICON_TEXT = "\u25cf"
SUBSCRIPTION_HATCH_TAG = "subscription-hatch"
RANK_SORT_NONE = "none"
RANK_SORT_ASC = "asc"
RANK_SORT_DESC = "desc"
RANK_SORT_COLUMNS = ("rank", "tab", "side", "combo", "width", "signal", "price", "edge")
MONTH_ABBREVIATIONS = (
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


@dataclass(frozen=True)
class _WorkerResult:
    kind: str
    token: int
    generation: int = 0
    value: Any = None
    error: str | None = None


@dataclass(frozen=True)
class MatrixInputState:
    display_expiration: str
    raw_expiration: str
    side: str
    low_strike: float
    high_strike: float
    widths: tuple[float, ...]
    ticker_symbol: str = ""
    matrix_mode: str = DEFAULT_MATRIX_MODE
    width_bands: tuple[float, ...] | None = None


@dataclass(frozen=True)
class RankSortState:
    column: str | None = None
    direction: str = RANK_SORT_NONE


@dataclass(frozen=True)
class FairValueEdgeCurveValues:
    fair_value_mid: float
    edge: float
    buy_endpoint: float
    sell_endpoint: float


@dataclass(frozen=True)
class MarketDataQuoteCounts:
    combo_quotes: int
    leg_quotes: int

    @property
    def total(self) -> int:
        return self.combo_quotes + self.leg_quotes


@dataclass(frozen=True)
class MarketDataQuoteStatus:
    current: MarketDataQuoteCounts | None = None
    preview_total: MarketDataQuoteCounts | None = None
    added: MarketDataQuoteCounts | None = None
    removed: MarketDataQuoteCounts | None = None
    preview_action: str = "loaded"


@dataclass(frozen=True)
class MatrixInputPlan:
    row_strikes: tuple[float, ...]
    plan: MatrixSelectionPlan
    widths: tuple[float, ...] = ()


@dataclass(frozen=True)
class MatrixViewInputPlan:
    row_strikes: tuple[float, ...]
    side_plans: dict[str, MatrixInputPlan]
    widths: tuple[float, ...] = ()


@dataclass
class MatrixSideState:
    right: str
    row_strikes: tuple[float, ...]
    matrix_plan: MatrixSelectionPlan
    matrix_snapshots: dict[MatrixCellKey, MonitoredSideSnapshot] = field(default_factory=dict)
    ranked_signals: list[RankedMatrixSignal] = field(default_factory=list)


@dataclass
class MatrixTabState:
    tab_id: int
    label: str
    matrix_input: MatrixInputState
    row_strikes: tuple[float, ...]
    matrix_plan: MatrixSelectionPlan
    leg_quotes: dict[int, QuoteSnapshot] = field(default_factory=dict)
    leg_quote_ids: frozenset[int] = frozenset()
    matrix_snapshots: dict[MatrixCellKey, MonitoredSideSnapshot] = field(default_factory=dict)
    ranked_signals: list[RankedMatrixSignal] = field(default_factory=list)
    selected_cell_key: SelectedMatrixCellKey | None = None
    is_stale: bool = False
    side_states: dict[str, MatrixSideState] = field(default_factory=dict)
    underlying_price_hint: float | None = None


@dataclass(frozen=True)
class RecorderMatrixTabView:
    tab_id: int
    label: str
    matrix_input: MatrixInputState
    row_strikes: tuple[float, ...]
    matrix_plan: MatrixSelectionPlan
    leg_quotes: dict[int, QuoteSnapshot]
    leg_quote_ids: frozenset[int]
    is_stale: bool = False
    underlying_price_hint: float | None = None


@dataclass(frozen=True)
class CachedMatrixTab:
    raw_expiration: str
    side: str
    low_strike: float
    high_strike: float
    widths: tuple[float, ...]
    selected_cell_key: SelectedMatrixCellKey | None = None
    label: str = ""
    row_strikes: tuple[float, ...] = ()


@dataclass(frozen=True)
class CachedMatrixWorkspace:
    ticker_symbol: str
    tabs: tuple[CachedMatrixTab, ...]
    active_tab_identity: tuple[str, str, str] | None = None
    matrix_mode: str = DEFAULT_MATRIX_MODE


@dataclass(frozen=True)
class RankedMatrixSignalRow:
    rank: int
    tab_id: int
    tab_label: str
    lower_strike: float
    width: float
    pair_key: str
    selection: VerticalSelection
    snapshot: MonitoredSideSnapshot
    signal: MatrixSignal


@dataclass(frozen=True)
class MatrixWidthSlot:
    width: float
    selected: bool
    x: float


@dataclass(frozen=True)
class MatrixGridLayout:
    strike_width: float
    group_width: float
    combo_width: float
    signal_width: float
    total_width: float
    slots: tuple[MatrixWidthSlot, ...]


@dataclass(frozen=True)
class SplitMatrixGridLayout:
    strike_x: float
    strike_width: float
    group_width: float
    combo_width: float
    signal_width: float
    total_width: float
    put_slots: tuple[MatrixWidthSlot, ...]
    call_slots: tuple[MatrixWidthSlot, ...]


@dataclass(frozen=True)
class MatrixCanvasCellItems:
    rectangle_ids: tuple[int, int]
    combo_text_id: int
    signal_text_id: int
    liquidity_gauge: MatrixLiquidityGaugeCanvasItems | None = None
    compact_signal: bool = False
    position_icon_id: int | None = None


@dataclass(frozen=True)
class MatrixLiquidityGaugeState:
    quote_width_ratio: float | None = None
    quote_width_overflow: bool = False
    quote_crossed: bool = False
    bid_support: float | None = None
    ask_support: float | None = None
    size_comparable: bool = False
    size_peer_count: int = 0


@dataclass(frozen=True)
class MatrixLiquidityGaugeCanvasItems:
    quote_id: int
    auxiliary_id: int
    size_track_id: int
    bid_id: int
    ask_id: int
    center_id: int
    left: float
    right: float
    quote_y: float
    size_top: float
    size_bottom: float


@dataclass(frozen=True)
class MatrixCellSubscriptionStyle:
    outline: str = GRID_LINE
    width: int = 1
    dash: tuple[int, ...] = ()
    hatch: int = 0  # hatch spacing in pixels; 0 draws none


def resolve_matrix_cell_subscription_style(
    direct_states: Sequence[str],
    context_states: Sequence[str] = (),
    *,
    recurrent_failures: int = 0,
) -> MatrixCellSubscriptionStyle:
    """Resolve leg subscription health without replacing moneyness cell fills.

    Healthy cells keep the plain hairline. A failed direct leg hatches the cell
    (denser when the failure recurs) instead of outlining it in red.
    """
    direct = {str(state).strip().lower() for state in direct_states}
    context = {str(state).strip().lower() for state in context_states}
    if "failed" in direct:
        return MatrixCellSubscriptionStyle(
            hatch=(
                one_desk.RECURRENT_FAILED_HATCH_SPACING
                if recurrent_failures >= 2
                else one_desk.FAILED_HATCH_SPACING
            ),
        )
    if direct & {"pending_subscribe", "pending_unsubscribe"}:
        return MatrixCellSubscriptionStyle(outline=SUBSCRIPTION_PENDING_COLOR, width=2)
    if not direct or direct & {"desired", "unknown"}:
        return MatrixCellSubscriptionStyle(
            outline=SUBSCRIPTION_RECONCILING_COLOR,
            width=2,
            dash=(3, 2),
        )
    if context & {"failed", "pending_subscribe", "pending_unsubscribe", "desired", "unknown"}:
        return MatrixCellSubscriptionStyle(
            outline=SUBSCRIPTION_CONTEXT_COLOR,
            width=2,
            dash=(3, 2),
        )
    return MatrixCellSubscriptionStyle()


@dataclass(frozen=True)
class _ReloadMatrixTabResult:
    tab_id: int
    search_result: SearchResult
    records: Sequence[OptionContractRecord]


@dataclass(frozen=True)
class _StartRecordingResult:
    output_root: Path
    ticker_label: str
    market_session_guard: MarketSessionGuard
    market_now_utc: datetime
    local_now_utc: datetime
    session_status: MarketSessionStatus
    armed: bool = False


@dataclass(frozen=True)
class _RecordSnapshotResult:
    recorder: ParquetSnapshotRecorder
    session_id: str
    captured_at: datetime
    rows: Sequence[Mapping[str, Any]]


@dataclass(frozen=True)
class RecorderIndicatorState:
    state: str
    text: str
    color: str


def resolve_recorder_indicator_state(
    *,
    running: bool,
    start_inflight: bool,
    armed: bool,
    stopping: bool = False,
    error: str | None = None,
) -> RecorderIndicatorState:
    if error:
        return RecorderIndicatorState("ERROR", "! REC ERROR", RECORDER_ACTIVE_COLOR)
    if running:
        return RecorderIndicatorState("ON", "\u25cf REC ON", RECORDER_ACTIVE_COLOR)
    if stopping:
        return RecorderIndicatorState("STOPPING", "\u25cf REC STOPPING", RECORDER_PENDING_COLOR)
    if armed:
        return RecorderIndicatorState("ARMED", "\u25cf REC ARMED", RECORDER_PENDING_COLOR)
    if start_inflight:
        return RecorderIndicatorState("STARTING", "\u25cf REC STARTING", RECORDER_PENDING_COLOR)
    return RecorderIndicatorState("OFF", "\u25cb REC OFF", RECORDER_OFF_COLOR)


@dataclass(frozen=True)
class FormulaSettingControl:
    key: str
    label: str
    section: str
    purpose: str
    min_value: float = 0.0
    include_min: bool = True


@dataclass(frozen=True)
class FairValueExamplePoint:
    x: float
    value: float
    role: str


@dataclass(frozen=True)
class FairValueSettingsExampleData:
    edge_schedule: tuple[tuple[float, float], ...]
    observation_points: tuple[FairValueExamplePoint, ...]
    fitted_points: tuple[tuple[float, float], ...]
    atomic_values: tuple[float, ...]
    composed_width: float
    composed_fair_value: float
    composed_edge: float
    buy_endpoint: float
    sell_endpoint: float


@dataclass(frozen=True)
class MatrixCellVisualLegendItem:
    key: str
    category: str
    label: str
    description: str
    fill: str | None = None
    outline: str | None = None
    width: int = 1
    dash: tuple[int, ...] = ()
    sample_text: str = ""
    text_color: str = INK
    hatch: int = 0

    @property
    def border_width(self) -> int:
        return self.width


FAIR_VALUE_SYSTEM_OVERVIEW = (
    (
        "1. Display and recording",
        "Complete spreads in the selected strike range remain visible and recordable, including ITM spreads. "
        "Spreads using either strike price ending in .50 are hidden and not recorded, including "
        "157.50/162.50 at width 5.00. Strike widths ending in .50 are also excluded from display, "
        "recording, and fair-value calculations. "
        "ITM rows are muted and excluded from the aggregate actionable-signal table.",
    ),
    (
        "2. Calibration universe",
        "Each eligible calibration-spread midpoint is divided by its strike interval to form a 0-1 value density. "
        "Adjacent intervals with widths ending in .50 are combined into supported wider calibration spreads, "
        "so their individual midpoints "
        "never enter the fit. The "
        "underlying places those densities on a common moneyness axis. Each CALL curve's universe contains only "
        "eligible CALL midpoints, and each PUT curve's universe contains only eligible PUT midpoints. Each same-side "
        "universe contains displayed and nearby OTM context plus a limited near-money bridge; deep ITM display is not "
        "blindly fitted. Within that universe, every structurally usable midpoint remains a regression candidate even "
        "when its synthetic market is wider than the strike interval, so quote width is not a hard eligibility cutoff.",
    ),
    (
        "3. Fair-value curve",
        "Each atomic candidate gets a leave-one-out decision fit. When its direct midpoint belongs to the calibration "
        "universe, that observation is held out; a display-only candidate outside calibration has no direct "
        "observation to remove. The remaining eligible observations from that same option side are fitted by the "
        "robust, bounded, monotone sigmoid/probability-style regression. This prevents a candidate from pulling its "
        "own fair value toward its midpoint, while robust weighting still "
        "limits other isolated residual outliers without letting the curve reverse or leave the atomic spread's "
        "physical range. Min Regression Points is checked on each decision fit after the holdout; without that full "
        "support, the atomic fair value and signal are unavailable. Existing fit-point count and recorded fit error "
        "remain full-pool diagnostics; fit error is the mean absolute in-sample density residual.",
    ),
    (
        "4. Wider spreads and signals",
        "A wider spread's fair value is the sum of the calibration intervals' leave-one-out fair values it spans, "
        "with proportional values where a strike falls inside an interval. A trailing remainder that cannot form "
        "a supported interval uses the last supported interval's fitted density. If any "
        "required atomic fair value is unavailable, the wider fair value and signal are also unavailable. Otherwise "
        "its configured width edge is applied around fair value: widths at or below 1 use the W1 edge, while larger "
        "fractional widths interpolate between configured bands. BUY at or below fair minus edge, SELL at or above "
        "fair plus edge.",
    ),
    (
        "Independent CALL and PUT curves, and missing spot",
        "CALL and PUT curves remain fully independent, including in BOTH view. CALL decision fits use only eligible "
        "CALL vertical midpoints, and PUT decision fits use only eligible PUT vertical midpoints; no opposite-side "
        "observation enters either curve. A positive REST chain price can seed moneyness until the live underlying "
        "stream takes priority.",
    ),
)


def fair_value_settings_example_data(settings: FormulaSettings) -> FairValueSettingsExampleData:
    """Stable explanatory data used by the Settings fair-value graphics."""
    edge_schedule = tuple(
        (float(width), fair_value_edge_for_width(float(width), settings))
        for width in range(1, 6)
    )
    observation_points = (
        FairValueExamplePoint(-3.0, 0.08, "market_mid"),
        FairValueExamplePoint(-2.25, 0.15, "wide_market_mid"),
        FairValueExamplePoint(-1.5, 0.22, "market_mid"),
        FairValueExamplePoint(-0.75, 0.56, "held_out_candidate"),
        FairValueExamplePoint(0.0, 0.55, "market_mid"),
        FairValueExamplePoint(1.0, 0.74, "market_mid"),
        FairValueExamplePoint(2.0, 0.88, "wide_market_mid"),
    )
    fitted_points = (
        (-3.0, 0.08),
        (-2.5, 0.11),
        (-2.0, 0.16),
        (-1.5, 0.23),
        (-1.0, 0.32),
        (-0.5, 0.43),
        (0.0, 0.55),
        (0.5, 0.66),
        (1.0, 0.75),
        (1.5, 0.83),
        (2.0, 0.88),
    )
    atomic_values = (0.27, 0.31, 0.35)
    composed_fair_value = round(sum(atomic_values), 4)
    composed_width = 3.0
    composed_edge = fair_value_edge_for_width(composed_width, settings)
    return FairValueSettingsExampleData(
        edge_schedule=edge_schedule,
        observation_points=observation_points,
        fitted_points=fitted_points,
        atomic_values=atomic_values,
        composed_width=composed_width,
        composed_fair_value=composed_fair_value,
        composed_edge=composed_edge,
        buy_endpoint=round(composed_fair_value - composed_edge, 4),
        sell_endpoint=round(composed_fair_value + composed_edge, 4),
    )


def matrix_cell_visual_legend_items() -> tuple[MatrixCellVisualLegendItem, ...]:
    """Return every visual matrix-cell state shown in the Settings guide."""
    return (
        MatrixCellVisualLegendItem(
            "fill_otm", "fill", "Plain fill", "Normal quoted OTM spread.", fill=GRID_BG, sample_text="100/101"
        ),
        MatrixCellVisualLegendItem(
            "fill_selected_otm", "fill", "Selected fill", "Selected OTM cell.", fill=SELECTED_BG, sample_text="100/101"
        ),
        MatrixCellVisualLegendItem(
            "fill_itm", "fill", "ITM fill", "At least one leg is ITM: displayed and recorded, but muted and not aggregate-actionable.", fill=ITM_BG, sample_text="100/101", text_color=ITM_TEXT
        ),
        MatrixCellVisualLegendItem(
            "fill_selected_itm", "fill", "Selected ITM fill", "Selected ITM cell.", fill=ITM_SELECTED_BG, sample_text="100/101", text_color=ITM_TEXT
        ),
        MatrixCellVisualLegendItem(
            "fill_missing", "fill", "Blank cell", "No complete spread exists for this strike/width; the cell is blank.", fill=MISSING_BG
        ),
        MatrixCellVisualLegendItem(
            "position_saved", "position", "Navy position dot", "A position is saved for this exact ticker, expiration, side, and pair. The dot remains while its exit alert is paused and disappears when the position is deleted. Click the cell and use Position to edit it.", fill=GRID_BG, sample_text="100/101", text_color=POSITION_ICON_COLOR
        ),
        MatrixCellVisualLegendItem(
            "position_exit", "position", "Position exit target", "A saved position reached its target using your average entry and the configured width edge. EXIT BUY / EXIT SELL (dense: XB / XS) illuminates even for ITM spreads. Normal fair-value signals remain in the Signals table and detail chart.", fill=POSITION_BUY_BG, sample_text="EXIT BUY 0.38", text_color=BUY_COLOR
        ),
        MatrixCellVisualLegendItem(
            "border_confirmed", "border", "Hairline border", "Direct legs, hidden fair-value context, and underlying stream are confirmed. Healthy cells stay plain.", fill=GRID_BG, outline=GRID_LINE, sample_text="LIVE"
        ),
        MatrixCellVisualLegendItem(
            "border_pending", "border", "Thick navy border", "A direct leg is pending subscribe or unsubscribe acknowledgement.", fill=GRID_BG, outline=SUBSCRIPTION_PENDING_COLOR, width=2, sample_text="PENDING"
        ),
        MatrixCellVisualLegendItem(
            "border_reconciling", "border", "Dashed navy border", "A direct leg is desired or unknown but not yet confirmed.", fill=GRID_BG, outline=SUBSCRIPTION_RECONCILING_COLOR, width=2, dash=(3, 2), sample_text="SYNC"
        ),
        MatrixCellVisualLegendItem(
            "border_context", "border", "Dashed amber border", "Direct legs are usable, but hidden same-side fair-value context or the underlying stream is degraded.", fill=GRID_BG, outline=SUBSCRIPTION_CONTEXT_COLOR, width=2, dash=(3, 2), sample_text="FV CTX"
        ),
        MatrixCellVisualLegendItem(
            "border_failed", "border", "Hatched cell", "At least one direct leg subscription failed. The hatch marks the cell's quotes as untrusted.", fill=GRID_BG, outline=GRID_LINE, hatch=one_desk.FAILED_HATCH_SPACING, sample_text="FAILED"
        ),
        MatrixCellVisualLegendItem(
            "border_recurrent_failed", "border", "Dense hatch", "The direct-leg failure has recurred during this workspace generation.", fill=GRID_BG, outline=GRID_LINE, hatch=one_desk.RECURRENT_FAILED_HATCH_SPACING, sample_text="REPEAT"
        ),
        MatrixCellVisualLegendItem(
            "text_buy", "text", "Green B text", "Fair-value BUY signal: price is at or below fair minus configured edge. Dense cells shorten the example to B.42.", fill=GRID_BG, sample_text="B 0.42 (0.12)", text_color=BUY_COLOR
        ),
        MatrixCellVisualLegendItem(
            "text_sell", "text", "Red S text", "Fair-value SELL signal: price is at or above fair plus configured edge. Dense cells shorten the example to S.68.", fill=GRID_BG, sample_text="S 0.68 (0.12)", text_color=SELL_COLOR
        ),
        MatrixCellVisualLegendItem(
            "text_anomaly", "text", "Violet B/S text", "Anomaly signal took priority over the fair-value opportunity label.", fill=GRID_BG, sample_text="B 0.42 (0.12)", text_color=ANOMALY_COLOR
        ),
        MatrixCellVisualLegendItem(
            "text_neutral", "text", "Ink market mid", "M is the synthetic market mid. Fair value is ready, but there is no actionable signal. Dense cells shorten the example to M.55; a minus sign appears only for a negative price.", fill=GRID_BG, sample_text="M 0.55"
        ),
        MatrixCellVisualLegendItem(
            "text_fv_waiting", "text", "Amber market + FV?", "M is the usable synthetic market mid; FV? means fair value is waiting for spot, enough remaining usable observations after the leave-one-out holdout, or complete atomic leave-one-out coverage. Dense cells use M.55?; select the cell to see the reason.", fill=GRID_BG, sample_text="M 0.55 FV?", text_color=SUBSCRIPTION_CONTEXT_COLOR
        ),
        MatrixCellVisualLegendItem(
            "text_waiting", "text", "Gray ellipsis", "Waiting for the cell snapshot or its required quotes.", fill=GRID_BG, sample_text="...", text_color=MUTED
        ),
        MatrixCellVisualLegendItem(
            "text_no_mid", "text", "Gray double dash", "Snapshot exists, but no usable synthetic mid is available.", fill=GRID_BG, sample_text="--", text_color=MUTED
        ),
        MatrixCellVisualLegendItem(
            "text_itm_muted", "text", "Muted gray text", "ITM combo/signal text is informational and excluded from aggregate actionable signals.", fill=ITM_BG, sample_text="B 0.42 (0.12)", text_color=ITM_TEXT
        ),
        MatrixCellVisualLegendItem(
            "liquidity_quote_width",
            "liquidity",
            "Top quote-width bracket",
            "Synthetic ask minus bid, divided by the spread's strike distance. Shorter is tighter. It is visual context, not a quote filter.",
            fill=GRID_BG,
            sample_text="0.55",
        ),
        MatrixCellVisualLegendItem(
            "liquidity_relative_size",
            "liquidity",
            "Bottom relative-support bars",
            "The darker left half is bid size and the lighter right half is ask size. Each synthetic size is the minimum available size across its two required leg sides. Both share a log scale capped at the visible same-side, exact-width curve's 90th percentile. Longer means more relative displayed support; this is not guaranteed execution or native complex-book depth.",
            fill=GRID_BG,
            sample_text="0.55",
        ),
        MatrixCellVisualLegendItem(
            "liquidity_missing_size",
            "liquidity",
            "Hollow size half",
            "That side's displayed size is missing. A zero size leaves an empty half without the hollow outline, so missing and zero remain distinct.",
            fill=GRID_BG,
            sample_text="0.55",
        ),
        MatrixCellVisualLegendItem(
            "liquidity_unranked",
            "liquidity",
            "Dashed size track",
            f"Relative size is not ranked until at least {LIQUIDITY_GAUGE_MIN_PEERS} quote-complete, size-bearing cells provide a positive reference on that curve.",
            fill=GRID_BG,
            sample_text="0.55",
        ),
        MatrixCellVisualLegendItem(
            "liquidity_overflow",
            "liquidity",
            "Amber overflow chevron",
            "The synthetic quote width exceeds 100% of the spread's strike distance. The bracket is capped, but the quote remains available to the rest of the app.",
            fill=GRID_BG,
            sample_text="0.55",
        ),
        MatrixCellVisualLegendItem(
            "liquidity_crossed",
            "liquidity",
            "Red X",
            "The current synthetic ask is below its bid. Quote width and relative sizes are treated as invalid until the quote normalizes.",
            fill=GRID_BG,
            sample_text="0.55",
        ),
        MatrixCellVisualLegendItem(
            "state_stale", "state", "Stale overlay", "A cached tab has no live contents until Reload matrix completes.", fill=SUNKEN, outline=INK_2, width=2, dash=(3, 2), sample_text="Reload matrix", text_color=INK_2
        ),
    )


FORMULA_SETTING_CONTROLS = (
    FormulaSettingControl(
        "fair_value_edge_width_rate",
        "Width 1 Edge",
        "Fair Value Edge Curve",
        "Minimum edge for a width-1 spread.",
    ),
    FormulaSettingControl(
        "fair_value_edge_width_2",
        "Width 2 Edge",
        "Fair Value Edge Curve",
        "Minimum edge for a width-2 spread.",
    ),
    FormulaSettingControl(
        "fair_value_edge_width_3",
        "Width 3 Edge",
        "Fair Value Edge Curve",
        "Minimum edge for a width-3 spread.",
    ),
    FormulaSettingControl(
        "fair_value_edge_width_4",
        "Width 4 Edge",
        "Fair Value Edge Curve",
        "Minimum edge for a width-4 spread.",
    ),
    FormulaSettingControl(
        "fair_value_edge_width_5",
        "Width 5 Edge",
        "Fair Value Edge Curve",
        "Minimum edge for a width-5 spread.",
    ),
    FormulaSettingControl(
        "fair_value_edge_curve_min_points",
        "Min Regression Points",
        "Fair Value Edge Curve",
        "Minimum usable calibration observations required for each atomic decision fit after any direct-mid holdout; below this, fair value and signal are unavailable.",
        min_value=2.0,
    ),
)
EDITABLE_FORMULA_SETTING_CONTROLS = FORMULA_SETTING_CONTROLS
FAIR_VALUE_EDGE_KEYS_BY_WIDTH = {
    1: "fair_value_edge_width_rate",
    2: "fair_value_edge_width_2",
    3: "fair_value_edge_width_3",
    4: "fair_value_edge_width_4",
    5: "fair_value_edge_width_5",
}
FAIR_VALUE_EDGE_WIDTH_BY_KEY = {
    key: width for width, key in FAIR_VALUE_EDGE_KEYS_BY_WIDTH.items()
}


def format_formula_setting_value(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Durably replace a small JSON settings/cache file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def formula_settings_config_path(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    appdata = (env.get("APPDATA") or "").strip()
    if appdata:
        return Path(appdata) / "OptionsMatrix" / "settings.json"
    return (home or Path.home()) / ".options_matrix_settings.json"


def width_preferences_config_path(settings_path: Path | None = None) -> Path:
    return (settings_path or formula_settings_config_path()).with_name("width_preferences.json")


def load_width_band_preferences(
    path: Path | None = None,
    *,
    cached_workspace: CachedMatrixWorkspace | None = None,
) -> dict[float, bool]:
    selected = set(DEFAULT_SELECTED_WIDTH_BANDS)
    try:
        with (path or width_preferences_config_path()).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        # Seed older installations from the active saved matrix only once at
        # startup. Later chain availability must never replace these choices.
        if cached_workspace is not None and cached_workspace.tabs:
            cached_tab = next(
                (
                    tab for tab in cached_workspace.tabs
                    if (cached_workspace.ticker_symbol, tab.raw_expiration, tab.side)
                    == cached_workspace.active_tab_identity
                ),
                cached_workspace.tabs[0],
            )
            selected = set(matrix_width_bands(cached_tab.widths))
    except (OSError, json.JSONDecodeError):
        pass
    else:
        if isinstance(payload, MappingABC) and payload.get("version") == 1:
            raw_bands = payload.get("selected_bands")
            if (
                isinstance(raw_bands, list)
                and all(
                    isinstance(band, (int, float))
                    and not isinstance(band, bool)
                    and band in MATRIX_WIDTH_BANDS
                    for band in raw_bands
                )
            ):
                selected = set(raw_bands)
    return {band: band in selected for band in MATRIX_WIDTH_BANDS}


def save_width_band_preferences(
    preferences: Mapping[float, bool],
    path: Path | None = None,
) -> None:
    _atomic_write_json(
        path or width_preferences_config_path(),
        {
            "version": 1,
            "selected_bands": [band for band in MATRIX_WIDTH_BANDS if preferences.get(band, False)],
        },
    )


def formula_settings_to_dict(
    settings: FormulaSettings,
    *,
    signal_method: str | None = None,
) -> dict[str, float | str]:
    payload: dict[str, float | str] = {
        control.key: float(getattr(settings, control.key))
        for control in FORMULA_SETTING_CONTROLS
    }
    if signal_method is not None:
        payload[SETTINGS_SIGNAL_METHOD_KEY] = normalize_signal_method(signal_method)
    return payload


def _formula_setting_valid(value: float, control: FormulaSettingControl) -> bool:
    if not math.isfinite(value):
        return False
    if control.include_min:
        return value >= control.min_value
    return value > control.min_value


def _formula_setting_range_text(control: FormulaSettingControl) -> str:
    if control.include_min:
        return f"{control.min_value:g} or greater"
    return f"greater than {control.min_value:g}"


def formula_settings_from_mapping(mapping: Mapping[str, object]) -> FormulaSettings:
    values: dict[str, float] = {}
    new_edge_keys = tuple(FAIR_VALUE_EDGE_KEYS_BY_WIDTH[width] for width in range(2, 6))
    migrate_legacy_edge_schedule = not any(key in mapping for key in new_edge_keys)
    for control in FORMULA_SETTING_CONTROLS:
        default_value = float(getattr(DEFAULT_FORMULA_SETTINGS, control.key))
        if (
            migrate_legacy_edge_schedule
            and control.key in new_edge_keys
        ):
            base_value = values[FAIR_VALUE_EDGE_KEYS_BY_WIDTH[1]]
            raw_value = fair_value_edge_for_width(
                float(FAIR_VALUE_EDGE_WIDTH_BY_KEY[control.key]),
                base_value,
            )
        else:
            raw_value = mapping.get(control.key, default_value)
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            value = default_value
        if not _formula_setting_valid(value, control):
            value = default_value
        values[control.key] = value
    return FormulaSettings(**values)


def signal_method_from_mapping(mapping: Mapping[str, object]) -> str:
    raw_value = mapping.get(SETTINGS_SIGNAL_METHOD_KEY, DEFAULT_SIGNAL_METHOD)
    if not isinstance(raw_value, str):
        return DEFAULT_SIGNAL_METHOD
    try:
        return normalize_signal_method(raw_value)
    except ValueError:
        return DEFAULT_SIGNAL_METHOD


def parse_formula_settings_values(values: Mapping[str, str]) -> FormulaSettings:
    parsed: dict[str, float] = {}
    for control in FORMULA_SETTING_CONTROLS:
        raw_value = values.get(control.key, "").strip()
        try:
            value = float(raw_value)
        except ValueError as exc:
            raise ValueError(f"{control.label} must be a number.") from exc
        if not _formula_setting_valid(value, control):
            raise ValueError(f"{control.label} must be {_formula_setting_range_text(control)}.")
        parsed[control.key] = value
    return FormulaSettings(**parsed)


def load_formula_settings(path: Path | None = None) -> FormulaSettings:
    config_path = path or formula_settings_config_path()
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return DEFAULT_FORMULA_SETTINGS
    if not isinstance(payload, MappingABC):
        return DEFAULT_FORMULA_SETTINGS
    return formula_settings_from_mapping(payload)


def load_signal_method(path: Path | None = None) -> str:
    config_path = path or formula_settings_config_path()
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return DEFAULT_SIGNAL_METHOD
    if not isinstance(payload, MappingABC):
        return DEFAULT_SIGNAL_METHOD
    return signal_method_from_mapping(payload)


def save_formula_settings(
    settings: FormulaSettings,
    path: Path | None = None,
    *,
    signal_method: str | None = None,
) -> None:
    config_path = path or formula_settings_config_path()
    _atomic_write_json(
        config_path,
        formula_settings_to_dict(settings, signal_method=signal_method),
    )


def normalize_matrix_mode(value: str | None) -> str:
    """Accept legacy cache values while normalizing all behavior to unified mode."""
    raw = (value or DEFAULT_MATRIX_MODE).strip()
    for mode in MATRIX_MODES:
        if raw.lower() == mode.lower():
            return DEFAULT_MATRIX_MODE
    raise ValueError(f"Unsupported matrix mode: {value!r}")


def normalize_matrix_view(value: str | None) -> str:
    raw = (value or "").strip().upper()
    if raw == MATRIX_VIEW_BOTH:
        return MATRIX_VIEW_BOTH
    return normalize_right(raw)


def matrix_view_rights(value: str | None) -> tuple[str, ...]:
    view = normalize_matrix_view(value)
    if view == MATRIX_VIEW_BOTH:
        return (RIGHT_PUT, RIGHT_CALL)
    return (view,)


def matrix_workspace_cache_path(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    appdata = (env.get("APPDATA") or "").strip()
    if appdata:
        return Path(appdata) / "OptionsMatrix" / "matrix_cache.json"
    return (home or Path.home()) / ".options_matrix_cache.json"


def cached_matrix_workspace_from_tabs(
    tabs: Sequence[MatrixTabState],
    active_tab_id: int | None,
) -> CachedMatrixWorkspace | None:
    if not tabs:
        return None
    ticker_symbol = tabs[0].matrix_input.ticker_symbol
    if not ticker_symbol:
        return None
    if any(tab.matrix_input.ticker_symbol != ticker_symbol for tab in tabs):
        return None
    for index, tab in enumerate(tabs):
        if any(
            matrix_tab_identity(tab.matrix_input) == matrix_tab_identity(other.matrix_input)
            or matrix_tab_views_conflict(tab.matrix_input, other.matrix_input)
            for other in tabs[index + 1 :]
        ):
            return None

    active_tab = next((tab for tab in tabs if tab.tab_id == active_tab_id), None)
    return CachedMatrixWorkspace(
        ticker_symbol=ticker_symbol,
        active_tab_identity=matrix_tab_identity(active_tab.matrix_input) if active_tab is not None else None,
        tabs=tuple(_cached_matrix_tab_from_state(tab) for tab in tabs),
        matrix_mode=DEFAULT_MATRIX_MODE,
    )


def _cached_matrix_tab_from_state(tab: MatrixTabState) -> CachedMatrixTab:
    matrix_input = tab.matrix_input
    row_strikes = _cache_row_strikes(tab.row_strikes)
    widths = tuple(
        width
        for width in matrix_input.widths
        if is_supported_spread_width(width) and width <= MAX_MATRIX_STRIKE_WIDTH
    )
    if not widths:
        widths = discovered_matrix_widths(row_strikes)
    selected_cell_key = _cache_cell_key(tab.selected_cell_key)
    if selected_cell_key is not None and _selected_cell_width(selected_cell_key) not in widths:
        selected_cell_key = None
    return CachedMatrixTab(
        raw_expiration=matrix_input.raw_expiration,
        side=matrix_input.side,
        low_strike=matrix_input.low_strike,
        high_strike=matrix_input.high_strike,
        widths=widths,
        selected_cell_key=selected_cell_key,
        label=tab.label,
        row_strikes=row_strikes,
    )


def matrix_workspace_to_dict(workspace: CachedMatrixWorkspace) -> dict[str, object]:
    matrix_mode = normalize_matrix_mode(workspace.matrix_mode)
    return {
        "version": MATRIX_CACHE_VERSION,
        "matrix_mode": matrix_mode,
        "ticker_symbol": workspace.ticker_symbol,
        "active_tab_identity": list(workspace.active_tab_identity)
        if workspace.active_tab_identity is not None
        else None,
        "tabs": [
            {
                "raw_expiration": tab.raw_expiration,
                "view_type": tab.side,
                "low_strike": tab.low_strike,
                "high_strike": tab.high_strike,
                "widths": list(
                    width
                    for width in tab.widths
                    if is_supported_spread_width(width) and width <= MAX_MATRIX_STRIKE_WIDTH
                ),
                "selected_cell_key": list(tab.selected_cell_key)
                if _cache_cell_key(tab.selected_cell_key) is not None
                and _selected_cell_width(tab.selected_cell_key) <= MAX_MATRIX_STRIKE_WIDTH
                and _selected_cell_width(tab.selected_cell_key) in tab.widths
                else None,
                "label": tab.label,
                "row_strikes": list(_cache_row_strikes(tab.row_strikes)),
            }
            for tab in workspace.tabs
        ],
    }


def matrix_workspace_from_mapping(mapping: Mapping[str, object]) -> CachedMatrixWorkspace | None:
    if mapping.get("version") != MATRIX_CACHE_VERSION:
        return None
    ticker_symbol = _cache_string(mapping.get("ticker_symbol")).upper()
    if not ticker_symbol:
        return None
    try:
        matrix_mode = normalize_matrix_mode(_cache_string(mapping.get("matrix_mode")) or MATRIX_MODE_REGULAR)
    except ValueError:
        return None
    raw_tabs = mapping.get("tabs")
    if not isinstance(raw_tabs, SequenceABC) or isinstance(raw_tabs, (str, bytes)) or not raw_tabs:
        return None

    tabs: list[CachedMatrixTab] = []
    for raw_tab in raw_tabs:
        if not isinstance(raw_tab, MappingABC):
            return None
        parsed_tab = _cached_matrix_tab_from_mapping(raw_tab, matrix_mode=matrix_mode)
        if parsed_tab is None:
            return None
        if any(
            cached_tab.raw_expiration == parsed_tab.raw_expiration
            and (
                cached_tab.side == parsed_tab.side
                or bool(
                    set(matrix_view_rights(cached_tab.side))
                    & set(matrix_view_rights(parsed_tab.side))
                )
            )
            for cached_tab in tabs
        ):
            return None
        tabs.append(parsed_tab)
    if len(tabs) > MAX_MATRIX_TABS:
        return None

    active_identity = _cache_active_tab_identity(mapping.get("active_tab_identity"), ticker_symbol)
    tab_identities = {
        (ticker_symbol, tab.raw_expiration, tab.side)
        for tab in tabs
    }
    if active_identity is not None and active_identity not in tab_identities:
        return None
    return CachedMatrixWorkspace(
        ticker_symbol=ticker_symbol,
        tabs=tuple(tabs),
        active_tab_identity=active_identity,
        matrix_mode=matrix_mode,
    )


def _cached_matrix_tab_from_mapping(
    mapping: Mapping[str, object],
    *,
    matrix_mode: str = MATRIX_MODE_REGULAR,
) -> CachedMatrixTab | None:
    try:
        normalize_matrix_mode(matrix_mode)
    except ValueError:
        return None
    raw_expiration = _cache_string(mapping.get("raw_expiration"))
    if not raw_expiration:
        return None
    try:
        raw_view = _cache_string(mapping.get("view_type")) or _cache_string(mapping.get("side"))
        side = normalize_matrix_view(raw_view)
        low_strike = float(mapping.get("low_strike"))
        high_strike = float(mapping.get("high_strike"))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (low_strike, high_strike)):
        return None

    raw_widths = mapping.get("widths")
    if not isinstance(raw_widths, SequenceABC) or isinstance(raw_widths, (str, bytes)):
        return None
    widths: list[float] = []
    for raw_width in raw_widths:
        try:
            width = _round_key(float(raw_width))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(width) or width <= 0:
            return None
        if not is_supported_spread_width(width) or width > MAX_MATRIX_STRIKE_WIDTH:
            continue
        if width not in widths:
            widths.append(width)
    widths = sorted(widths)
    row_strikes = _cache_row_strikes(
        mapping.get("row_strikes"),
        fallback=_fallback_cached_row_strikes(low_strike, high_strike),
    )
    if not widths:
        widths = list(discovered_matrix_widths(row_strikes))
    if not widths:
        return None

    selected_cell_key = _cache_cell_key(mapping.get("selected_cell_key"))
    if side == MATRIX_VIEW_BOTH and selected_cell_key is not None and len(selected_cell_key) != 3:
        selected_cell_key = None
    if (
        side != MATRIX_VIEW_BOTH
        and selected_cell_key is not None
        and len(selected_cell_key) == 3
        and selected_cell_key[0] != side
    ):
        selected_cell_key = None
    if selected_cell_key is not None and _selected_cell_width(selected_cell_key) not in widths:
        selected_cell_key = None
    return CachedMatrixTab(
        raw_expiration=raw_expiration,
        side=side,
        low_strike=_round_key(low_strike),
        high_strike=_round_key(high_strike),
        widths=tuple(widths),
        selected_cell_key=selected_cell_key,
        label=_cache_string(mapping.get("label")),
        row_strikes=row_strikes,
    )


def _cache_active_tab_identity(value: object, ticker_symbol: str) -> tuple[str, str, str] | None:
    if value is None:
        return None
    if not isinstance(value, SequenceABC) or isinstance(value, (str, bytes)) or len(value) != 3:
        return None
    cached_ticker = _cache_string(value[0]).upper()
    raw_expiration = _cache_string(value[1])
    try:
        side = normalize_matrix_view(_cache_string(value[2]))
    except ValueError:
        return None
    if cached_ticker != ticker_symbol or not raw_expiration:
        return None
    return (cached_ticker, raw_expiration, side)


def _cache_cell_key(value: object) -> SelectedMatrixCellKey | None:
    if value is None:
        return None
    if not isinstance(value, SequenceABC) or isinstance(value, (str, bytes)) or len(value) not in {2, 3}:
        return None
    try:
        offset = 1 if len(value) == 3 else 0
        right = normalize_right(str(value[0])) if offset else None
        lower_strike = _round_key(float(value[offset]))
        width = _round_key(float(value[offset + 1]))
    except (TypeError, ValueError):
        return None
    if (
        not is_supported_matrix_strike(lower_strike)
        or not is_supported_matrix_strike(lower_strike + width)
        or not is_supported_spread_width(width)
    ):
        return None
    if right is not None:
        return (right, lower_strike, width)
    return (lower_strike, width)


def _selected_cell_width(
    cell_key: SelectedMatrixCellKey,
) -> float:
    return _round_key(cell_key[-1])


def _cache_row_strikes(
    value: object,
    *,
    fallback: Sequence[float] = (),
) -> tuple[float, ...]:
    if not isinstance(value, SequenceABC) or isinstance(value, (str, bytes)):
        return _cache_row_strikes(fallback)
    strikes: list[float] = []
    for raw_strike in value:
        try:
            strike = _round_key(float(raw_strike))
        except (TypeError, ValueError):
            return _cache_row_strikes(fallback)
        if not math.isfinite(strike):
            return _cache_row_strikes(fallback)
        if strike not in strikes:
            strikes.append(strike)
    if not strikes and fallback:
        return _cache_row_strikes(fallback)
    # A nonempty cache containing only excluded strikes should stay empty,
    # rather than inventing different rows from its range boundaries.
    return tuple(strike for strike in sorted(strikes) if is_supported_matrix_strike(strike))


def _fallback_cached_row_strikes(low_strike: float, high_strike: float) -> tuple[float, ...]:
    low = _round_key(min(low_strike, high_strike))
    high = _round_key(max(low_strike, high_strike))
    if low == high:
        return (low,)
    if float(low).is_integer() and float(high).is_integer() and (high - low) <= 25:
        return tuple(float(strike) for strike in range(int(low), int(high) + 1))
    return (low, high)


def _cache_string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def load_matrix_workspace_cache(path: Path | None = None) -> CachedMatrixWorkspace | None:
    config_path = path or matrix_workspace_cache_path()
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, MappingABC):
        return None
    return matrix_workspace_from_mapping(payload)


def save_matrix_workspace_cache(
    workspace: CachedMatrixWorkspace,
    path: Path | None = None,
) -> None:
    config_path = path or matrix_workspace_cache_path()
    _atomic_write_json(config_path, matrix_workspace_to_dict(workspace))


def clear_matrix_workspace_cache(path: Path | None = None) -> None:
    config_path = path or matrix_workspace_cache_path()
    try:
        config_path.unlink()
    except FileNotFoundError:
        return


def selected_matrix_widths(
    width_states: Mapping[float, bool],
    *,
    matrix_mode: str | None = None,
) -> list[float]:
    if matrix_mode is not None:
        normalize_matrix_mode(matrix_mode)
    return [
        band
        for band in MATRIX_WIDTH_BANDS
        if bool(width_states.get(band, band == MATRIX_WIDTH_BANDS[0]))
    ]


def matrix_width_band(width: object) -> float | None:
    try:
        numeric_width = _round_key(float(width))
    except (TypeError, ValueError):
        return None
    if not is_supported_spread_width(numeric_width):
        return None
    for band in MATRIX_WIDTH_BANDS:
        if band - 1.0 < numeric_width <= band:
            return band
    return None


def matrix_width_bands(widths: Sequence[float]) -> tuple[float, ...]:
    present = {
        band
        for width in widths
        if (band := matrix_width_band(width)) is not None
    }
    return tuple(band for band in MATRIX_WIDTH_BANDS if band in present)


def discovered_matrix_widths(
    row_strikes: Sequence[float],
    enabled_bands: Sequence[float] | None = None,
) -> tuple[float, ...]:
    ordered = sorted({
        _round_key(strike) for strike in row_strikes if is_supported_matrix_strike(strike)
    })
    enabled = (
        set(MATRIX_WIDTH_BANDS)
        if enabled_bands is None
        else set(matrix_width_bands(tuple(enabled_bands)))
    )
    widths: set[float] = set()
    for lower_index, lower_strike in enumerate(ordered):
        for upper_strike in ordered[lower_index + 1 :]:
            width = _round_key(upper_strike - lower_strike)
            if width > MAX_MATRIX_STRIKE_WIDTH:
                break
            band = matrix_width_band(width)
            if band in enabled:
                widths.add(width)
    return tuple(sorted(widths))


def modified_matrix_widths(row_strikes: Sequence[float]) -> tuple[float, ...]:
    """Compatibility alias for callers using the former mode-specific name."""
    return discovered_matrix_widths(row_strikes)


def discovered_matrix_view_widths(
    option_records: Sequence[OptionContractRecord],
    view: str,
    low_strike: float,
    high_strike: float,
) -> tuple[float, ...]:
    low = _round_key(min(low_strike, high_strike))
    high = _round_key(max(low_strike, high_strike))
    discovered: set[float] = set()
    for right in matrix_view_rights(view):
        strikes = tuple(
            sorted(
                {
                    _round_key(record.strike)
                    for record in option_records
                    if record.right == right and low <= _round_key(record.strike) <= high
                }
            )
        )
        discovered.update(discovered_matrix_widths(strikes))
    return tuple(sorted(discovered))


def _matrix_input_width_bands(matrix_input: MatrixInputState) -> tuple[float, ...]:
    if matrix_input.width_bands is not None:
        return matrix_width_bands(matrix_input.width_bands)
    cached_bands = matrix_width_bands(matrix_input.widths)
    return cached_bands or MATRIX_WIDTH_BANDS


def parse_matrix_input_state(
    *,
    display_expiration: str,
    raw_expiration: str,
    side: str,
    low_strike: str,
    high_strike: str,
    width_states: Mapping[float, bool],
    ticker_symbol: str = "",
    matrix_mode: str | None = None,
) -> MatrixInputState:
    low = _round_key(float(low_strike.strip()))
    high = _round_key(float(high_strike.strip()))
    if high < low:
        low, high = high, low
    resolved_mode = normalize_matrix_mode(matrix_mode)
    selected_bands = tuple(selected_matrix_widths(width_states, matrix_mode=resolved_mode))
    return MatrixInputState(
        display_expiration=display_expiration,
        raw_expiration=raw_expiration,
        side=normalize_matrix_view(side),
        low_strike=low,
        high_strike=high,
        widths=selected_bands,
        ticker_symbol=(ticker_symbol or "").strip().upper(),
        matrix_mode=resolved_mode,
        width_bands=selected_bands,
    )


def matrix_tab_key(matrix_input: MatrixInputState) -> tuple[str, str, float, float]:
    return (
        matrix_input.raw_expiration,
        matrix_input.side,
        _round_key(matrix_input.low_strike),
        _round_key(matrix_input.high_strike),
    )


def matrix_tab_identity(matrix_input: MatrixInputState) -> tuple[str, str, str]:
    return (
        matrix_input.ticker_symbol,
        matrix_input.raw_expiration,
        matrix_input.side,
    )


def matrix_tab_views_conflict(
    existing: MatrixInputState,
    candidate: MatrixInputState,
) -> bool:
    if existing.ticker_symbol != candidate.ticker_symbol:
        return False
    if existing.raw_expiration != candidate.raw_expiration:
        return False
    existing_view = normalize_matrix_view(existing.side)
    candidate_view = normalize_matrix_view(candidate.side)
    if existing_view == candidate_view:
        return False
    return bool(set(matrix_view_rights(existing_view)) & set(matrix_view_rights(candidate_view)))


def matrix_tab_label(matrix_input: MatrixInputState) -> str:
    raw = matrix_input.raw_expiration
    expiry_text = matrix_input.display_expiration or raw
    if len(raw) >= 8 and raw[:8].isdigit():
        year = raw[2:4]
        month = int(raw[4:6])
        day = int(raw[6:8])
        month_text = MONTH_ABBREVIATIONS[month] if 1 <= month <= 12 else raw[4:6]
        expiry_text = f"{month_text}{day:02d}'{year}"
    side_text = {
        RIGHT_CALL: "C",
        RIGHT_PUT: "P",
        MATRIX_VIEW_BOTH: "C/P",
    }[normalize_matrix_view(matrix_input.side)]
    return (
        f"{expiry_text} {side_text} "
        f"{format_strike(matrix_input.low_strike)}-{format_strike(matrix_input.high_strike)}"
    )


def has_duplicate_matrix_tab(
    existing_inputs: Sequence[MatrixInputState],
    matrix_input: MatrixInputState,
) -> bool:
    candidate_identity = matrix_tab_identity(matrix_input)
    return any(matrix_tab_identity(existing_input) == candidate_identity for existing_input in existing_inputs)


def matrix_tab_limit_reached(tab_count: int) -> bool:
    return tab_count >= MAX_MATRIX_TABS


def selection_request_key(selection: VerticalSelection) -> tuple[tuple[str, int], ...]:
    return tuple((leg.action, leg.contract.con_id) for leg in selection.legs)


def matrix_quote_target_selections(plan: MatrixSelectionPlan | None) -> list[VerticalSelection]:
    if plan is None:
        return []
    return flatten_matrix_context(plan)


def leg_quote_ids(selections: Sequence[VerticalSelection]) -> set[int]:
    return {
        leg.contract.con_id
        for selection in selections
        for leg in selection.legs
    }


def subscription_target_key(selections: Sequence[VerticalSelection]) -> tuple[tuple[tuple[str, int], ...], ...]:
    return tuple(sorted(selection_request_key(selection) for selection in selections))


def union_matrix_quote_target_selections(
    plans: Sequence[MatrixSelectionPlan],
    extra_plan: MatrixSelectionPlan | None = None,
) -> list[VerticalSelection]:
    selections: list[VerticalSelection] = []
    seen: set[tuple[tuple[str, int], ...]] = set()
    for plan in [*plans, *([] if extra_plan is None else [extra_plan])]:
        for selection in matrix_quote_target_selections(plan):
            key = selection_request_key(selection)
            if key in seen:
                continue
            seen.add(key)
            selections.append(selection)
    return selections


def matrix_plans_for_tab(tab: MatrixTabState) -> list[MatrixSelectionPlan]:
    if tab.side_states:
        return [
            tab.side_states[right].matrix_plan
            for right in (RIGHT_PUT, RIGHT_CALL)
            if right in tab.side_states
        ]
    if normalize_matrix_view(tab.matrix_input.side) == MATRIX_VIEW_BOTH:
        return []
    return [tab.matrix_plan]


def union_matrix_tab_quote_target_selections(
    tabs: Sequence[MatrixTabState],
    *,
    extra_plans: Sequence[MatrixSelectionPlan] = (),
) -> list[VerticalSelection]:
    plans = [
        plan
        for tab in tabs
        if not tab.is_stale
        for plan in matrix_plans_for_tab(tab)
    ]
    selections = union_matrix_quote_target_selections(plans)
    if not extra_plans:
        return selections
    seen = {selection_request_key(selection) for selection in selections}
    for selection in union_matrix_quote_target_selections(list(extra_plans)):
        key = selection_request_key(selection)
        if key in seen:
            continue
        seen.add(key)
        selections.append(selection)
    return selections


def flatten_matrix_tabs_for_recording(
    tabs: Sequence[MatrixTabState],
) -> list[RecorderMatrixTabView]:
    views: list[RecorderMatrixTabView] = []
    for tab in tabs:
        if tab.side_states:
            for right in (RIGHT_PUT, RIGHT_CALL):
                side_state = tab.side_states.get(right)
                if side_state is None:
                    continue
                side_input = replace(tab.matrix_input, side=right)
                views.append(
                    RecorderMatrixTabView(
                        tab_id=tab.tab_id,
                        label=matrix_tab_label(side_input),
                        matrix_input=side_input,
                        row_strikes=side_state.row_strikes,
                        matrix_plan=side_state.matrix_plan,
                        leg_quotes=tab.leg_quotes,
                        leg_quote_ids=tab.leg_quote_ids,
                        is_stale=tab.is_stale,
                        underlying_price_hint=tab.underlying_price_hint,
                    )
                )
            continue

        if normalize_matrix_view(tab.matrix_input.side) == MATRIX_VIEW_BOTH:
            continue
        views.append(
            RecorderMatrixTabView(
                tab_id=tab.tab_id,
                label=tab.label,
                matrix_input=tab.matrix_input,
                row_strikes=tab.row_strikes,
                matrix_plan=tab.matrix_plan,
                leg_quotes=tab.leg_quotes,
                leg_quote_ids=tab.leg_quote_ids,
                is_stale=tab.is_stale,
                underlying_price_hint=tab.underlying_price_hint,
            )
        )
    return views


def matrix_quote_target_counts_for_plans(
    plans: Sequence[MatrixSelectionPlan],
    extra_plan: MatrixSelectionPlan | None = None,
) -> MarketDataQuoteCounts | None:
    selections = union_matrix_quote_target_selections(plans, extra_plan=extra_plan)
    if not selections:
        return None
    return market_data_quote_counts(selections)


def market_data_quote_counts(selections: Sequence[VerticalSelection]) -> MarketDataQuoteCounts:
    combo_keys = {selection_request_key(selection) for selection in selections}
    leg_ids = {
        leg.contract.con_id
        for selection in selections
        for leg in selection.legs
    }
    return MarketDataQuoteCounts(combo_quotes=len(combo_keys), leg_quotes=len(leg_ids))


def market_data_quote_status_for_selections(
    current_selections: Sequence[VerticalSelection],
    preview_selections: Sequence[VerticalSelection] | None = None,
) -> MarketDataQuoteStatus:
    current_combo_keys, current_leg_ids = _market_data_quote_sets(current_selections)
    if preview_selections is None:
        return _market_data_quote_status_from_sets(
            current_combo_keys,
            current_leg_ids,
        )

    preview_combo_keys, preview_leg_ids = _market_data_quote_sets(preview_selections)
    return _market_data_quote_status_from_sets(
        current_combo_keys,
        current_leg_ids,
        current_combo_keys | preview_combo_keys,
        current_leg_ids | preview_leg_ids,
        preview_action="loaded",
    )


def matrix_quote_status_for_plans(
    plans: Sequence[MatrixSelectionPlan],
    extra_plan: MatrixSelectionPlan | None = None,
    *,
    include_preview: bool = True,
) -> MarketDataQuoteStatus:
    current_selections = union_matrix_quote_target_selections(plans)
    preview_selections = (
        matrix_quote_target_selections(extra_plan)
        if include_preview and extra_plan is not None
        else None
    )
    return market_data_quote_status_for_selections(current_selections, preview_selections)


def matrix_quote_status_for_plan_update(
    plans: Sequence[MatrixSelectionPlan],
    replaced_plan: MatrixSelectionPlan,
    replacement_plan: MatrixSelectionPlan,
) -> MarketDataQuoteStatus:
    current_selections = union_matrix_quote_target_selections(plans)
    replacement_plans: list[MatrixSelectionPlan] = []
    replaced = False
    for plan in plans:
        if not replaced and plan is replaced_plan:
            replacement_plans.append(replacement_plan)
            replaced = True
        else:
            replacement_plans.append(plan)
    if not replaced:
        replacement_plans.append(replacement_plan)

    current_combo_keys, current_leg_ids = _market_data_quote_sets(current_selections)
    preview_combo_keys, preview_leg_ids = _market_data_quote_sets(
        union_matrix_quote_target_selections(replacement_plans)
    )
    return _market_data_quote_status_from_sets(
        current_combo_keys,
        current_leg_ids,
        preview_combo_keys,
        preview_leg_ids,
        preview_action="updated",
    )


def format_market_data_quote_status(status: MarketDataQuoteStatus | None) -> str:
    if status is None or (status.current is None and status.preview_total is None):
        return "Live option streams: -- | Synthetic spreads monitored: --"

    current = status.current or MarketDataQuoteCounts(combo_quotes=0, leg_quotes=0)
    underlying_streams = 1 if current.leg_quotes else 0
    text = (
        f"Live option streams: {current.leg_quotes} legs + {underlying_streams} underlying | "
        f"Synthetic spreads monitored: {current.combo_quotes}"
    )
    if status.preview_total is None:
        return text

    added = status.added or MarketDataQuoteCounts(combo_quotes=0, leg_quotes=0)
    removed = status.removed
    removed_text = ""
    if removed is not None:
        removed_text = f", -{removed.leg_quotes} streams -{removed.combo_quotes} spreads"
    return (
        f"{text} | If {status.preview_action}: {status.preview_total.leg_quotes} option streams, "
        f"{status.preview_total.combo_quotes} synthetic spreads "
        f"(+{added.leg_quotes} streams +{added.combo_quotes} spreads{removed_text})"
    )


def format_compact_market_data_quote_status(status: MarketDataQuoteStatus | None) -> str:
    """Format the header's live and staged quote footprint without changing its semantics."""
    if status is None or (status.current is None and status.preview_total is None):
        return "LIVE  --"

    current = status.current or MarketDataQuoteCounts(combo_quotes=0, leg_quotes=0)
    current_underlyings = 1 if current.leg_quotes else 0
    text = f"LIVE  {current.leg_quotes}L + {current_underlyings}U • {current.combo_quotes}S"
    if status.preview_total is None:
        return text

    preview_underlyings = 1 if status.preview_total.leg_quotes else 0
    added = status.added or MarketDataQuoteCounts(combo_quotes=0, leg_quotes=0)
    removed = status.removed or MarketDataQuoteCounts(combo_quotes=0, leg_quotes=0)
    added_underlyings = max(0, preview_underlyings - current_underlyings)
    removed_underlyings = max(0, current_underlyings - preview_underlyings)
    has_additions = bool(added.leg_quotes or added.combo_quotes or added_underlyings)
    has_removals = bool(removed.leg_quotes or removed.combo_quotes or removed_underlyings)
    if not has_additions and not has_removals:
        return text

    added_text = f"+{added.leg_quotes}L +{added_underlyings}U +{added.combo_quotes}S"
    removed_text = f"−{removed.leg_quotes}L −{removed_underlyings}U −{removed.combo_quotes}S"
    if has_additions and has_removals:
        pending = f"PENDING  ADD {added_text}   REMOVE {removed_text}"
    elif has_additions:
        pending = f"PENDING  {added_text}"
    else:
        pending = f"PENDING  REMOVE {removed_text}"
    return f"{text}     {pending}"


def _market_data_quote_sets(
    selections: Sequence[VerticalSelection],
) -> tuple[set[tuple[tuple[str, int], ...]], set[int]]:
    combo_keys = {selection_request_key(selection) for selection in selections}
    leg_ids = {
        leg.contract.con_id
        for selection in selections
        for leg in selection.legs
    }
    return combo_keys, leg_ids


def _market_data_quote_status_from_sets(
    current_combo_keys: set[tuple[tuple[str, int], ...]],
    current_leg_ids: set[int],
    preview_combo_keys: set[tuple[tuple[str, int], ...]] | None = None,
    preview_leg_ids: set[int] | None = None,
    *,
    preview_action: str = "loaded",
) -> MarketDataQuoteStatus:
    current_counts = (
        MarketDataQuoteCounts(combo_quotes=len(current_combo_keys), leg_quotes=len(current_leg_ids))
        if current_combo_keys or current_leg_ids
        else None
    )
    if preview_combo_keys is None or preview_leg_ids is None:
        return MarketDataQuoteStatus(current=current_counts)

    return MarketDataQuoteStatus(
        current=current_counts,
        preview_total=MarketDataQuoteCounts(
            combo_quotes=len(preview_combo_keys),
            leg_quotes=len(preview_leg_ids),
        ),
        added=MarketDataQuoteCounts(
            combo_quotes=len(preview_combo_keys - current_combo_keys),
            leg_quotes=len(preview_leg_ids - current_leg_ids),
        ),
        removed=(
            MarketDataQuoteCounts(
                combo_quotes=len(current_combo_keys - preview_combo_keys),
                leg_quotes=len(current_leg_ids - preview_leg_ids),
            )
            if preview_action == "updated"
            else None
        ),
        preview_action=preview_action,
    )


def build_matrix_input_plan(
    option_records: Sequence[OptionContractRecord],
    matrix_input: MatrixInputState,
    *,
    underlying_price: float | None = None,
) -> MatrixInputPlan | None:
    side_records = [
        record
        for record in option_records
        if record.right == matrix_input.side
    ]
    available_strikes = sorted({_round_key(record.strike) for record in side_records})
    row_strikes = tuple(
        strike
        for strike in available_strikes
        if matrix_input.low_strike <= strike <= matrix_input.high_strike
        and is_supported_matrix_strike(strike)
    )
    if not row_strikes:
        return None
    widths = discovered_matrix_widths(
        row_strikes,
        enabled_bands=_matrix_input_width_bands(matrix_input),
    )
    if not widths:
        return None

    # Build each width independently so its visible lower-strike candidates
    # require both legs to remain inside the selected range. Context neighbors
    # remain available outside the visible range for endpoint calibration.
    visible: dict[tuple[float, float], VerticalSelection] = {}
    context_by_width: dict[float, tuple[VerticalSelection, ...]] = {}
    resolved_widths: list[float] = []
    low = _round_key(matrix_input.low_strike)
    high = _round_key(matrix_input.high_strike)
    for raw_width in widths:
        width = _round_key(raw_width)
        visible_lower_strikes = tuple(
            strike
            for strike in row_strikes
            if _round_key(strike + width) <= high
            and is_supported_matrix_strike(strike + width)
        )
        partial_plan = build_matrix_selection_plan(
            side_records,
            visible_lower_strikes,
            (width,),
        )
        width_visible = {
            cell_key: selection
            for cell_key, selection in partial_plan.visible.items()
            if low <= _round_key(selection.pair.lower.strike)
            and _round_key(selection.pair.upper.strike) <= high
            and is_supported_matrix_pair(selection.pair)
        }
        if not width_visible:
            continue
        resolved_widths.append(width)
        visible.update(width_visible)
        context_by_width[width] = partial_plan.context_by_width.get(width, ())

    if not resolved_widths:
        return None
    plan = MatrixSelectionPlan(
        visible=visible,
        context_by_width=context_by_width,
    )
    return MatrixInputPlan(
        row_strikes=row_strikes,
        plan=plan,
        widths=tuple(resolved_widths),
    )


def build_matrix_view_plan(
    option_records: Sequence[OptionContractRecord],
    matrix_input: MatrixInputState,
    *,
    underlying_price: float | None = None,
) -> MatrixViewInputPlan | None:
    required_rights = matrix_view_rights(matrix_input.side)
    side_plans: dict[str, MatrixInputPlan] = {}
    for right in required_rights:
        side_input = replace(matrix_input, side=right)
        side_plan = build_matrix_input_plan(
            option_records,
            side_input,
            underlying_price=underlying_price,
        )
        if side_plan is not None:
            side_plans[right] = side_plan
    if not side_plans or (
        normalize_matrix_view(matrix_input.side) == MATRIX_VIEW_BOTH
        and set(side_plans) != set(required_rights)
    ):
        return None

    row_strikes = tuple(sorted({strike for plan in side_plans.values() for strike in plan.row_strikes}))
    widths = tuple(sorted({width for plan in side_plans.values() for width in plan.widths}))
    return MatrixViewInputPlan(
        row_strikes=row_strikes,
        side_plans=side_plans,
        widths=widths,
    )


def preview_market_data_quote_counts(
    option_records: Sequence[OptionContractRecord],
    matrix_input: MatrixInputState,
    *,
    underlying_price: float | None = None,
) -> MarketDataQuoteCounts | None:
    view_plan = build_matrix_view_plan(
        option_records,
        matrix_input,
        underlying_price=underlying_price,
    )
    if view_plan is None:
        return None
    selections = union_matrix_quote_target_selections(
        [side_plan.plan for side_plan in view_plan.side_plans.values()]
    )
    return market_data_quote_counts(selections) if selections else None


def seed_cached_matrix_quotes(
    selections: Sequence[VerticalSelection],
    leg_quote_cache: Mapping[int, QuoteSnapshot],
) -> dict[int, QuoteSnapshot]:
    leg_quotes: dict[int, QuoteSnapshot] = {}
    for selection in selections:
        for leg in selection.legs:
            leg_quote = leg_quote_cache.get(leg.contract.con_id)
            if leg_quote is not None:
                leg_quotes[leg.contract.con_id] = leg_quote
    return leg_quotes


def _positive_finite_price(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0.0 else None


def underlying_reference_price(quote: QuoteSnapshot | None) -> float | None:
    if quote is None:
        return None
    for candidate in (quote.mark, quote.mid, quote.last):
        price = _positive_finite_price(candidate)
        if price is not None:
            return price
    return None


def matrix_tab_underlying_reference_price(
    tab: MatrixTabState | RecorderMatrixTabView | None,
    quote: QuoteSnapshot | None,
) -> float | None:
    live_price = underlying_reference_price(quote)
    if live_price is not None:
        return live_price
    return _positive_finite_price(getattr(tab, "underlying_price_hint", None))


def selection_has_itm_strike(
    selection: VerticalSelection,
    underlying_price: float | None,
) -> bool:
    if underlying_price is None:
        return False
    pair = selection.pair
    strikes = (pair.lower.strike, pair.upper.strike)
    if pair.right == RIGHT_CALL:
        return any(strike < underlying_price for strike in strikes)
    if pair.right == RIGHT_PUT:
        return any(strike > underlying_price for strike in strikes)
    return False


def build_aggregate_ranked_signals(
    tabs: Sequence[MatrixTabState],
    underlying_price: float | None,
) -> list[RankedMatrixSignalRow]:
    live_price = _positive_finite_price(underlying_price)
    rows: list[RankedMatrixSignalRow] = []
    for tab in tabs:
        tab_price = live_price
        if tab_price is None:
            tab_price = _positive_finite_price(tab.underlying_price_hint)
        # A tab without either source cannot prove that a candidate is OTM, so
        # it remains absent from the actionable aggregate while staying visible
        # and recordable in the matrix.
        if tab_price is None:
            continue
        for item in tab.ranked_signals:
            if not is_supported_matrix_pair(item.selection.pair):
                continue
            if selection_has_itm_strike(item.selection, tab_price):
                continue
            rows.append(
                RankedMatrixSignalRow(
                    rank=0,
                    tab_id=tab.tab_id,
                    tab_label=tab.label,
                    lower_strike=item.lower_strike,
                    width=item.width,
                    pair_key=item.pair_key,
                    selection=item.selection,
                    snapshot=item.snapshot,
                    signal=item.signal,
                )
            )

    kind_priority = {SIGNAL_KIND_ANOMALY: 0, SIGNAL_KIND_OPPORTUNITY: 1}
    rows.sort(
        key=lambda item: (
            kind_priority.get(item.signal.kind, 2),
            -item.signal.edge,
            item.tab_label,
            item.pair_key,
            item.width,
        )
    )
    return [
        RankedMatrixSignalRow(
            rank=index,
            tab_id=item.tab_id,
            tab_label=item.tab_label,
            lower_strike=item.lower_strike,
            width=item.width,
            pair_key=item.pair_key,
            selection=item.selection,
            snapshot=item.snapshot,
            signal=item.signal,
        )
        for index, item in enumerate(rows, start=1)
    ]


def next_rank_sort_state(current: RankSortState, column: str) -> RankSortState:
    if column not in RANK_SORT_COLUMNS:
        return current
    if current.column != column or current.direction == RANK_SORT_NONE:
        return RankSortState(column=column, direction=RANK_SORT_ASC)
    if current.direction == RANK_SORT_ASC:
        return RankSortState(column=column, direction=RANK_SORT_DESC)
    return RankSortState()


def sorted_ranked_signals(
    ranked_signals: Sequence[RankedMatrixSignal | RankedMatrixSignalRow],
    sort_state: RankSortState,
) -> list[RankedMatrixSignal | RankedMatrixSignalRow]:
    if sort_state.column is None or sort_state.direction == RANK_SORT_NONE:
        return list(ranked_signals)

    ordered_by_default_rank = sorted(ranked_signals, key=lambda item: item.rank)
    reverse = sort_state.direction == RANK_SORT_DESC
    return sorted(
        ordered_by_default_rank,
        key=lambda item: _rank_sort_value(item, sort_state.column or "rank"),
        reverse=reverse,
    )


def _rank_sort_value(item: RankedMatrixSignal | RankedMatrixSignalRow, column: str) -> int | float | str:
    if column == "rank":
        return item.rank
    if column == "tab":
        return getattr(item, "tab_label", "")
    if column == "side":
        return item.selection.pair.right
    if column == "combo":
        return item.pair_key
    if column == "width":
        return item.width
    if column == "signal":
        return item.signal.signal
    if column == "price":
        return item.signal.price
    if column == "edge":
        return item.signal.edge
    return item.rank


def fair_value_edge_curve_values(snapshot: MonitoredSideSnapshot) -> FairValueEdgeCurveValues | None:
    values = (
        snapshot.fair_value_edge_curve_mid,
        snapshot.fair_value_edge_curve_edge,
        snapshot.fair_value_edge_curve_buy_endpoint,
        snapshot.fair_value_edge_curve_sell_endpoint,
    )
    if any(value is None for value in values):
        return None
    return FairValueEdgeCurveValues(
        fair_value_mid=values[0],
        edge=values[1],
        buy_endpoint=values[2],
        sell_endpoint=values[3],
    )


def fair_value_unavailable_message(
    snapshot: MonitoredSideSnapshot,
    *,
    underlying_price: float | None,
    min_points: float,
) -> str | None:
    """Explain why the selected-spread fair-value graphic is unavailable."""
    if fair_value_edge_curve_values(snapshot) is not None:
        return None
    if _positive_finite_price(underlying_price) is None:
        return "FV waiting: no usable underlying price"
    fit_points = max(0, int(snapshot.fair_value_edge_curve_fit_points))
    if fit_points == 0:
        return "FV waiting: no usable atomic observations"
    return "FV unavailable: incomplete atomic fair-value coverage"


def detail_display_values(snapshot: MonitoredSideSnapshot) -> list[float]:
    values: list[float] = []
    for value in (
        snapshot.anomaly_price,
        snapshot.synthetic_quote.bid if snapshot.synthetic_quote is not None else None,
        snapshot.synthetic_mid,
        snapshot.synthetic_quote.ask if snapshot.synthetic_quote is not None else None,
    ):
        if value is not None:
            values.append(value)

    fair_value = fair_value_edge_curve_values(snapshot)
    if fair_value is not None:
        values.extend(
            [
                fair_value.fair_value_mid,
                fair_value.buy_endpoint,
                fair_value.sell_endpoint,
            ]
        )
    return values


def matrix_liquidity_gauge_states(
    tab: MatrixTabState,
    *,
    minimum_peers: int = LIQUIDITY_GAUGE_MIN_PEERS,
) -> dict[SelectedMatrixCellKey, MatrixLiquidityGaugeState]:
    """Build display-only liquidity states from visible peers on each curve.

    A curve is one option right and one exact spread width. Hidden fair-value
    context never enters the comparison. Bid and ask sizes share one robust
    scale so bar length compares support across cells without losing the
    within-cell bid/ask imbalance.
    """
    entries: list[
        tuple[
            SelectedMatrixCellKey,
            tuple[str, float],
            MonitoredSideSnapshot | None,
        ]
    ] = []
    matrix_view = normalize_matrix_view(tab.matrix_input.side)
    if matrix_view == MATRIX_VIEW_BOTH:
        for right in (RIGHT_CALL, RIGHT_PUT):
            side_state = tab.side_states.get(right)
            if side_state is None:
                continue
            for cell_key, selection in side_state.matrix_plan.visible.items():
                view_key: MatrixViewCellKey = (right, cell_key[0], cell_key[1])
                entries.append(
                    (
                        view_key,
                        (right, _round_key(selection.pair.width)),
                        side_state.matrix_snapshots.get(cell_key),
                    )
                )
    else:
        for cell_key, selection in tab.matrix_plan.visible.items():
            entries.append(
                (
                    cell_key,
                    (selection.pair.right, _round_key(selection.pair.width)),
                    tab.matrix_snapshots.get(cell_key),
                )
            )

    peer_sizes: dict[tuple[str, float], list[float]] = {}
    peer_cells: dict[tuple[str, float], int] = {}
    for _cell_key, curve_key, snapshot in entries:
        if not _liquidity_quote_is_complete(snapshot):
            continue
        sizes = _liquidity_sizes(snapshot)
        if not sizes:
            continue
        peer_cells[curve_key] = peer_cells.get(curve_key, 0) + 1
        peer_sizes.setdefault(curve_key, []).extend(sizes)

    references: dict[tuple[str, float], float] = {}
    for curve_key, sizes in peer_sizes.items():
        if peer_cells.get(curve_key, 0) < max(1, int(minimum_peers)):
            continue
        reference = _percentile(sizes, 0.90)
        if reference <= 0.0:
            positive_sizes = [size for size in sizes if size > 0.0]
            reference = max(positive_sizes, default=0.0)
        if reference > 0.0:
            references[curve_key] = reference

    states: dict[SelectedMatrixCellKey, MatrixLiquidityGaugeState] = {}
    for cell_key, curve_key, snapshot in entries:
        ratio, overflow, crossed = _liquidity_quote_width_state(snapshot)
        peer_count = peer_cells.get(curve_key, 0)
        reference = references.get(curve_key)
        comparable = (
            reference is not None
            and not crossed
            and _liquidity_quote_is_complete(snapshot)
        )
        bid_size = _liquidity_size(
            snapshot.synthetic_quote.bid_size
            if snapshot is not None and snapshot.synthetic_quote is not None
            else None
        )
        ask_size = _liquidity_size(
            snapshot.synthetic_quote.ask_size
            if snapshot is not None and snapshot.synthetic_quote is not None
            else None
        )
        states[cell_key] = MatrixLiquidityGaugeState(
            quote_width_ratio=ratio,
            quote_width_overflow=overflow,
            quote_crossed=crossed,
            bid_support=(
                _relative_liquidity_support(bid_size, reference)
                if comparable and bid_size is not None
                else None
            ),
            ask_support=(
                _relative_liquidity_support(ask_size, reference)
                if comparable and ask_size is not None
                else None
            ),
            size_comparable=comparable,
            size_peer_count=peer_count,
        )
    return states


def compact_matrix_signal_text(text: str) -> str:
    """Keep dense matrix labels clear while reserving a right-edge gauge lane."""
    raw = (text or "").strip()
    exit_match = re.fullmatch(r"EXIT (BUY|SELL)\s+([^\s]+)", raw)
    if exit_match is not None:
        return f"X{exit_match.group(1)[0]}{_compact_price_text(exit_match.group(2))}"
    signal_match = re.fullmatch(r"([BS])\s+([^\s()]+)(?:\s+\([^)]*\))?", raw)
    if signal_match is not None:
        return f"{signal_match.group(1)}{_compact_price_text(signal_match.group(2))}"
    waiting_match = re.fullmatch(r"M\s+([^\s]+)\s+FV\?", raw)
    if waiting_match is not None:
        return f"M{_compact_price_text(waiting_match.group(1))}?"
    neutral_match = re.fullmatch(r"M\s+([^\s]+)", raw)
    if neutral_match is not None:
        return f"M{_compact_price_text(neutral_match.group(1))}"
    return raw


def resolve_matrix_cell_fills(
    selection: VerticalSelection | None,
    *,
    selected: bool,
    is_itm: bool,
    live: bool = True,
    exit_signal: str | None = None,
) -> tuple[str, str]:
    """Return independent COMBO and SIGNAL/PRICE fills for one matrix cell."""
    if selection is None:
        return MISSING_BG, MISSING_BG
    if is_itm:
        base_fill = ITM_SELECTED_BG if selected else ITM_BG
    else:
        base_fill = SELECTED_BG if selected else GRID_BG
    if live and exit_signal in (SIGNAL_BUY, SIGNAL_SELL):
        return base_fill, POSITION_BUY_BG if exit_signal == SIGNAL_BUY else POSITION_SELL_BG
    return base_fill, base_fill


def _compact_price_text(text: str) -> str:
    if text.startswith("0."):
        return text[1:]
    if text.startswith("-0."):
        return "-." + text[3:]
    return text


def _finite_number(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _liquidity_size(value: object) -> float | None:
    numeric = _finite_number(value)
    if numeric is None or numeric < 0.0:
        return None
    return numeric


def _liquidity_sizes(snapshot: MonitoredSideSnapshot | None) -> list[float]:
    if snapshot is None or snapshot.synthetic_quote is None:
        return []
    return [
        size
        for raw_size in (
            snapshot.synthetic_quote.bid_size,
            snapshot.synthetic_quote.ask_size,
        )
        if (size := _liquidity_size(raw_size)) is not None
    ]


def _liquidity_quote_is_complete(snapshot: MonitoredSideSnapshot | None) -> bool:
    if snapshot is None or snapshot.synthetic_quote is None:
        return False
    bid = _finite_number(snapshot.synthetic_quote.bid)
    ask = _finite_number(snapshot.synthetic_quote.ask)
    return bid is not None and ask is not None and ask >= bid and snapshot.width > 0.0


def _liquidity_quote_width_state(
    snapshot: MonitoredSideSnapshot | None,
) -> tuple[float | None, bool, bool]:
    if snapshot is None or snapshot.synthetic_quote is None:
        return None, False, False
    bid = _finite_number(snapshot.synthetic_quote.bid)
    ask = _finite_number(snapshot.synthetic_quote.ask)
    spread_width = _finite_number(snapshot.width)
    if bid is None or ask is None or spread_width is None or spread_width <= 0.0:
        return None, False, False
    if ask < bid:
        return None, False, True
    raw_ratio = (ask - bid) / spread_width
    overflow = raw_ratio > 1.0
    clamped = min(max(raw_ratio, 0.0), 1.0)
    quantized = round(clamped * LIQUIDITY_GAUGE_WIDTH_STEPS) / LIQUIDITY_GAUGE_WIDTH_STEPS
    return quantized, overflow, False


def _percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    if len(ordered) == 1:
        return ordered[0]
    position = min(max(float(probability), 0.0), 1.0) * (len(ordered) - 1)
    lower_index = int(math.floor(position))
    upper_index = int(math.ceil(position))
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    return ordered[lower_index] + ((ordered[upper_index] - ordered[lower_index]) * fraction)


def _relative_liquidity_support(size: float, reference: float) -> float:
    if reference <= 0.0:
        return 1.0 if size > 0.0 else 0.0
    raw_support = min(1.0, math.log1p(size) / math.log1p(reference))
    quantized = round(raw_support * LIQUIDITY_GAUGE_SIZE_STEPS) / LIQUIDITY_GAUGE_SIZE_STEPS
    if size > 0.0:
        quantized = max(1.0 / LIQUIDITY_GAUGE_SIZE_STEPS, quantized)
    return min(max(quantized, 0.0), 1.0)


def matrix_grid_layout(
    canvas_width: float,
    selected_widths: Sequence[float],
    *,
    strike_width: float = 64.0,
    slot_widths: Sequence[float] | None = None,
    minimum_group_width: float = 124.0,
) -> MatrixGridLayout:
    selected = {_round_key(width) for width in selected_widths}
    slots_source = tuple(slot_widths) if slot_widths is not None else DEFAULT_WIDTHS
    if not slots_source:
        slots_source = tuple(selected_widths) if selected_widths else DEFAULT_WIDTHS
    normalized_slots = tuple(_round_key(width) for width in slots_source)
    available_group_width = max(1.0, float(canvas_width) - strike_width)
    group_width = available_group_width / len(normalized_slots)
    if slot_widths is not None:
        group_width = max(float(minimum_group_width), group_width)
    combo_width = POSITION_ICON_LANE_WIDTH + max(
        54.0,
        min(86.0, group_width * 0.38),
    )
    signal_width = max(1.0, group_width - combo_width)
    return MatrixGridLayout(
        strike_width=strike_width,
        group_width=group_width,
        combo_width=combo_width,
        signal_width=signal_width,
        total_width=strike_width + (len(normalized_slots) * group_width),
        slots=tuple(
            MatrixWidthSlot(
                width=width,
                selected=_round_key(width) in selected,
                x=strike_width + (index * group_width),
            )
            for index, width in enumerate(normalized_slots)
        ),
    )


def split_matrix_grid_layout(
    canvas_width: float,
    selected_widths: Sequence[float],
    *,
    strike_width: float = 64.0,
    slot_widths: Sequence[float] | None = None,
    minimum_group_width: float = 124.0,
) -> SplitMatrixGridLayout:
    """Lay out mirrored call/put width groups around one strike ladder."""
    selected = {_round_key(width) for width in selected_widths}
    slots_source = tuple(slot_widths) if slot_widths is not None else DEFAULT_WIDTHS
    if not slots_source:
        slots_source = tuple(selected_widths) if selected_widths else DEFAULT_WIDTHS
    normalized_slots = tuple(sorted({_round_key(width) for width in slots_source}))
    available_group_width = max(1.0, float(canvas_width) - strike_width)
    group_width = available_group_width / max(1, len(normalized_slots) * 2)
    if slot_widths is not None:
        group_width = max(float(minimum_group_width), group_width)
    combo_width = POSITION_ICON_LANE_WIDTH + max(
        54.0,
        min(86.0, group_width * 0.38),
    )
    signal_width = max(1.0, group_width - combo_width)
    strike_x = len(normalized_slots) * group_width
    return SplitMatrixGridLayout(
        strike_x=strike_x,
        strike_width=strike_width,
        group_width=group_width,
        combo_width=combo_width,
        signal_width=signal_width,
        total_width=strike_width + (len(normalized_slots) * group_width * 2),
        put_slots=tuple(
            MatrixWidthSlot(
                width=width,
                selected=width in selected,
                x=strike_x + strike_width + (index * group_width),
            )
            for index, width in enumerate(normalized_slots)
        ),
        call_slots=tuple(
            MatrixWidthSlot(
                width=width,
                selected=width in selected,
                x=(len(normalized_slots) - index - 1) * group_width,
            )
            for index, width in enumerate(normalized_slots)
        ),
    )


class OptionsMatrixApp(PositionAlertsMixin):
    window_title = "Options Matrix Studio (mini PC)"

    def __init__(
        self,
        *,
        settings_root: Path | None = None,
        market_data_service: Any | None = None,
        demo: bool = False,
    ) -> None:
        self.root = tk.Tk()
        self.theme = one_desk.apply(self.root)
        self.root.title(f"{self.window_title} - DEMO DATA" if demo else self.window_title)
        self.root.geometry("1260x760")
        self.root.minsize(1040, 620)

        self._demo_mode = bool(demo)
        self._settings_root = settings_root
        self._settings_path = settings_root / "settings.json" if settings_root is not None else None
        self._workspace_cache_path = settings_root / "matrix_cache.json" if settings_root is not None else None
        self._studio_settings_path = (
            settings_root if settings_root is not None else studio_app_data_dir()
        ) / STUDIO_SETTINGS_FILENAME
        if demo:
            # Made-up demo tickers must never replace the real saved tabs.
            self._workspace_cache_path = self._studio_settings_path.with_name("demo_matrix_cache.json")
        self.studio_settings: StudioSettings = load_studio_settings(self._studio_settings_path)
        self.services: StudioServices | None = None
        self.service = (
            market_data_service
            if market_data_service is not None
            else SchwabMarketDataService()
        )
        self.worker_events: queue.Queue[_WorkerResult] = queue.Queue()
        self.formula_settings = load_formula_settings(self._settings_path)
        self.signal_method = load_signal_method(self._settings_path)
        self._initialize_positions(settings_root)
        self.cached_matrix_workspace: CachedMatrixWorkspace | None = load_matrix_workspace_cache(self._workspace_cache_path)
        self.matrix_mode = DEFAULT_MATRIX_MODE
        self._task_token = 0
        self._task_tokens: dict[str, int] = {}
        self._workspace_generation = 0
        self._connect_inflight = False
        self._disconnect_inflight = False
        self._service_connected = False
        self._connection_active = False
        self._quote_event_generation = int(getattr(self.service, "quote_generation", 0))
        self._subscription_diagnostics_by_con_id: dict[int, object] = {}
        self._subscription_last_states: dict[int, str] = {}
        self._subscription_failure_counts: dict[int, int] = {}
        self._quote_redraw_after_id: str | None = None
        self._matrix_layout_after_id: str | None = None
        self._dirty_tab_ids: set[int] = set()
        self._matrix_render_signature: tuple[object, ...] | None = None
        self._matrix_cell_items: dict[SelectedMatrixCellKey, MatrixCanvasCellItems] = {}
        self._matrix_liquidity_render_state: dict[
            SelectedMatrixCellKey,
            tuple[MatrixLiquidityGaugeState | None, bool],
        ] = {}
        self._rank_render_signature: tuple[object, ...] | None = None
        self._shutdown_in_progress = False
        self._shutdown_finalized = False
        self._restore_workspace_inflight = False
        self._combo_target_key: tuple[tuple[tuple[str, int], ...], ...] | None = None
        self._combo_subscription_inflight = False
        self.snapshot_row_builder = SnapshotRowBuilder(
            self.formula_settings,
            signal_method=self.signal_method,
        )
        self.recorder: ParquetSnapshotRecorder | None = None
        self._recorder_after_id: str | None = None
        self._recorder_next_deadline: float | None = None
        self._recorder_snapshot_inflight = False
        self._recorder_stop_inflight = False
        self._recorder_stop_silent = False
        self._close_after_recorder_stop = False
        self._recorder_start_inflight = False
        self._recorder_armed = False
        self._recorder_error_message: str | None = None
        self._recorder_armed_output_root: Path | None = None
        self._recorder_armed_ticker_label = "UNKNOWN"
        self._recorder_market_session_guard: MarketSessionGuard | None = None
        self._recorder_market_time_offset = timedelta(0)
        self._recorder_market_status_message = ""
        self._recorder_market_close_utc: datetime | None = None

        self.search_result: SearchResult | None = None
        self.expiration_display_to_raw: dict[str, str] = {}
        self.option_records_by_expiration: dict[tuple[str, str], list[OptionContractRecord]] = {}
        self.option_records: list[OptionContractRecord] = []
        self.matrix_tabs: list[MatrixTabState] = []
        self.active_tab_id: int | None = None
        self._next_tab_id = 1
        self.row_strikes: list[float] = []
        self.applied_matrix_input: MatrixInputState | None = None
        self._pending_matrix_input: MatrixInputState | None = None
        self.matrix_plan: MatrixSelectionPlan | None = None
        self.matrix_snapshots: dict[SelectedMatrixCellKey, MonitoredSideSnapshot] = {}
        self.ranked_signals: list[RankedMatrixSignalRow] = []
        self.rank_sort_state = RankSortState()
        self.underlying_quote: QuoteSnapshot | None = None
        self.leg_quote_cache: dict[int, QuoteSnapshot] = {}
        self.active_market_data_quote_counts: MarketDataQuoteCounts | None = None
        self.selected_cell_key: SelectedMatrixCellKey | None = None
        self.matrix_hitboxes: list[tuple[float, float, float, float, SelectedMatrixCellKey]] = []
        self.matrix_reload_hitbox: tuple[float, float, float, float] | None = None
        self.rank_item_cells: dict[str, tuple[int, SelectedMatrixCellKey]] = {}
        self.rank_heading_labels = {
            "rank": "RANK",
            "tab": "TAB",
            "side": "SIDE",
            "combo": "COMBO",
            "width": "WIDTH",
            "signal": "SIGNAL",
            "price": "PRICE",
            "edge": "EDGE",
        }

        self.ticker_var = tk.StringVar()
        self.expiration_var = tk.StringVar()
        self.side_var = tk.StringVar(value=RIGHT_CALL)
        self.low_strike_var = tk.StringVar()
        self.high_strike_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Connect to Schwab, then load a ticker.")
        self.status_display_var = tk.StringVar(value=self.status_var.get())
        self.connection_var = tk.StringVar(value="○ OFFLINE")
        self.auth_status_var = tk.StringVar(value="")
        self.market_data_quote_status_var = tk.StringVar(
            value=format_compact_market_data_quote_status(None)
        )
        self._market_data_quote_status: MarketDataQuoteStatus | None = None
        self._connection_status_message = ""
        self._status_label_width = 0
        self.selected_header_var = tk.StringVar(
            value="SELECTED SPREAD: (TYPE) --- 0W (EXPIRY) - -- (--)"
        )
        self.recorder_status_var = tk.StringVar(value="Recorder idle.")
        self.recorder_indicator_var = tk.StringVar(value="\u25cb REC OFF")
        self.recording_folder_var = tk.StringVar(value=str(self._recording_output_root()))
        self.recording_session_var = tk.StringVar(value="No recording session yet.")
        self.recording_file_var = tk.StringVar(value="--")
        self.recording_rows_var = tk.StringVar(value="0 rows saved")
        self.recording_last_write_var = tk.StringVar(value="--")
        self.record_daily_var = tk.BooleanVar(value=self.studio_settings.record_every_trading_day)
        self.share_indicator_var = tk.StringVar(value="\u21c4 SHARING OFF")
        self._width_preferences_path = width_preferences_config_path(self._settings_path)
        self._width_band_preferences = load_width_band_preferences(
            self._width_preferences_path,
            cached_workspace=self.cached_matrix_workspace,
        )
        self.width_vars = {
            band: tk.BooleanVar(value=self._width_band_preferences[band])
            for band in MATRIX_WIDTH_BANDS
        }
        self.width_checkbuttons: dict[float, ttk.Checkbutton] = {}
        self.available_width_bands: tuple[float, ...] = ()
        self._width_band_availability_known = False

        fonts = one_desk.matrix_fonts(self.theme)
        self.grid_font = fonts.grid
        self.grid_font_bold = fonts.grid_bold
        self.detail_font = fonts.detail
        self.detail_font_bold = fonts.detail_bold
        self.header_font = fonts.header
        self.prose_font = fonts.prose
        self.prose_bold_font = fonts.prose_bold

        self._build_ui()
        one_desk.caps_labels(self.root)
        self._load_cached_workspace_as_stale_tabs()
        self._set_connected_state(False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(UI_POLL_INTERVAL_MS, self._poll_queues)
        self._start_position_timer()

    def _build_connection_bar(self, main: ttk.Frame) -> None:
        """Schwab connection, REC indicator and status line (row 0)."""
        connection = ttk.Frame(main)
        connection.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        connection.columnconfigure(5, weight=1)

        connection_title = ttk.Label(connection, text="SCHWAB MARKET DATA")
        connection_title.grid(row=0, column=0, sticky="w")
        self._install_tooltip(
            connection_title,
            "Credentials are read from schwab_auth/schwab_token.json.",
        )
        self.connection_status_label = ttk.Label(
            connection,
            textvariable=self.connection_var,
            foreground=CONNECTION_OFFLINE_COLOR,
            width=20,
            anchor="center",
        )
        self.connection_status_label.grid(row=0, column=1, sticky="w", padx=(10, 4))
        self._install_tooltip(
            self.connection_status_label,
            lambda: self._connection_tooltip_text(),
        )
        self.auth_status_label = ttk.Label(
            connection,
            textvariable=getattr(self, "auth_status_var", self.connection_var),
            foreground=CONNECTION_PENDING_COLOR,
            width=13,
            anchor="center",
        )
        self.auth_status_label.grid(row=0, column=2, sticky="w", padx=(0, 4))
        self._install_tooltip(
            self.auth_status_label,
            lambda: self._auth_tooltip_text(),
        )
        self.connect_button = ttk.Button(connection, text="Connect", command=self._connect)
        self.connect_button.grid(row=0, column=3, padx=(0, 6))
        self.disconnect_button = ttk.Button(connection, text="Disconnect", command=self._disconnect)
        self.disconnect_button.grid(row=0, column=4, padx=(0, 10))
        self.recorder_indicator_label = ttk.Label(
            connection,
            textvariable=self.recorder_indicator_var,
            foreground=RECORDER_OFF_COLOR,
            width=15,
            anchor="center",
        )
        self.recorder_indicator_label.grid(row=0, column=6, sticky="e")
        self._build_studio_bar_controls(connection)

        self.market_data_quote_status_label = ttk.Label(
            connection,
            textvariable=self.market_data_quote_status_var,
            foreground=MUTED,
            anchor="w",
        )
        self.market_data_quote_status_label.grid(
            row=1,
            column=0,
            columnspan=9,
            sticky="ew",
            pady=(2, 0),
        )
        self._install_tooltip(
            self.market_data_quote_status_label,
            lambda: self._market_data_quote_tooltip_text(),
        )

        ttk.Label(connection, text="STATUS", foreground=MUTED).grid(
            row=2,
            column=0,
            sticky="w",
            pady=(1, 0),
        )
        self.status_label = ttk.Label(
            connection,
            textvariable=getattr(self, "status_display_var", self.status_var),
            foreground=NAVY,
            anchor="w",
            width=1,
        )
        self.status_label.grid(
            row=2,
            column=1,
            columnspan=8,
            sticky="ew",
            padx=(8, 0),
            pady=(1, 0),
        )
        self.status_label.bind("<Configure>", self._on_status_label_configure, add="+")
        self._install_tooltip(self.status_label, lambda: self.status_var.get())

    def _build_matrix_controls(self, main: ttk.Frame) -> None:
        """Ticker, expiry, side, strike range and width inputs (row 1)."""
        controls = ttk.Frame(main)
        controls.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        for column in range(15):
            controls.columnconfigure(column, weight=1 if column in {1, 4, 6, 8, 10, 12} else 0)

        ttk.Label(controls, text="TICKER").grid(row=0, column=0, sticky="w")
        ticker_entry = ttk.Entry(controls, textvariable=self.ticker_var, width=10)
        ticker_entry.grid(row=0, column=1, sticky="ew", padx=(4, 6))
        ticker_entry.bind("<Return>", lambda _event: self._load_ticker())
        ttk.Button(controls, text="Load", command=self._load_ticker).grid(row=0, column=2, padx=(0, 10))

        ttk.Label(controls, text="EXPIRY").grid(row=0, column=3, sticky="w")
        self.expiration_combo = ttk.Combobox(
            controls,
            textvariable=self.expiration_var,
            state="readonly",
            width=14,
        )
        self.expiration_combo.grid(row=0, column=4, sticky="ew", padx=(4, 10))
        self.expiration_combo.bind("<<ComboboxSelected>>", lambda _event: self._load_expiration())

        ttk.Label(controls, text="SIDE").grid(row=0, column=5, sticky="w")
        self.side_combo = ttk.Combobox(
            controls,
            textvariable=self.side_var,
            values=MATRIX_VIEWS,
            state="readonly",
            width=8,
        )
        self.side_combo.grid(row=0, column=6, sticky="ew", padx=(4, 10))
        self.side_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_side_changed())

        ttk.Label(controls, text="LOW").grid(row=0, column=7, sticky="w")
        self.low_strike_combo = ttk.Combobox(
            controls,
            textvariable=self.low_strike_var,
            state="readonly",
            width=9,
        )
        self.low_strike_combo.grid(row=0, column=8, sticky="ew", padx=(4, 10))
        self.low_strike_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_matrix_input_changed())

        ttk.Label(controls, text="HIGH").grid(row=0, column=9, sticky="w")
        self.high_strike_combo = ttk.Combobox(
            controls,
            textvariable=self.high_strike_var,
            state="readonly",
            width=9,
        )
        self.high_strike_combo.grid(row=0, column=10, sticky="ew", padx=(4, 10))
        self.high_strike_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_matrix_input_changed())

        ttk.Label(controls, text="WIDTH BANDS").grid(row=0, column=11, sticky="w")
        width_host = ttk.Frame(controls)
        width_host.grid(row=0, column=12, sticky="w", padx=(4, 10))
        for index, band in enumerate(MATRIX_WIDTH_BANDS):
            check = ttk.Checkbutton(
                width_host,
                text=f"W{band:g}",
                variable=self.width_vars[band],
                command=self._on_width_band_changed,
            )
            check.grid(row=0, column=index, padx=(0, 4))
            self.width_checkbuttons[band] = check

        ttk.Button(controls, text="Load matrix", command=self._load_matrix).grid(
            row=0, column=13, padx=(0, 6)
        )
        ttk.Button(controls, text="Settings…", command=self._open_settings_dialog).grid(
            row=0, column=14, padx=(0, 6)
        )

    def _build_extra_tabs(self, notebook: ttk.Notebook) -> None:
        """Bottom tabs after Signals and Positions; the mini PC adds Recording."""
        self._build_recording_controls(notebook)

    def _build_studio_bar_controls(self, connection: ttk.Frame) -> None:
        """Sharing indicator and the Mini PC settings button (columns 7-8)."""
        self.share_indicator_label = ttk.Label(
            connection,
            textvariable=self.share_indicator_var,
            foreground=NAVY,
            width=16,
            anchor="center",
        )
        self.share_indicator_label.grid(row=0, column=7, sticky="e", padx=(6, 0))
        self._install_tooltip(self.share_indicator_label, lambda: self._share_tooltip_text())
        ttk.Button(connection, text="Mini PC…", command=self._open_studio_settings_dialog).grid(
            row=0, column=8, padx=(6, 0)
        )

    def _share_tooltip_text(self) -> str:
        services = getattr(self, "services", None)
        if services is None:
            return "Sharing starts when the program opens."
        return services.share_status_text()

    def _open_studio_settings_dialog(self) -> None:
        from .studio_dialog import StudioSettingsDialog

        StudioSettingsDialog(self)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        main = ttk.Frame(self.root, padding=4)
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)
        main.rowconfigure(3, weight=0)

        self._build_connection_bar(main)

        self._build_matrix_controls(main)

        matrix_title = tk.Frame(
            main, bd=0, highlightthickness=1, highlightbackground=GRID_LINE,
            highlightcolor=GRID_LINE, bg=GRID_BG, height=28,
        )
        matrix_title.grid(row=2, column=0, sticky="new")
        matrix_title.columnconfigure(1, weight=1)
        tk.Label(
            matrix_title, text="SPREAD WIDTH MATRIX", bg=GRID_BG, fg=MUTED,
            font=getattr(self, "header_font", self.grid_font),
        ).grid(
            row=0, column=0, sticky="w", padx=(6, 12), pady=4
        )
        self.matrix_tabs_frame = tk.Frame(matrix_title, bg=GRID_BG)
        self.matrix_tabs_frame.grid(row=0, column=1, sticky="w", pady=2)
        tk.Label(
            matrix_title,
            text="Stream: hairline live · navy pending · dashed amber FV context · hatch failed",
            bg=GRID_BG,
            fg=MUTED,
            font=self.detail_font,
        ).grid(row=0, column=2, sticky="e", padx=(8, 6))

        matrix_host = ttk.Frame(main)
        matrix_host.grid(row=2, column=0, sticky="nsew", pady=(28, 0))
        matrix_host.columnconfigure(0, weight=1)
        matrix_host.rowconfigure(0, weight=1)
        matrix_host.rowconfigure(1, weight=0)

        self.matrix_canvas = tk.Canvas(
            matrix_host,
            background=GRID_BG,
            highlightthickness=0,
            bd=0,
        )
        self.matrix_canvas.grid(row=0, column=0, sticky="nsew")
        self.matrix_x_scrollbar = ttk.Scrollbar(matrix_host, orient="horizontal", command=self.matrix_canvas.xview)
        self.matrix_x_scrollbar.grid(row=1, column=0, sticky="ew")
        self.matrix_canvas.configure(xscrollcommand=self.matrix_x_scrollbar.set)
        self.matrix_canvas.bind("<Button-1>", self._on_matrix_click)
        self.matrix_canvas.bind("<Configure>", self._schedule_matrix_layout_redraw)

        bottom = ttk.Notebook(main)
        bottom.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        signals_tab = ttk.Frame(bottom)
        bottom.add(signals_tab, text="Signals")
        signals_tab.columnconfigure(0, weight=0)
        signals_tab.columnconfigure(1, weight=1)

        self.rank_tree = ttk.Treeview(
            signals_tab,
            columns=("rank", "tab", "side", "combo", "width", "signal", "price", "edge"),
            show="headings",
            height=7,
            selectmode="browse",
        )
        for column, heading, width in (
            ("rank", "RANK", 52),
            ("tab", "TAB", 110),
            ("side", "SIDE", 52),
            ("combo", "COMBO", 82),
            ("width", "WIDTH", 62),
            ("signal", "SIGNAL", 66),
            ("price", "PRICE", 70),
            ("edge", "EDGE", 70),
        ):
            self.rank_tree.heading(
                column,
                text=heading,
                command=lambda selected_column=column: self._on_rank_heading_clicked(selected_column),
            )
            self.rank_tree.column(column, width=width, minwidth=width, stretch=False, anchor="center")
        self.rank_tree.grid(row=0, column=0, sticky="nsw")
        self.rank_tree.bind("<<TreeviewSelect>>", self._on_rank_selected)

        detail = ttk.Frame(signals_tab)
        detail.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        detail.columnconfigure(0, weight=1)
        detail.rowconfigure(1, weight=1)
        tk.Label(
            detail,
            textvariable=self.selected_header_var,
            anchor="w",
            bd=0,
            highlightthickness=1,
            highlightbackground=GRID_LINE,
            highlightcolor=GRID_LINE,
            font=self.grid_font,
            bg=GRID_BG,
        ).grid(row=0, column=0, sticky="ew")
        self.detail_canvas = tk.Canvas(
            detail,
            height=104,
            background=GRID_BG,
            highlightthickness=1,
            highlightbackground=GRID_LINE,
            highlightcolor=GRID_LINE,
            bd=0,
        )
        self.detail_canvas.grid(row=1, column=0, sticky="ew")
        self.detail_canvas.bind("<Configure>", lambda _event: self._redraw_selected_detail())
        self._build_position_controls(bottom, detail)
        self._build_extra_tabs(bottom)

        self._redraw_matrix_tabs()
        self._refresh_recorder_controls()
        self._refresh_width_band_controls()


    def _build_recording_controls(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=6)
        notebook.add(tab, text="Recording")
        tab.columnconfigure(1, weight=1)
        ttk.Label(tab, text="Save recordings to").grid(row=0, column=0, sticky="w")
        self.recording_folder_entry = ttk.Entry(tab, textvariable=self.recording_folder_var)
        self.recording_folder_entry.grid(row=0, column=1, sticky="ew", padx=6)
        self.recording_folder_entry.bind("<FocusOut>", lambda _event: self._save_recording_folder())
        self.recording_folder_entry.bind("<Return>", lambda _event: self._save_recording_folder())
        self.recording_browse_button = ttk.Button(
            tab, text="Browse…", command=self._browse_recording_folder
        )
        self.recording_browse_button.grid(row=0, column=2)
        ttk.Button(tab, text="Open folder", command=self._open_recording_folder).grid(
            row=0, column=3, padx=(6, 0)
        )

        buttons = ttk.Frame(tab)
        buttons.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 4))
        self.recording_start_button = ttk.Button(
            buttons, text="Start recording", command=self._on_start_recording_clicked
        )
        self.recording_start_button.pack(side="left")
        self.recording_stop_button = ttk.Button(
            buttons, text="Stop recording", command=self._on_stop_recording_clicked
        )
        self.recording_stop_button.pack(side="left", padx=(6, 0))
        ttk.Checkbutton(
            buttons,
            text="Record every trading day automatically",
            variable=self.record_daily_var,
            command=self._on_record_daily_changed,
        ).pack(side="left", padx=(16, 0))

        for row, (label, variable) in enumerate(
            (
                ("Status", self.recorder_status_var),
                ("Session folder", self.recording_session_var),
                ("Current file", self.recording_file_var),
                ("Progress", self.recording_rows_var),
                ("Last file write", self.recording_last_write_var),
            ),
            start=2,
        ):
            ttk.Label(tab, text=label, foreground=MUTED).grid(row=row, column=0, sticky="nw")
            ttk.Label(tab, textvariable=variable, wraplength=760, justify="left").grid(
                row=row, column=1, columnspan=3, sticky="w"
            )
        ttk.Label(
            tab,
            text=(
                "One snapshot per second while the NYSE is open. Starting before the open waits "
                "for the bell; recording stops at the close, including early closes."
            ),
            foreground=MUTED,
        ).grid(row=7, column=0, columnspan=4, sticky="w", pady=(6, 0))

    def _save_recording_folder(self) -> None:
        value = self.recording_folder_var.get().strip()
        folder = "" if not value or Path(value) == default_recording_root() else value
        if folder != self.studio_settings.recording_folder:
            self._update_studio_settings(recording_folder=folder)

    def _browse_recording_folder(self) -> None:
        from tkinter import filedialog

        initial = self._recording_output_root()
        while not initial.is_dir() and initial != initial.parent:
            initial = initial.parent
        selected = filedialog.askdirectory(
            parent=self.root,
            title="Choose where recordings are saved",
            initialdir=str(initial),
        )
        if selected:
            self.recording_folder_var.set(str(Path(selected)))
            self._save_recording_folder()

    def _open_recording_folder(self) -> None:
        folder = self._recording_output_root()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)  # type: ignore[attr-defined]  # Windows only
        except (OSError, AttributeError) as exc:
            self._set_status(f"Could not open the recording folder: {exc}")

    def _on_start_recording_clicked(self) -> None:
        self._save_recording_folder()
        try:
            self.start_capture()
        except RuntimeError as exc:
            messagebox.showerror("Recording", str(exc), parent=self.root)

    def _on_stop_recording_clicked(self) -> None:
        services = getattr(self, "services", None)
        if services is not None:
            services.user_stopped_recording()
        self._stop_recording()

    def _on_record_daily_changed(self) -> None:
        self._update_studio_settings(record_every_trading_day=bool(self.record_daily_var.get()))

    def _update_studio_settings(self, **changes: Any) -> bool:
        """Apply and save mini PC settings; running services pick them up."""
        updated = replace(self.studio_settings, **changes)
        if updated == self.studio_settings:
            return True
        self.studio_settings = updated
        saved = True
        try:
            save_studio_settings(updated, self._studio_settings_path)
        except OSError as exc:
            saved = False
            self._set_status(f"Setting applied, but it could not be saved: {exc}")
        services = getattr(self, "services", None)
        if services is not None:
            services.settings_changed()
        return saved

    def _connect(self) -> None:
        if getattr(self, "_connect_inflight", False) or getattr(self, "_disconnect_inflight", False):
            self._set_status("Wait for the current connection action to finish.")
            return
        if self.service.connected:
            self._connection_active = True
            self._set_connected_state(True)
            self._set_status("Already connected.")
            return
        if getattr(self, "_connection_active", False):
            self._set_connected_state(False)
            self._set_status(
                "Schwab is reconnecting. Wait for reconnection, or use Disconnect before retrying Connect."
            )
            return
        self._connect_inflight = True
        self._connection_active = True
        self._set_connected_state(False)
        self._start_task("connect", self._connect_task)

    def _disconnect(self) -> None:
        if getattr(self, "_connect_inflight", False) or getattr(self, "_disconnect_inflight", False):
            self._set_status("Wait for the current connection action to finish.")
            return
        self._disconnect_inflight = True
        self._set_connected_state(bool(self.service.connected))
        OptionsMatrixApp._advance_workspace_generation(self)
        self._stop_recording(silent=True)
        self._save_current_matrix_cache()
        self._start_task("disconnect", self.service.disconnect)


    def start_capture(self) -> None:
        """Start recording, or arm it until the open when the market is not open yet."""
        self._recorder_error_message = None
        try:
            if self._shutdown_in_progress:
                raise RuntimeError("Options Matrix is closing.")
            if not self.service.connected:
                raise RuntimeError("Connect Options Matrix to Schwab before recording.")
            if not self._has_live_matrix_tabs():
                raise RuntimeError("Load at least one live matrix tab before recording.")
            self._start_recording()
        except RuntimeError as exc:
            self._recorder_error_message = str(exc)
            self.recorder_status_var.set(str(exc))
            self._refresh_recorder_controls()
            raise

    def stop_capture(self) -> None:
        """Stop recording; buffered rows finish writing before the session closes."""
        self._stop_recording(silent=True)

    def capture_active(self) -> bool:
        """Whether recording is starting, armed, running or finishing its writes."""
        return bool(
            getattr(self, "recorder", None) is not None
            or getattr(self, "_recorder_start_inflight", False)
            or getattr(self, "_recorder_armed", False)
            or getattr(self, "_recorder_stop_inflight", False)
        )

    def capture_status(self) -> dict[str, Any]:
        """Return the public capture health contract, without exposing Tk state."""
        recorder = self.recorder
        status = recorder.status() if recorder is not None else None
        error = self._recorder_error_message or (status.error if status is not None else None)
        state = (
            "Error" if error else
            "Stopping" if self._recorder_stop_inflight else
            "Recording" if recorder is not None and recorder.running else
            "Armed" if self._recorder_armed else
            "Starting" if self._recorder_start_inflight else "Idle"
        )
        return {
            "type": "health", "state": state, "error": error,
            "live_tabs": self._recorder_live_tab_count(),
            "spread_count": self._recorder_subscribed_spread_count(status),
            "market_status": self._recorder_market_status_message,
            "broker_connected": bool(self.service.connected),
        }

    def _has_live_matrix_tabs(self) -> bool:
        return any(not tab.is_stale for tab in self.matrix_tabs)

    def _recording_output_root(self) -> Path:
        """The configured recording folder, or this project's ``recordings``."""
        settings = getattr(self, "studio_settings", None)
        configured = getattr(settings, "recording_folder", "") if settings is not None else ""
        if configured:
            return Path(configured).expanduser()
        return default_recording_root()

    def _start_recording(self) -> None:
        if self.recorder is not None or getattr(self, "_recorder_stop_inflight", False):
            return
        if self._recorder_start_inflight or getattr(self, "_recorder_armed", False):
            return
        if not self._has_live_matrix_tabs():
            self._set_status("Load at least one live matrix tab before recording.")
            self._refresh_recorder_controls()
            return
        output_root = self._recording_output_root()
        ticker_label = recording_ticker_label_from_tabs(self.matrix_tabs)
        self._recorder_error_message = None
        self._recorder_start_inflight = True
        self.recorder_status_var.set("Checking Schwab market time ...")
        self._refresh_recorder_controls()
        self._start_task("start_recording", lambda: self._start_recording_task(output_root, ticker_label))

    def _start_recording_task(self, output_root: Path, ticker_label: str = "UNKNOWN") -> _StartRecordingResult:
        if not self.service.connected:
            raise RuntimeError("Connect to Schwab before starting the recorder.")
        if output_root.exists() and not output_root.is_dir():
            raise RuntimeError(f"The recording folder is a file, not a folder: {output_root}")
        market_now_utc = self.service.request_current_time()
        local_now_utc = datetime.now(timezone.utc)
        market_session_guard = self._build_market_session_guard()
        session_status = market_session_guard.status_at(market_now_utc)
        if not session_status.is_open and session_status.reason != "pre_open":
            raise RuntimeError(session_status.message)
        return _StartRecordingResult(
            output_root=output_root,
            ticker_label=ticker_label,
            market_session_guard=market_session_guard,
            market_now_utc=market_now_utc,
            local_now_utc=local_now_utc,
            session_status=session_status,
            armed=not session_status.is_open,
        )

    def _build_market_session_guard(self) -> MarketSessionGuard:
        if getattr(self, "_demo_mode", False):
            from .demo_service import DemoMarketSessionGuard

            return DemoMarketSessionGuard()
        return MarketSessionGuard()

    def _create_recorder(self, output_root: Path, ticker_label: str) -> ParquetSnapshotRecorder:
        return ParquetSnapshotRecorder(output_root, ticker_label=ticker_label)

    def _finish_start_recording(self, result: _StartRecordingResult) -> None:
        if not self._recorder_start_inflight:
            return
        self._recorder_start_inflight = False
        self._recorder_market_session_guard = result.market_session_guard
        self._recorder_market_time_offset = result.market_now_utc - result.local_now_utc
        if result.armed:
            self._recorder_armed = True
            self._recorder_armed_output_root = result.output_root
            self._recorder_armed_ticker_label = result.ticker_label
            self._recorder_market_status_message = result.session_status.message
            self._recorder_market_close_utc = result.session_status.market_close_utc
            open_text = self._format_recorder_time(result.session_status.market_open_utc)
            message = f"Armed - recording starts at {open_text}."
            self.recorder_status_var.set(message)
            self._set_status(message)
            self._refresh_recorder_controls()
            self._schedule_recorder_tick()
            return
        self._start_recorder_session(
            result.output_root,
            result.ticker_label,
            result.market_now_utc,
            result.session_status,
        )

    def _start_recorder_session(
        self,
        output_root: Path,
        ticker_label: str,
        started_at: datetime,
        session_status: MarketSessionStatus,
    ) -> None:
        recorder = self._create_recorder(output_root, ticker_label)
        try:
            session_id = recorder.start(started_at=started_at)
        except (OSError, RuntimeError) as exc:
            self._recorder_error_message = str(exc)
            self._set_status(str(exc))
            self.recorder_status_var.set(str(exc))
            messagebox.showerror("Recorder", str(exc), parent=self.root)
            self._refresh_recorder_controls()
            return
        self.recorder = recorder
        self._recorder_stop_inflight = False
        self._recorder_snapshot_inflight = False
        self._recorder_next_deadline = time.monotonic()
        self._recorder_error_message = None
        self._recorder_market_status_message = session_status.message
        self._recorder_market_close_utc = session_status.market_close_utc
        self.recorder_status_var.set(f"Recording. {session_status.message}")
        self._set_status(f"Recording started: {session_id}. {session_status.message}")
        self._refresh_recorder_controls()
        self._schedule_recorder_tick(immediate=True)

    def _stop_recording(self, *, silent: bool = False) -> None:
        self._cancel_recorder_timer()
        self._recorder_next_deadline = None
        was_armed = getattr(self, "_recorder_armed", False)
        self._recorder_armed = False
        recorder = self.recorder
        if recorder is None:
            self._recorder_start_inflight = False
            self._recorder_market_session_guard = None
            self._recorder_market_time_offset = timedelta(0)
            self._recorder_market_status_message = ""
            self._recorder_market_close_utc = None
            self._recorder_stop_inflight = False
            if was_armed and not silent:
                self.recorder_status_var.set("Recorder idle.")
                self._recorder_error_message = None
            self._refresh_recorder_controls()
            return

        request_stop = getattr(recorder, "request_stop", None)
        if callable(request_stop):
            if not getattr(self, "_recorder_stop_inflight", False):
                self._recorder_stop_inflight = True
                self._recorder_stop_silent = bool(silent)
                request_stop()
            elif not silent:
                self._recorder_stop_silent = False
            self.recorder_status_var.set("Stopping; flushing accepted rows ...")
            self._refresh_recorder_controls()
            self._poll_recorder_stop()
            return

        # Compatibility for recorder implementations without the nonblocking API.
        error: str | None = None
        try:
            recorder.stop()
        except RuntimeError as exc:
            error = str(exc)
        status = recorder.status()
        status_error = getattr(status, "error", None)
        if error is None and status_error:
            error = status_error
        self.recorder = None
        self._recorder_start_inflight = False
        self._recorder_market_session_guard = None
        if error is not None:
            self._recorder_error_message = error
            self.recorder_status_var.set(error)
            self._set_status(error)
            if not silent:
                messagebox.showerror("Recorder", error, parent=self.root)
        elif not silent:
            self._recorder_error_message = None
            self.recorder_status_var.set("Recorder idle.")
            self._set_status(f"Recording stopped. Rows written: {status.rows_written}.")
        OptionsMatrixApp._refresh_recorder_controls(self, status_override=status)

    def _poll_recorder_stop(self) -> None:
        recorder = self.recorder
        if recorder is None or not getattr(self, "_recorder_stop_inflight", False):
            return
        status = recorder.status()
        self._refresh_recorder_controls(status_override=status)
        if status.writer_alive and not status.flush_complete:
            self.root.after(75, self._poll_recorder_stop)
            return

        error: str | None = None
        try:
            completed = recorder.wait_for_stop(timeout=0.0)
            if not completed:
                self.root.after(75, self._poll_recorder_stop)
                return
        except RuntimeError as exc:
            error = str(exc)
        final_status = recorder.status()
        if error is None:
            error = final_status.error
        silent = bool(getattr(self, "_recorder_stop_silent", False))
        if self.recorder is recorder:
            self.recorder = None
        self._recorder_stop_inflight = False
        self._recorder_snapshot_inflight = False
        self._recorder_start_inflight = False
        self._recorder_market_session_guard = None
        if error is not None:
            self._recorder_error_message = error
            self.recorder_status_var.set(error)
            self._set_status(error)
            if not silent and not getattr(self, "_shutdown_in_progress", False):
                messagebox.showerror("Recorder", error, parent=self.root)
        elif not silent:
            self._recorder_error_message = None
            self.recorder_status_var.set("Recorder idle.")
            self._set_status(f"Recording stopped. Rows written: {final_status.rows_written}.")
        OptionsMatrixApp._refresh_recorder_controls(self, status_override=final_status)
        if getattr(self, "_close_after_recorder_stop", False):
            OptionsMatrixApp._finalize_close(self)

    def _schedule_recorder_tick(self, *, immediate: bool = False) -> None:
        self._cancel_recorder_timer()
        now = time.monotonic()
        deadline = getattr(self, "_recorder_next_deadline", None)
        if immediate or deadline is None:
            deadline = now
            self._recorder_next_deadline = deadline
        delay_ms = max(0, int(round((deadline - now) * 1000)))
        self._recorder_after_id = self.root.after(delay_ms, self._record_snapshot_tick)

    def _cancel_recorder_timer(self) -> None:
        if self._recorder_after_id is None:
            return
        try:
            self.root.after_cancel(self._recorder_after_id)
        except tk.TclError:
            pass
        self._recorder_after_id = None

    def _record_snapshot_tick(self) -> None:
        self._recorder_after_id = None
        deadline = getattr(self, "_recorder_next_deadline", None)
        self._recorder_next_deadline = (deadline if deadline is not None else time.monotonic()) + 1.0
        if getattr(self, "_recorder_armed", False):
            self._recorder_tick_armed()
            return
        recorder = self.recorder
        if recorder is None or not recorder.running:
            self._refresh_recorder_controls()
            return
        if getattr(self, "_recorder_snapshot_inflight", False):
            message = "Recorder snapshot construction missed the fixed 1-second cadence."
            self._recorder_error_message = message
            self.recorder_status_var.set(message)
            self._set_status(message)
            self._stop_recording(silent=True)
            return
        try:
            captured_at = self._recorder_market_now()
            session_status = self._recorder_session_status(captured_at)
            if session_status is not None and not session_status.is_open:
                message = f"Auto-stopped recording: {session_status.message}"
                self._recorder_market_status_message = message
                self._recorder_market_close_utc = getattr(session_status, "market_close_utc", None)
                self.recorder_status_var.set(message)
                self._set_status(message)
                self._stop_recording(silent=True)
                return
            if session_status is not None:
                self._recorder_market_status_message = session_status.message
                self._recorder_market_close_utc = getattr(session_status, "market_close_utc", None)
            session_id = recorder.session_id
            if session_id is None:
                raise RuntimeError("Recorder session is not initialized.")
            tabs = tuple(
                replace(tab, leg_quotes=dict(tab.leg_quotes))
                for tab in flatten_matrix_tabs_for_recording(self.matrix_tabs)
            )
            builder = self.snapshot_row_builder
            underlying_quote = self.underlying_quote
            snapshot_id = str(uuid.uuid4())

            def build_snapshot() -> _RecordSnapshotResult:
                rows = builder.build_rows(
                    session_id=session_id,
                    snapshot_id=snapshot_id,
                    captured_at=captured_at,
                    tabs=tabs,
                    underlying_quote=underlying_quote,
                )
                return _RecordSnapshotResult(
                    recorder=recorder,
                    session_id=session_id,
                    captured_at=captured_at,
                    rows=rows,
                )

            start_task = getattr(self, "_start_task", None)
            if callable(start_task):
                self._recorder_snapshot_inflight = True
                start_task("record_snapshot", build_snapshot, announce=False)
            else:
                recorder.record_rows(build_snapshot().rows)
                self._refresh_recorder_controls()
        except RuntimeError as exc:
            self._recorder_error_message = str(exc)
            self.recorder_status_var.set(str(exc))
            self._set_status(str(exc))
            self._stop_recording(silent=True)
            return
        self._schedule_recorder_tick()

    def _recorder_tick_armed(self) -> None:
        """Armed pre-open: wait for the bell, then start the real recorder."""
        captured_at = self._recorder_market_now()
        session_status = self._recorder_session_status(captured_at)
        if session_status is None:
            self._recorder_armed = False
            self._refresh_recorder_controls()
            return
        if not session_status.is_open:
            if session_status.reason == "pre_open":
                self._schedule_recorder_tick()
                return
            self._recorder_armed = False
            message = f"Recording not started: {session_status.message}"
            self.recorder_status_var.set(message)
            self._set_status(message)
            self._refresh_recorder_controls()
            return
        self._recorder_armed = False
        output_root = self._recorder_armed_output_root or Path(".")
        self._start_recorder_session(
            output_root,
            self._recorder_armed_ticker_label,
            captured_at,
            session_status,
        )

    def _recorder_market_now(self) -> datetime:
        return datetime.now(timezone.utc) + self._recorder_market_time_offset

    def _recorder_session_status(self, captured_at: datetime) -> MarketSessionStatus | None:
        if self._recorder_market_session_guard is None:
            return None
        return self._recorder_market_session_guard.status_at(captured_at)


    def _format_recorder_time(self, value: datetime | None) -> str:
        if value is None:
            return "--"
        return value.astimezone(MARKET_TIMEZONE).strftime("%Y-%m-%d %I:%M:%S %p ET")


    def _recorder_live_tab_count(self) -> int:
        return sum(1 for tab in getattr(self, "matrix_tabs", ()) if not getattr(tab, "is_stale", False))

    def _recorder_subscribed_spread_count(self, status: RecorderStatus | None) -> int:
        quote_counts = getattr(self, "active_market_data_quote_counts", None)
        if quote_counts is not None:
            return int(quote_counts.combo_quotes)
        if status is not None and (status.running or status.last_snapshot_rows):
            return int(status.last_snapshot_rows)
        return 0


    def _refresh_recorder_controls(self, status_override: RecorderStatus | None = None) -> None:
        recorder = getattr(self, "recorder", None)
        running = recorder is not None and recorder.running
        start_inflight = getattr(self, "_recorder_start_inflight", False)
        armed = getattr(self, "_recorder_armed", False)
        status = status_override if status_override is not None else recorder.status() if recorder is not None else None
        stopping = bool(
            getattr(self, "_recorder_stop_inflight", False)
            or getattr(status, "stopping", False)
        )
        OptionsMatrixApp._refresh_recorder_indicator(
            self,
            status,
            running=running,
            start_inflight=start_inflight,
            armed=armed,
            stopping=stopping,
        )
        OptionsMatrixApp._refresh_recording_panel(
            self,
            status,
            busy=bool(running or start_inflight or armed or stopping),
            can_stop=bool((running or start_inflight or armed) and not stopping),
        )
        if recorder is None:
            if (
                not start_inflight
                and not armed
                and self.recorder_status_var.get() in {"Recording.", "Checking Schwab market time ..."}
            ):
                self.recorder_status_var.set("Recorder idle.")
            return
        status_text = (
            "Stopping; flushing accepted rows ..."
            if stopping
            else f"Recording. {getattr(self, '_recorder_market_status_message', '')}"
            if status.running and getattr(self, "_recorder_market_status_message", "")
            else "Recording." if status.running else "Recorder idle."
        )
        if status.error:
            status_text = status.error
        self.recorder_status_var.set(status_text)

    def _refresh_recording_panel(
        self,
        status: RecorderStatus | None,
        *,
        busy: bool,
        can_stop: bool,
    ) -> None:
        """Keep the Recording tab's buttons and progress lines current."""
        start_button = getattr(self, "recording_start_button", None)
        if start_button is None:
            return
        try:
            start_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
            self.recording_stop_button.configure(state=tk.NORMAL if can_stop else tk.DISABLED)
            self.recording_folder_entry.configure(state="disabled" if busy else "normal")
            self.recording_browse_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        except tk.TclError:
            return
        if status is None:
            return
        session_dir = getattr(status, "session_dir", None)
        if session_dir:
            self.recording_session_var.set(str(session_dir))
        part = getattr(status, "current_part_path", None)
        self.recording_file_var.set(str(part) if part else "--")
        self.recording_rows_var.set(
            f"{status.rows_written:,} rows saved · {status.pending_rows:,} waiting to be written"
        )
        last_write = getattr(status, "last_write_at", None)
        self.recording_last_write_var.set(
            OptionsMatrixApp._format_recorder_time(self, last_write) if last_write else "--"
        )

    def _refresh_recorder_indicator(
        self,
        status: RecorderStatus | None,
        *,
        running: bool,
        start_inflight: bool,
        armed: bool,
        stopping: bool = False,
    ) -> None:
        status_error = getattr(status, "error", None) if status is not None else None
        indicator = resolve_recorder_indicator_state(
            running=running,
            start_inflight=start_inflight,
            armed=armed,
            stopping=stopping,
            error=status_error or getattr(self, "_recorder_error_message", None),
        )
        variable = getattr(self, "recorder_indicator_var", None)
        if variable is not None:
            variable.set(indicator.text)
        label = getattr(self, "recorder_indicator_label", None)
        if label is not None:
            label.configure(foreground=indicator.color)

    def _reload_active_matrix_tab(self) -> None:
        tab = self._active_tab()
        if tab is None or not tab.is_stale or self._restore_workspace_inflight:
            return
        self._restore_workspace_inflight = True
        self._set_status(f"Reloading {tab.label} ...")
        self._start_task("reload_matrix_tab", lambda: self._reload_matrix_tab_task(tab.tab_id))

    def _reload_matrix_tab_task(self, tab_id: int) -> _ReloadMatrixTabResult:
        tab = next((candidate for candidate in self.matrix_tabs if candidate.tab_id == tab_id), None)
        if tab is None:
            raise RuntimeError("Cached matrix tab is no longer available.")
        if not self.service.connected:
            self._connect_task()
        search_result = self.service.search_ticker(tab.matrix_input.ticker_symbol)
        available_expirations = set(search_result.chain.expirations)
        if tab.matrix_input.raw_expiration not in available_expirations:
            raise RuntimeError(f"Cached expiration is no longer available: {tab.matrix_input.raw_expiration}.")
        records = self.service.load_option_records(search_result, tab.matrix_input.raw_expiration)
        return _ReloadMatrixTabResult(
            tab_id=tab_id,
            search_result=search_result,
            records=records,
        )

    def _connect_task(self) -> None:
        self.service.connect()

    def _load_ticker(self) -> None:
        ticker = self.ticker_var.get().strip().upper()
        if not ticker:
            self._set_status("Enter a ticker before loading expirations.")
            return
        if getattr(self, "_connect_inflight", False) or getattr(self, "_disconnect_inflight", False):
            self._set_status("Wait for the current connection action to finish before loading a ticker.")
            return
        if not self.service.connected:
            connection_active = bool(getattr(self, "_connection_active", False))
            self._set_connected_state(False)
            self._set_status(
                (
                    "Schwab is reconnecting. Wait for reconnection before loading, or use DISCONNECT to cancel."
                    if connection_active
                    else "Schwab is disconnected. Use Connect and wait for a live connection before loading."
                )
            )
            return
        OptionsMatrixApp._advance_workspace_generation(self)
        self.ticker_var.set(ticker)
        self._clear_cached_matrix_workspace()
        clear_active_quotes = bool(self.matrix_tabs and self.service.connected)
        if self.matrix_tabs:
            self._clear_matrix_state()
        self._clear_loaded_chain(clear_matrix=False)
        self._start_task(
            "load_ticker",
            lambda: self._load_ticker_task(ticker, clear_active_quotes=clear_active_quotes),
        )

    def _load_ticker_task(self, ticker: str, *, clear_active_quotes: bool = False) -> SearchResult:
        if not self.service.connected:
            raise RuntimeError(
                "Schwab connection was lost before the ticker could load. Wait for reconnection, or use Connect."
            )
        if clear_active_quotes:
            self.service.clear_active_quotes()
        return self.service.search_ticker(ticker)

    def _load_expiration(self) -> None:
        if self.search_result is None:
            return
        display_expiration = self.expiration_var.get().strip()
        raw_expiration = self.expiration_display_to_raw.get(display_expiration)
        if not raw_expiration:
            return
        search_result = self.search_result
        cached_records = OptionsMatrixApp._cached_option_records(self, raw_expiration)
        if cached_records is not None:
            self.option_records = list(cached_records)
            self._populate_strike_controls()
            self._refresh_staged_market_data_quote_status()
            self._set_status("Expiration staged. Click Load matrix to apply input changes.")
            return
        self.option_records = []
        self.low_strike_combo["values"] = []
        self.high_strike_combo["values"] = []
        self.low_strike_var.set("")
        self.high_strike_var.set("")
        self._refresh_width_band_controls()
        self._refresh_staged_market_data_quote_status()
        self._start_task(
            "load_expiration",
            lambda: (raw_expiration, self.service.load_option_records(search_result, raw_expiration)),
        )

    def _on_side_changed(self) -> None:
        self._populate_strike_controls()
        self._refresh_staged_market_data_quote_status()
        self._set_status("Side staged. Click Load matrix to apply input changes.")

    def _staged_width_band_availability(self) -> tuple[float, ...] | None:
        try:
            raw_expiration = self._staged_raw_expiration()
        except (AttributeError, TypeError, ValueError):
            return None
        if raw_expiration is None:
            return None
        try:
            records = OptionsMatrixApp._cached_option_records(self, raw_expiration)
        except (AttributeError, TypeError, ValueError):
            return None
        if records is None:
            return None
        try:
            low = float(self.low_strike_var.get().strip())
            high = float(self.high_strike_var.get().strip())
            view = normalize_matrix_view(self.side_var.get())
        except (AttributeError, TypeError, ValueError):
            return None
        actual_widths = discovered_matrix_view_widths(records, view, low, high)
        return matrix_width_bands(actual_widths)

    def _remember_width_band_preferences(self) -> bool:
        available = set(getattr(self, "available_width_bands", ()))
        if not available:
            return False
        preferences = getattr(self, "_width_band_preferences", None)
        if preferences is None:
            preferences = {band: band in DEFAULT_SELECTED_WIDTH_BANDS for band in MATRIX_WIDTH_BANDS}
            self._width_band_preferences = preferences
        changed = False
        for band, variable in self.width_vars.items():
            if band in available:
                selected = bool(variable.get())
                changed = changed or preferences.get(band, False) != selected
                preferences[band] = selected
        return changed

    def _stage_width_band_preferences(self, matrix_input: MatrixInputState) -> None:
        """Keep global user choices when a cached or reloaded tab is staged."""
        preferences = getattr(self, "_width_band_preferences", None)
        if preferences is None:
            selected_bands = set(_matrix_input_width_bands(matrix_input))
            preferences = {band: band in selected_bands for band in MATRIX_WIDTH_BANDS}
            self._width_band_preferences = preferences
        for band, variable in self.width_vars.items():
            variable.set(bool(preferences.get(band, False)))

    def _refresh_width_band_controls(self) -> None:
        available = self._staged_width_band_availability()
        preferences = getattr(self, "_width_band_preferences", None)
        if preferences is None:
            preferences = {band: band in DEFAULT_SELECTED_WIDTH_BANDS for band in MATRIX_WIDTH_BANDS}
            self._width_band_preferences = preferences
        self._width_band_availability_known = available is not None
        self.available_width_bands = tuple(available or ())

        for band in MATRIX_WIDTH_BANDS:
            variable = self.width_vars[band]
            button = self.width_checkbuttons.get(band)
            if available is None:
                variable.set(bool(preferences.get(band, False)))
                state = tk.DISABLED
            elif band not in available:
                variable.set(False)
                state = tk.DISABLED
            else:
                variable.set(bool(preferences.get(band, False)))
                state = tk.NORMAL
            if button is not None:
                button.configure(state=state)

    def _on_width_band_changed(self) -> None:
        changed = self._remember_width_band_preferences()
        self._on_matrix_input_changed()
        if changed:
            try:
                save_width_band_preferences(
                    self._width_band_preferences,
                    path=getattr(self, "_width_preferences_path", None),
                )
            except OSError:
                self._set_status("Width selection applied, but its preferences could not be saved.")

    def _on_matrix_input_changed(self) -> None:
        self._refresh_width_band_controls()
        self._refresh_staged_market_data_quote_status()
        self._set_status("Click Load matrix to apply input changes.")

    def _load_matrix(self) -> None:
        if self.search_result is None:
            self._set_status("Load a ticker before loading the matrix.")
            return
        try:
            matrix_input = self._read_matrix_input()
        except ValueError:
            self._set_status("Choose a valid expiration, low strike, and high strike.")
            return
        if self._reject_matrix_tab_request(matrix_input):
            return

        cached_records = OptionsMatrixApp._cached_option_records(self, matrix_input.raw_expiration)
        if cached_records is None:
            search_result = self.search_result
            self._pending_matrix_input = matrix_input
            self._start_task(
                "load_matrix_records",
                lambda: (
                    matrix_input.raw_expiration,
                    self.service.load_option_records(search_result, matrix_input.raw_expiration),
                ),
            )
            return

        self._apply_matrix_input(matrix_input, cached_records)

    def _apply_matrix_input(
        self,
        matrix_input: MatrixInputState,
        option_records: Sequence[OptionContractRecord],
    ) -> None:
        existing_tab = self._find_matrix_tab(matrix_input)
        if existing_tab is None and self._reject_matrix_tab_request(matrix_input):
            return
        underlying_price = underlying_reference_price(self.underlying_quote)
        if underlying_price is None and self.search_result is not None:
            # A positive chain reference can seed fair-value moneyness context
            # until the first usable live underlying tick arrives. Visible
            # membership never waits for or depends on either price source.
            chain_price = getattr(self.service, "chain_underlying_price", None)
            if callable(chain_price):
                underlying_price = _positive_finite_price(
                    chain_price(self.search_result, matrix_input.raw_expiration)
                )
        view_plan = build_matrix_view_plan(
            option_records,
            matrix_input,
            underlying_price=underlying_price,
        )
        if view_plan is None:
            self._set_status(
                "Both view requires complete call and put spreads in the enabled width bands and selected range."
                if normalize_matrix_view(matrix_input.side) == MATRIX_VIEW_BOTH
                else "No complete spreads are available in the enabled width bands and selected range."
            )
            return
        OptionsMatrixApp._advance_workspace_generation(self)
        if not view_plan.widths:
            self._set_status("No complete spreads are available in the enabled width bands and selected range.")
            return
        resolved_matrix_input = replace(matrix_input, widths=view_plan.widths)
        is_both = normalize_matrix_view(resolved_matrix_input.side) == MATRIX_VIEW_BOTH
        side_states = (
            {
                right: MatrixSideState(
                    right=right,
                    row_strikes=side_plan.row_strikes,
                    matrix_plan=side_plan.plan,
                )
                for right, side_plan in view_plan.side_plans.items()
            }
            if is_both
            else {}
        )
        primary_plan = (
            MatrixSelectionPlan(visible={}, context_by_width={})
            if is_both
            else next(iter(view_plan.side_plans.values())).plan
        )
        target_selections = union_matrix_quote_target_selections(
            [side_plan.plan for side_plan in view_plan.side_plans.values()]
        )
        leg_quotes = seed_cached_matrix_quotes(
            target_selections,
            self.leg_quote_cache,
        )
        tab_leg_quote_ids = frozenset(leg_quote_ids(target_selections))
        if existing_tab is not None:
            existing_tab.label = matrix_tab_label(resolved_matrix_input)
            existing_tab.matrix_input = resolved_matrix_input
            existing_tab.row_strikes = view_plan.row_strikes
            existing_tab.matrix_plan = primary_plan
            existing_tab.leg_quotes = leg_quotes
            existing_tab.leg_quote_ids = tab_leg_quote_ids
            existing_tab.matrix_snapshots = {}
            existing_tab.ranked_signals = []
            existing_tab.side_states = side_states
            existing_tab.underlying_price_hint = underlying_price
            existing_tab.is_stale = False
            self.active_tab_id = existing_tab.tab_id
            self._rebuild_tab_from_quotes(existing_tab)
            self._sync_view_from_active_tab()
            self._redraw_matrix_tabs()
            self._redraw_all()
            self._save_current_matrix_cache()
            self._set_status(
                f"Updated {existing_tab.label}; loading {len(target_selections)} spread contexts."
            )
            self._sync_quote_subscriptions()
            return

        tab = MatrixTabState(
            tab_id=self._next_tab_id,
            label=matrix_tab_label(resolved_matrix_input),
            matrix_input=resolved_matrix_input,
            row_strikes=view_plan.row_strikes,
            matrix_plan=primary_plan,
            leg_quotes=leg_quotes,
            leg_quote_ids=tab_leg_quote_ids,
            side_states=side_states,
            underlying_price_hint=underlying_price,
        )
        self._next_tab_id += 1
        self.matrix_tabs.append(tab)
        self.active_tab_id = tab.tab_id
        self._rebuild_tab_from_quotes(tab)
        self._sync_view_from_active_tab()
        self._redraw_matrix_tabs()
        self._redraw_all()
        self._save_current_matrix_cache()
        self._set_status(
            f"Loading {len(target_selections)} spread contexts for {tab.label}."
        )
        self._sync_quote_subscriptions()

    def _read_matrix_input(self) -> MatrixInputState:
        display_expiration = self.expiration_var.get().strip()
        raw_expiration = self.expiration_display_to_raw.get(display_expiration, "")
        if not display_expiration or not raw_expiration:
            raise ValueError("missing expiration")
        return parse_matrix_input_state(
            display_expiration=display_expiration,
            raw_expiration=raw_expiration,
            side=self.side_var.get(),
            low_strike=self.low_strike_var.get(),
            high_strike=self.high_strike_var.get(),
            width_states={width: variable.get() for width, variable in self.width_vars.items()},
            ticker_symbol=self.ticker_var.get(),
            matrix_mode=DEFAULT_MATRIX_MODE,
        )

    def _find_matrix_tab(self, matrix_input: MatrixInputState) -> MatrixTabState | None:
        target_identity = matrix_tab_identity(matrix_input)
        for tab in self.matrix_tabs:
            if matrix_tab_identity(tab.matrix_input) == target_identity:
                return tab
        return None

    def _reject_matrix_tab_request(self, matrix_input: MatrixInputState) -> bool:
        existing_tab = self._find_matrix_tab(matrix_input)
        if existing_tab is None:
            conflicting_tab = next(
                (
                    tab
                    for tab in self.matrix_tabs
                    if matrix_tab_views_conflict(tab.matrix_input, matrix_input)
                ),
                None,
            )
            if conflicting_tab is not None:
                message = (
                    f"{matrix_tab_label(matrix_input)} overlaps {conflicting_tab.label}. "
                    "Close the existing CALL/PUT/BOTH view before loading the overlapping view."
                )
                self._show_matrix_tab_error(message)
                return True
        if existing_tab is None and matrix_tab_limit_reached(len(self.matrix_tabs)):
            message = f"Only {MAX_MATRIX_TABS} matrix tabs can be open. Close a tab before loading another matrix."
            self._show_matrix_tab_error(message)
            return True
        return False

    def _show_matrix_tab_error(self, message: str) -> None:
        self._set_status(message)
        messagebox.showerror("Matrix Tabs", message, parent=self.root)

    def _active_tab(self) -> MatrixTabState | None:
        if self.active_tab_id is None:
            return None
        for tab in self.matrix_tabs:
            if tab.tab_id == self.active_tab_id:
                return tab
        return None

    def _sync_view_from_active_tab(self) -> None:
        tab = self._active_tab()
        if tab is None:
            self._clear_active_view_state()
            return
        self.applied_matrix_input = tab.matrix_input
        self.row_strikes = [strike for strike in tab.row_strikes if is_supported_matrix_strike(strike)]
        self.matrix_plan = tab.matrix_plan
        if tab.side_states:
            self.matrix_snapshots = {
                (right, cell_key[0], cell_key[1]): snapshot
                for right, side_state in tab.side_states.items()
                for cell_key, snapshot in side_state.matrix_snapshots.items()
            }
        else:
            self.matrix_snapshots = dict(tab.matrix_snapshots)
        self.selected_cell_key = tab.selected_cell_key
        self._refresh_aggregate_ranked_signals()

    def _clear_active_view_state(self) -> None:
        self.row_strikes = []
        self.applied_matrix_input = None
        self.matrix_plan = None
        self.matrix_snapshots = {}
        self.ranked_signals = []
        self.rank_sort_state = RankSortState()
        self._rank_render_signature = None
        self.selected_cell_key = None

    def _mark_all_matrix_tabs_stale(self) -> None:
        for tab in self.matrix_tabs:
            self._mark_matrix_tab_stale(tab)
        self.leg_quote_cache.clear()
        self.underlying_quote = None
        self.active_market_data_quote_counts = None
        self._combo_target_key = None
        self._combo_subscription_inflight = False
        self._sync_view_from_active_tab()

    @staticmethod
    def _mark_matrix_tab_stale(tab: MatrixTabState) -> None:
        tab.is_stale = True
        tab.leg_quotes.clear()
        tab.leg_quote_ids = frozenset()
        tab.matrix_snapshots.clear()
        tab.ranked_signals.clear()
        for side_state in tab.side_states.values():
            side_state.matrix_snapshots.clear()
            side_state.ranked_signals.clear()

    def _load_cached_workspace_as_stale_tabs(self) -> None:
        workspace = self.cached_matrix_workspace
        if workspace is None or not workspace.tabs:
            return
        self.matrix_tabs = []
        self.active_tab_id = None
        self._next_tab_id = 1
        self.matrix_mode = DEFAULT_MATRIX_MODE
        self.ticker_var.set(workspace.ticker_symbol)
        self.expiration_display_to_raw = {
            format_expiration(tab.raw_expiration): tab.raw_expiration
            for tab in workspace.tabs
        }
        self.expiration_combo["values"] = list(self.expiration_display_to_raw.keys())
        active_identity = workspace.active_tab_identity
        for cached_tab in workspace.tabs:
            matrix_input = MatrixInputState(
                display_expiration=format_expiration(cached_tab.raw_expiration),
                raw_expiration=cached_tab.raw_expiration,
                side=cached_tab.side,
                low_strike=cached_tab.low_strike,
                high_strike=cached_tab.high_strike,
                widths=cached_tab.widths,
                ticker_symbol=workspace.ticker_symbol,
                matrix_mode=DEFAULT_MATRIX_MODE,
                width_bands=matrix_width_bands(cached_tab.widths),
            )
            tab = MatrixTabState(
                tab_id=self._next_tab_id,
                label=cached_tab.label or matrix_tab_label(matrix_input),
                matrix_input=matrix_input,
                row_strikes=cached_tab.row_strikes,
                matrix_plan=MatrixSelectionPlan(visible={}, context_by_width={}),
                selected_cell_key=cached_tab.selected_cell_key,
                is_stale=True,
            )
            self._next_tab_id += 1
            self.matrix_tabs.append(tab)
            if active_identity is not None and matrix_tab_identity(matrix_input) == active_identity:
                self.active_tab_id = tab.tab_id
        if self.matrix_tabs and self.active_tab_id is None:
            self.active_tab_id = self.matrix_tabs[0].tab_id
        active_tab = self._active_tab()
        if active_tab is not None:
            self._sync_controls_from_stale_matrix_input(active_tab.matrix_input)
        self._sync_view_from_active_tab()
        self._redraw_matrix_tabs()
        self._redraw_all()
        self._set_status("Cached matrix tabs loaded. Click Reload matrix to request live quotes.")

    def _refresh_aggregate_ranked_signals(self) -> None:
        self.ranked_signals = build_aggregate_ranked_signals(
            [tab for tab in self.matrix_tabs if not tab.is_stale],
            underlying_reference_price(self.underlying_quote),
        )

    def _save_current_matrix_cache(self, *, clear_when_empty: bool = False) -> None:
        workspace = cached_matrix_workspace_from_tabs(self.matrix_tabs, self.active_tab_id)
        if workspace is None:
            if clear_when_empty:
                self._clear_cached_matrix_workspace()
            return
        self.cached_matrix_workspace = workspace
        try:
            save_matrix_workspace_cache(workspace, path=getattr(self, "_workspace_cache_path", None))
        except OSError:
            pass

    def _clear_cached_matrix_workspace(self) -> None:
        self.cached_matrix_workspace = None
        try:
            clear_matrix_workspace_cache(path=getattr(self, "_workspace_cache_path", None))
        except OSError:
            pass

    def _apply_formula_settings(
        self,
        settings: FormulaSettings,
        *,
        save: bool,
        status_message: str | None = None,
    ) -> bool:
        changed = settings != self.formula_settings
        signal_method = getattr(self, "signal_method", DEFAULT_SIGNAL_METHOD)
        self.formula_settings = settings
        self.snapshot_row_builder = SnapshotRowBuilder(
            self.formula_settings,
            signal_method=signal_method,
        )
        save_error: OSError | None = None
        if save:
            try:
                save_formula_settings(settings, path=getattr(self, "_settings_path", None), signal_method=signal_method)
            except OSError as exc:
                save_error = exc

        if changed:
            for tab in self.matrix_tabs:
                if tab.is_stale:
                    tab.matrix_snapshots = {}
                    tab.ranked_signals = []
                    for side_state in tab.side_states.values():
                        side_state.matrix_snapshots = {}
                        side_state.ranked_signals = []
                else:
                    self._rebuild_tab_from_quotes(tab)
            if hasattr(self, "positions"):
                self._refresh_position_alerts()
            self._sync_view_from_active_tab()
            self._redraw_all()
            self._refresh_staged_market_data_quote_status()
            self._save_current_matrix_cache()

        if save_error is not None:
            message = f"Settings applied but could not be saved: {save_error}"
            self._set_status(message)
            messagebox.showerror("Settings", message, parent=self.root)
            return True
        if status_message is not None:
            self._set_status(status_message)
        return True

    def _redraw_matrix_tabs(self) -> None:
        for child in self.matrix_tabs_frame.winfo_children():
            child.destroy()
        if not self.matrix_tabs:
            tk.Label(
                self.matrix_tabs_frame,
                text="(no tabs)",
                bg=GRID_BG,
                fg=MUTED,
                font=self.grid_font,
            ).grid(row=0, column=0, sticky="w")
            return

        for index, tab in enumerate(self.matrix_tabs):
            is_active = tab.tab_id == self.active_tab_id
            bg = SELECTED_BG if is_active else HEADER_BG
            tab_frame = tk.Frame(
                self.matrix_tabs_frame, bg=bg, bd=0, highlightthickness=1,
                highlightbackground=NAVY if is_active else GRID_LINE,
                highlightcolor=NAVY if is_active else GRID_LINE,
            )
            tab_frame.grid(row=0, column=index, padx=(0, 4), sticky="w")
            label = tk.Label(
                tab_frame,
                text=tab.label,
                bg=bg,
                fg=INK,
                font=self.grid_font_bold if is_active else self.grid_font,
                padx=6,
                pady=2,
            )
            label.pack(side=tk.LEFT)
            close = tk.Label(
                tab_frame,
                text="x",
                bg=bg,
                fg=MUTED,
                font=self.grid_font_bold,
                padx=5,
                pady=2,
            )
            close.pack(side=tk.LEFT)
            tab_frame.bind("<Button-1>", lambda _event, tab_id=tab.tab_id: self._activate_matrix_tab(tab_id))
            label.bind("<Button-1>", lambda _event, tab_id=tab.tab_id: self._activate_matrix_tab(tab_id))
            close.bind("<Button-1>", lambda _event, tab_id=tab.tab_id: self._close_matrix_tab(tab_id))

    def _activate_matrix_tab(self, tab_id: int) -> None:
        if self.active_tab_id == tab_id:
            return
        if not any(tab.tab_id == tab_id for tab in self.matrix_tabs):
            return
        self.active_tab_id = tab_id
        self._sync_view_from_active_tab()
        self._redraw_matrix_tabs()
        self._redraw_all()
        self._refresh_staged_market_data_quote_status()

    def _close_matrix_tab(self, tab_id: int) -> None:
        close_index = next((index for index, tab in enumerate(self.matrix_tabs) if tab.tab_id == tab_id), None)
        if close_index is None:
            return
        was_active = self.active_tab_id == tab_id
        closed_tab = self.matrix_tabs.pop(close_index)
        if was_active:
            if self.matrix_tabs:
                next_index = min(close_index, len(self.matrix_tabs) - 1)
                self.active_tab_id = self.matrix_tabs[next_index].tab_id
            else:
                self.active_tab_id = None
        self._sync_view_from_active_tab()
        self._redraw_matrix_tabs()
        self._redraw_all()
        self._refresh_staged_market_data_quote_status()
        self._save_current_matrix_cache(clear_when_empty=True)
        self._set_status(f"Closed matrix tab {closed_tab.label}.")
        self._sync_quote_subscriptions(force=True)

    def _refresh_staged_market_data_quote_status(self) -> None:
        current_selections = union_matrix_tab_quote_target_selections(self.matrix_tabs)
        try:
            matrix_input = self._read_matrix_input()
        except ValueError:
            self._set_market_data_quote_status(market_data_quote_status_for_selections(current_selections))
            return

        existing_tab = self._find_matrix_tab(matrix_input)
        if existing_tab is None and any(
            matrix_tab_views_conflict(tab.matrix_input, matrix_input)
            for tab in self.matrix_tabs
        ):
            self._set_market_data_quote_status(market_data_quote_status_for_selections(current_selections))
            return

        option_records = OptionsMatrixApp._cached_option_records(self, matrix_input.raw_expiration)
        if option_records is None:
            self._set_market_data_quote_status(market_data_quote_status_for_selections(current_selections))
            return

        underlying_price = underlying_reference_price(self.underlying_quote)
        if underlying_price is None and self.search_result is not None:
            chain_price = getattr(self.service, "chain_underlying_price", None)
            if callable(chain_price):
                underlying_price = chain_price(self.search_result, matrix_input.raw_expiration)

        view_plan = build_matrix_view_plan(
            option_records,
            matrix_input,
            underlying_price=underlying_price,
        )
        if view_plan is None:
            self._set_market_data_quote_status(market_data_quote_status_for_selections(current_selections))
            return
        if matrix_tab_limit_reached(len(self.matrix_tabs)):
            if existing_tab is None:
                self._set_market_data_quote_status(market_data_quote_status_for_selections(current_selections))
                return
        replacement_plans = [side_plan.plan for side_plan in view_plan.side_plans.values()]
        preview_plans = [
            plan
            for tab in self.matrix_tabs
            if not tab.is_stale and tab is not existing_tab
            for plan in matrix_plans_for_tab(tab)
        ] + replacement_plans
        preview_selections = union_matrix_quote_target_selections(preview_plans)
        current_combo_keys, current_leg_ids = _market_data_quote_sets(current_selections)
        preview_combo_keys, preview_leg_ids = _market_data_quote_sets(preview_selections)
        self._set_market_data_quote_status(
            _market_data_quote_status_from_sets(
                current_combo_keys,
                current_leg_ids,
                preview_combo_keys,
                preview_leg_ids,
                preview_action="updated" if existing_tab is not None else "loaded",
            )
        )

    def _records_for_selected_side(self) -> list[OptionContractRecord]:
        rights = set(matrix_view_rights(self.side_var.get()))
        return [record for record in self.option_records if record.right in rights]

    def _advance_workspace_generation(self) -> int:
        generation = int(getattr(self, "_workspace_generation", 0)) + 1
        self._workspace_generation = generation
        self._dirty_tab_ids = set()
        service = getattr(self, "service", None)
        advance_quote_generation = getattr(service, "advance_quote_generation", None)
        if callable(advance_quote_generation):
            self._quote_event_generation = int(advance_quote_generation())
        else:
            self._quote_event_generation = int(getattr(self, "_quote_event_generation", 0)) + 1
        self._subscription_diagnostics_by_con_id = {}
        self._subscription_last_states = {}
        self._subscription_failure_counts = {}
        return generation

    def _current_ticker_symbol(self) -> str:
        search_result = getattr(self, "search_result", None)
        underlying = getattr(search_result, "underlying", None)
        symbol = getattr(underlying, "symbol", None)
        if symbol:
            return str(symbol).strip().upper()
        ticker_var = getattr(self, "ticker_var", None)
        if ticker_var is not None and hasattr(ticker_var, "get"):
            ticker = str(ticker_var.get()).strip().upper()
            if ticker:
                return ticker
        matrix_input = getattr(self, "applied_matrix_input", None) or getattr(
            self,
            "_pending_matrix_input",
            None,
        )
        if matrix_input is not None:
            return str(matrix_input.ticker_symbol).strip().upper()
        active_tab_id = getattr(self, "active_tab_id", None)
        for tab in getattr(self, "matrix_tabs", ()):
            if active_tab_id is None or tab.tab_id == active_tab_id:
                return str(tab.matrix_input.ticker_symbol).strip().upper()
        return ""

    def _cached_option_records(self, raw_expiration: str) -> list[OptionContractRecord] | None:
        key = (OptionsMatrixApp._current_ticker_symbol(self), raw_expiration)
        cache = getattr(self, "option_records_by_expiration", {})
        records = cache.get(key)
        if records is None:
            # Compatibility for state produced before option caches were ticker-scoped.
            records = cache.get(raw_expiration)
        return list(records) if records is not None else None

    def _cache_option_records(
        self,
        raw_expiration: str,
        records: Sequence[OptionContractRecord],
    ) -> list[OptionContractRecord]:
        cached = list(records)
        key = (OptionsMatrixApp._current_ticker_symbol(self), raw_expiration)
        self.option_records_by_expiration[key] = cached
        return cached

    def _applied_widths(self) -> tuple[float, ...]:
        if self.applied_matrix_input is None:
            return DEFAULT_WIDTHS
        return tuple(
            width for width in self.applied_matrix_input.widths
            if is_supported_spread_width(width)
        )

    def _matrix_layout_slot_widths(self) -> tuple[float, ...] | None:
        if self.applied_matrix_input is None:
            return None
        return tuple(
            width for width in self.applied_matrix_input.widths
            if is_supported_spread_width(width)
        )

    def _start_task(
        self,
        kind: str,
        func: Callable[[], Any],
        *,
        announce: bool = True,
    ) -> None:
        self._task_token += 1
        token = self._task_token
        self._task_tokens[kind] = token
        generation = int(getattr(self, "_workspace_generation", 0))
        if announce:
            self._set_status(f"{kind.replace('_', ' ').title()} ...")

        def runner() -> None:
            try:
                value = func()
                self.worker_events.put(
                    _WorkerResult(kind=kind, token=token, generation=generation, value=value)
                )
            except Exception as exc:
                self.worker_events.put(
                    _WorkerResult(kind=kind, token=token, generation=generation, error=str(exc))
                )

        threading.Thread(target=runner, name=f"OptionsMatrix-{kind}", daemon=True).start()

    def _poll_queues(self) -> None:
        self._poll_service_events()
        self._poll_worker_events()
        if not self._shutdown_in_progress:
            self.root.after(UI_POLL_INTERVAL_MS, self._poll_queues)

    def _poll_service_events(self) -> None:
        processed = 0
        while processed < 800:
            try:
                event = self.service.events.get_nowait()
            except queue.Empty:
                break
            processed += 1
            event_type = event.get("type")
            event_generation = event.get("generation")
            if (
                event_generation is not None
                and int(event_generation) != int(getattr(self, "_quote_event_generation", 0))
            ):
                continue
            if event_type == "status":
                message = str(event.get("message", ""))
                connected = event.get("connected")
                if isinstance(connected, bool):
                    was_connected = bool(getattr(self, "_service_connected", False))
                    connection_active = event.get("connection_active")
                    self._connection_active = (
                        connection_active
                        if isinstance(connection_active, bool)
                        else connected
                    )
                    if message.strip():
                        self._connection_status_message = message
                    self._set_connected_state(connected)
                    self._service_connected = connected
                    if connected and not was_connected:
                        sync_subscriptions = getattr(self, "_sync_quote_subscriptions", None)
                        if callable(sync_subscriptions):
                            sync_subscriptions()
                if message.strip() and not is_routine_reconnect_schedule_message(message):
                    self._set_status(message)
            elif event_type == "quote":
                quote = event.get("quote")
                if isinstance(quote, QuoteSnapshot):
                    self._handle_quote(quote)
            elif event_type == "subscription_status":
                subscriptions = event.get("subscriptions")
                if isinstance(subscriptions, SequenceABC):
                    self._handle_subscription_status(subscriptions)

    def _handle_subscription_status(self, subscriptions: Sequence[object]) -> None:
        diagnostics: dict[int, object] = {}
        prior_states = getattr(self, "_subscription_last_states", {})
        failure_counts = getattr(self, "_subscription_failure_counts", {})
        next_states: dict[int, str] = {}
        for diagnostic in subscriptions:
            con_id = getattr(diagnostic, "contract_con_id", None)
            if not isinstance(con_id, int):
                continue
            diagnostic_generation = getattr(diagnostic, "generation", None)
            if (
                diagnostic_generation is not None
                and int(diagnostic_generation) != int(getattr(self, "_quote_event_generation", 0))
            ):
                continue
            state = str(getattr(diagnostic, "state", "unknown")).strip().lower()
            diagnostics[con_id] = diagnostic
            next_states[con_id] = state
            if state == "failed" and prior_states.get(con_id) != "failed":
                failure_counts[con_id] = failure_counts.get(con_id, 0) + 1
        self._subscription_diagnostics_by_con_id = diagnostics
        self._subscription_last_states = next_states
        self._subscription_failure_counts = failure_counts
        services = getattr(self, "services", None)
        if services is not None:
            services.on_diagnostics(diagnostics.values())
        if hasattr(self, "positions"):
            self._refresh_position_alerts()
        if getattr(self, "active_tab_id", None) is not None:
            self._refresh_active_matrix_render()

    def _poll_worker_events(self) -> None:
        while True:
            try:
                result = self.worker_events.get_nowait()
            except queue.Empty:
                break
            if result.token != self._task_tokens.get(result.kind):
                # A newer task of the same kind superseded this result.
                continue
            current_generation = int(getattr(self, "_workspace_generation", 0))
            if (
                result.kind not in CONNECTION_TASK_KINDS
                and int(getattr(result, "generation", current_generation)) != current_generation
            ):
                # The ticker/workspace changed while this task was running.
                self._discard_stale_worker_result(result.kind)
                continue
            if result.error is not None:
                if result.kind in CONNECTION_TASK_KINDS:
                    OptionsMatrixApp._clear_connection_task_inflight(self, result.kind)
                    service = getattr(self, "service", None)
                    connected = bool(getattr(service, "connected", False))
                    self._connection_active = bool(
                        getattr(service, "connection_active", connected)
                    )
                    self._set_connected_state(connected)
                if result.kind == "subscribe_quotes":
                    self._combo_subscription_inflight = False
                    self._combo_target_key = None
                if result.kind == "start_recording":
                    self._recorder_start_inflight = False
                    self._recorder_error_message = result.error
                    self.recorder_status_var.set(result.error)
                    self._refresh_recorder_controls()
                if result.kind == "record_snapshot":
                    self._recorder_snapshot_inflight = False
                    self._recorder_error_message = result.error
                    self.recorder_status_var.set(result.error)
                    self._set_status(result.error)
                    self._stop_recording(silent=True)
                    continue
                if result.kind == "reload_matrix_tab":
                    self._restore_workspace_inflight = False
                    self._set_connected_state(self.service.connected)
                    self._redraw_matrix()
                self._set_status(result.error)
                continue
            self._handle_worker_result(result)

    def _clear_connection_task_inflight(self, kind: str) -> None:
        if kind == "connect":
            self._connect_inflight = False
        elif kind == "disconnect":
            self._disconnect_inflight = False

    def _discard_stale_worker_result(self, kind: str) -> None:
        if kind in CONNECTION_TASK_KINDS:
            OptionsMatrixApp._clear_connection_task_inflight(self, kind)
        elif kind == "subscribe_quotes":
            self._combo_subscription_inflight = False
            self._combo_target_key = None
            sync_subscriptions = getattr(self, "_sync_quote_subscriptions", None)
            if callable(sync_subscriptions):
                sync_subscriptions()
        elif kind == "start_recording":
            self._recorder_start_inflight = False
        elif kind == "record_snapshot":
            self._recorder_snapshot_inflight = False
        elif kind == "reload_matrix_tab":
            self._restore_workspace_inflight = False

    def _handle_worker_result(self, result: _WorkerResult) -> None:
        if result.kind == "connect":
            OptionsMatrixApp._clear_connection_task_inflight(self, "connect")
            service = getattr(self, "service", None)
            connected = bool(getattr(service, "connected", False))
            self._connection_active = bool(
                getattr(service, "connection_active", connected)
            )
            self._set_connected_state(connected)
            self._set_status(
                "Connected."
                if connected
                else "Connection attempt finished, but the Schwab streamer is not connected."
            )
            return
        if result.kind == "disconnect":
            OptionsMatrixApp._clear_connection_task_inflight(self, "disconnect")
            self._save_current_matrix_cache()
            service = getattr(self, "service", None)
            connected = bool(getattr(service, "connected", False))
            self._connection_active = bool(
                getattr(service, "connection_active", connected)
            )
            self._set_connected_state(connected)
            self._clear_loaded_chain(clear_matrix=False)
            self._mark_all_matrix_tabs_stale()
            active_tab = self._active_tab()
            if active_tab is not None:
                self._sync_controls_from_stale_matrix_input(active_tab.matrix_input)
            self._set_market_data_quote_status(None)
            self._redraw_matrix_tabs()
            self._redraw_all()
            self._set_status(
                "Disconnected."
                if not connected
                else "Disconnect completed, but the Schwab streamer still reports connected."
            )
            return
        if result.kind == "reload_matrix_tab":
            self._restore_workspace_inflight = False
            self._set_connected_state(self.service.connected)
            self._apply_reloaded_matrix_tab(result.value)
            return
        if result.kind == "load_ticker":
            self.search_result = result.value
            self._populate_expirations()
            self._set_status(f"Loaded expirations for {self.search_result.underlying.symbol}.")
            return
        if result.kind == "load_expiration":
            raw_expiration, records = result.value
            records = OptionsMatrixApp._cache_option_records(self, raw_expiration, records)
            if raw_expiration == self._staged_raw_expiration():
                self.option_records = records
                self._populate_strike_controls()
                self._refresh_staged_market_data_quote_status()
            self._set_status(f"Loaded {len(records)} option contracts. Click Load matrix to apply input changes.")
            return
        if result.kind == "load_matrix_records":
            raw_expiration, records = result.value
            records = OptionsMatrixApp._cache_option_records(self, raw_expiration, records)
            if raw_expiration == self._staged_raw_expiration():
                self.option_records = records
                self._populate_strike_controls()
            pending_input = self._pending_matrix_input
            self._pending_matrix_input = None
            if pending_input is not None and pending_input.raw_expiration == raw_expiration:
                self._apply_matrix_input(pending_input, records)
            else:
                self._refresh_staged_market_data_quote_status()
            return
        if result.kind == "subscribe_quotes":
            self._combo_subscription_inflight = False
            self._sync_quote_subscriptions()
            return
        if result.kind == "clear_quotes":
            self._set_status("Cleared inactive market-data subscriptions.")
            return
        if result.kind == "start_recording":
            self._finish_start_recording(result.value)
            return
        if result.kind == "record_snapshot":
            self._finish_record_snapshot(result.value)
            return

    def _finish_record_snapshot(self, result: _RecordSnapshotResult) -> None:
        self._recorder_snapshot_inflight = False
        recorder = self.recorder
        if (
            recorder is None
            or recorder is not result.recorder
            or recorder.session_id != result.session_id
            or getattr(self, "_recorder_stop_inflight", False)
            or not recorder.running
        ):
            return
        try:
            recorder.record_rows(result.rows)
        except RuntimeError as exc:
            message = str(exc)
            self._recorder_error_message = message
            self.recorder_status_var.set(message)
            self._set_status(message)
            self._stop_recording(silent=True)
            return
        self._refresh_recorder_controls()

    def _apply_reloaded_matrix_tab(self, result: _ReloadMatrixTabResult) -> None:
        tab = next((candidate for candidate in self.matrix_tabs if candidate.tab_id == result.tab_id), None)
        if tab is None:
            self._set_status("Cached matrix tab is no longer available.")
            return
        self.search_result = result.search_result
        self.ticker_var.set(tab.matrix_input.ticker_symbol)
        self._populate_expiration_values()
        self.option_records = OptionsMatrixApp._cache_option_records(
            self,
            tab.matrix_input.raw_expiration,
            result.records,
        )
        underlying_price = underlying_reference_price(self.underlying_quote)
        if underlying_price is None:
            chain_price = getattr(self.service, "chain_underlying_price", None)
            if callable(chain_price):
                underlying_price = _positive_finite_price(
                    chain_price(result.search_result, tab.matrix_input.raw_expiration)
                )
        view_plan = build_matrix_view_plan(
            result.records,
            tab.matrix_input,
            underlying_price=underlying_price,
        )
        if view_plan is None:
            self._set_status(
                f"Cached BOTH matrix requires valid complete call and put spreads: {tab.label}."
                if normalize_matrix_view(tab.matrix_input.side) == MATRIX_VIEW_BOTH
                else f"Cached matrix tab has no valid complete spreads in its selected range: {tab.label}."
            )
            return
        resolved_matrix_input = replace(tab.matrix_input, widths=view_plan.widths)
        is_both = normalize_matrix_view(resolved_matrix_input.side) == MATRIX_VIEW_BOTH
        side_states = (
            {
                right: MatrixSideState(
                    right=right,
                    row_strikes=side_plan.row_strikes,
                    matrix_plan=side_plan.plan,
                )
                for right, side_plan in view_plan.side_plans.items()
            }
            if is_both
            else {}
        )
        primary_plan = (
            MatrixSelectionPlan(visible={}, context_by_width={})
            if is_both
            else next(iter(view_plan.side_plans.values())).plan
        )
        target_selections = union_matrix_quote_target_selections(
            [side_plan.plan for side_plan in view_plan.side_plans.values()]
        )
        leg_quotes = seed_cached_matrix_quotes(
            target_selections,
            self.leg_quote_cache,
        )
        previous_selected_cell = tab.selected_cell_key
        tab.matrix_input = resolved_matrix_input
        tab.label = matrix_tab_label(resolved_matrix_input)
        tab.row_strikes = view_plan.row_strikes
        tab.matrix_plan = primary_plan
        tab.leg_quotes = leg_quotes
        tab.leg_quote_ids = frozenset(leg_quote_ids(target_selections))
        tab.matrix_snapshots = {}
        tab.ranked_signals = []
        tab.side_states = side_states
        tab.underlying_price_hint = underlying_price
        tab.selected_cell_key = previous_selected_cell
        tab.is_stale = False
        self.active_tab_id = tab.tab_id
        self._rebuild_tab_from_quotes(tab)
        self._sync_controls_from_matrix_input(tab.matrix_input)
        self._sync_view_from_active_tab()
        self._redraw_matrix_tabs()
        self._redraw_all()
        self._sync_quote_subscriptions(force=True)
        self._save_current_matrix_cache()
        self._set_status(f"Reloaded matrix {tab.label}.")

    def _handle_quote(self, quote: QuoteSnapshot) -> None:
        if quote.kind == QUOTE_KIND_UNDERLYING:
            search_result = getattr(self, "search_result", None)
            if search_result is None:
                return
            expected_symbol = str(search_result.underlying.symbol).strip().upper()
            expected_con_id = search_result.underlying.con_id
            quote_symbol = str(quote.label).strip().upper()
            if quote_symbol != expected_symbol or (
                quote.contract_con_id is not None
                and quote.contract_con_id != expected_con_id
            ):
                return
            self.underlying_quote = quote
            services = getattr(self, "services", None)
            if services is not None:
                services.on_quote(quote)
            self._dirty_tab_ids.update(
                tab.tab_id for tab in self.matrix_tabs if not tab.is_stale
            )
            self._schedule_quote_redraw()
            return
        if quote.kind == QUOTE_KIND_LEG and quote.contract_con_id is not None:
            self.leg_quote_cache[quote.contract_con_id] = quote
            services = getattr(self, "services", None)
            if services is not None:
                services.on_quote(quote)
            for tab in self.matrix_tabs:
                if tab.is_stale:
                    continue
                if quote.contract_con_id in tab.leg_quote_ids:
                    tab.leg_quotes[quote.contract_con_id] = quote
                    self._dirty_tab_ids.add(tab.tab_id)
            if self._dirty_tab_ids:
                self._schedule_quote_redraw()

    def _schedule_quote_redraw(self) -> None:
        if self._quote_redraw_after_id is not None:
            return
        self._quote_redraw_after_id = self.root.after(QUOTE_REDRAW_INTERVAL_MS, self._refresh_matrix_quotes)

    def _rebuild_tab_from_quotes(self, tab: MatrixTabState) -> None:
        if tab.is_stale:
            tab.matrix_snapshots = {}
            tab.ranked_signals = []
            for side_state in tab.side_states.values():
                side_state.matrix_snapshots = {}
                side_state.ranked_signals = []
            return
        underlying_price = matrix_tab_underlying_reference_price(tab, self.underlying_quote)
        if tab.side_states:
            tab.matrix_snapshots = {}
            tab.ranked_signals = []
            for right in (RIGHT_PUT, RIGHT_CALL):
                side_state = tab.side_states.get(right)
                if side_state is None:
                    continue
                side_state.matrix_snapshots = build_matrix_snapshots(
                    side_state.matrix_plan,
                    tab.leg_quotes,
                    formula_settings=self.formula_settings,
                    signal_method=self.signal_method,
                    underlying_price=underlying_price,
                )
                side_state.ranked_signals = build_ranked_matrix_signals(
                    side_state.matrix_snapshots,
                    signal_method=self.signal_method,
                )
                tab.ranked_signals.extend(side_state.ranked_signals)
            self._ensure_tab_selected_cell(tab)
            return
        tab.matrix_snapshots = build_matrix_snapshots(
            tab.matrix_plan,
            tab.leg_quotes,
            formula_settings=self.formula_settings,
            signal_method=self.signal_method,
            underlying_price=underlying_price,
        )
        tab.ranked_signals = build_ranked_matrix_signals(
            tab.matrix_snapshots,
            signal_method=self.signal_method,
        )
        self._ensure_tab_selected_cell(tab)

    def _refresh_matrix_quotes(self) -> None:
        self._quote_redraw_after_id = None
        dirty_tab_ids = set(getattr(self, "_dirty_tab_ids", ()))
        self._dirty_tab_ids.clear()
        if not dirty_tab_ids:
            return
        for tab in self.matrix_tabs:
            if tab.is_stale or tab.tab_id not in dirty_tab_ids:
                continue
            self._rebuild_tab_from_quotes(tab)
        positions_changed = self._refresh_position_alerts() if hasattr(self, "positions") else False
        self._sync_view_from_active_tab()
        if self.active_tab_id in dirty_tab_ids or positions_changed:
            self._refresh_active_matrix_render()
            self._redraw_selected_detail()
        self._redraw_rank_table_if_changed()

    def _sync_quote_subscriptions(self, *, force: bool = False) -> None:
        if self._combo_subscription_inflight:
            return
        target_selections = self._all_matrix_tab_quote_selections()
        self.active_market_data_quote_counts = (
            market_data_quote_counts(target_selections)
            if target_selections
            else None
        )
        self._refresh_staged_market_data_quote_status()
        target_key = subscription_target_key(target_selections)
        if not force and target_key == self._combo_target_key:
            return
        if self.search_result is None:
            self._combo_target_key = target_key
            return
        if not self.service.connected:
            # Keep the target pending. A typed connected-status transition
            # retries it after the streamer finishes reconnecting.
            self._combo_target_key = None
            return
        self._combo_target_key = target_key
        self._combo_subscription_inflight = True

        def task() -> None:
            self.service.subscribe_matrix_quotes(self.search_result, target_selections)

        self._start_task("subscribe_quotes", task)

    def _open_settings_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Strategy Settings")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(True, True)
        dialog.minsize(940, 650)

        value_vars = {
            control.key: tk.StringVar(
                value=format_formula_setting_value(getattr(self.formula_settings, control.key))
            )
            for control in EDITABLE_FORMULA_SETTING_CONTROLS
        }

        container = ttk.Frame(dialog, padding=10)
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)

        settings_tabs = ttk.Notebook(container)
        settings_tabs.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        method_frame = ttk.Frame(settings_tabs, padding=6)
        cell_guide_frame = ttk.Frame(settings_tabs, padding=6)
        settings_tabs.add(method_frame, text="Fair value")
        settings_tabs.add(cell_guide_frame, text="Matrix cell guide")
        method_frame.columnconfigure(0, weight=1)
        method_frame.rowconfigure(2, weight=1)
        cell_guide_frame.columnconfigure(0, weight=1)
        cell_guide_frame.columnconfigure(1, weight=0)
        cell_guide_frame.rowconfigure(1, weight=1)

        read_only = bool(getattr(self, "settings_read_only", False))

        def add_editable_control_row(frame: ttk.LabelFrame, control: FormulaSettingControl, row: int) -> None:
            ttk.Label(frame, text=control.label).grid(row=row, column=0, sticky="w", padx=(0, 6), pady=2)
            entry = ttk.Entry(frame, textvariable=value_vars[control.key], width=9)
            if read_only:
                entry.configure(state="readonly")
            entry.grid(row=row, column=1, sticky="w", padx=(0, 8), pady=2)
            default_value = getattr(DEFAULT_FORMULA_SETTINGS, control.key)
            ttk.Label(
                frame,
                text=f"Default {format_formula_setting_value(default_value)}",
                foreground=MUTED,
            ).grid(row=row, column=2, sticky="w", padx=(0, 8), pady=2)
            ttk.Button(
                frame,
                text="Reset",
                command=lambda key=control.key: value_vars[key].set(
                    format_formula_setting_value(getattr(DEFAULT_FORMULA_SETTINGS, key))
                ),
                state=tk.DISABLED if read_only else tk.NORMAL,
            ).grid(row=row, column=3, sticky="w", padx=(0, 8), pady=2)
            ttk.Label(
                frame,
                text=control.purpose,
                foreground=MUTED,
                wraplength=390,
            ).grid(row=row, column=4, sticky="w", pady=2)

        fair_value_overview = ttk.LabelFrame(method_frame, text="Overview", padding=8)
        fair_value_overview.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        fair_value_overview.columnconfigure(0, weight=1)
        for overview_row, (title, description) in enumerate(FAIR_VALUE_SYSTEM_OVERVIEW):
            ttk.Label(
                fair_value_overview,
                text=f"{title}: {description}",
                wraplength=890,
                foreground=INK_2,
                justify=tk.LEFT,
            ).grid(
                row=overview_row,
                column=0,
                sticky="w",
                pady=(0 if overview_row == 0 else 3, 0),
            )

        fair_value_settings = ttk.LabelFrame(method_frame, text="Fair Value Edge Curve", padding=8)
        fair_value_settings.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        fair_value_settings.columnconfigure(4, weight=1)
        for row, control in enumerate(EDITABLE_FORMULA_SETTING_CONTROLS):
            add_editable_control_row(fair_value_settings, control, row)

        fair_value_example = ttk.LabelFrame(method_frame, text="How Fair Value Is Built", padding=6)
        fair_value_example.grid(row=2, column=0, sticky="nsew")
        fair_value_example.columnconfigure(0, weight=1)
        fair_value_canvas = tk.Canvas(
            fair_value_example,
            width=920,
            height=300,
            bg=GRID_BG,
            highlightthickness=0,
        )
        fair_value_canvas.grid(row=0, column=0, sticky="nsew")

        ttk.Label(
            cell_guide_frame,
            text=(
                "A cell's fill, border, text, and right-edge liquidity gauge are independent "
                "layers. A navy dot marks a saved position. Its SIGNAL/PRICE half illuminates when its live exit target "
                "is reached. Fair-value signal text continues to update independently. "
                "The samples below describe every matrix state."
            ),
            wraplength=900,
            foreground=INK_2,
            justify=tk.LEFT,
        ).grid(row=0, column=0, sticky="ew", pady=(0, 6))
        cell_guide_canvas = tk.Canvas(
            cell_guide_frame,
            width=920,
            height=480,
            bg=GRID_BG,
            highlightthickness=0,
        )
        cell_guide_canvas.grid(row=1, column=0, sticky="nsew")
        cell_guide_scrollbar = ttk.Scrollbar(
            cell_guide_frame,
            orient=tk.VERTICAL,
            command=cell_guide_canvas.yview,
        )
        cell_guide_scrollbar.grid(row=1, column=1, sticky="ns")
        cell_guide_canvas.configure(yscrollcommand=cell_guide_scrollbar.set)

        def staged_settings() -> FormulaSettings:
            values = formula_settings_to_dict(self.formula_settings)
            values.update({key: value_var.get() for key, value_var in value_vars.items()})
            return formula_settings_from_mapping(values)

        def redraw_examples(*_args: object) -> None:
            settings = staged_settings()
            self._draw_fair_value_system_settings_examples(fair_value_canvas, settings)

        for value_var in value_vars.values():
            value_var.trace_add("write", redraw_examples)

        def reset_all() -> None:
            for control in EDITABLE_FORMULA_SETTING_CONTROLS:
                value_vars[control.key].set(
                    format_formula_setting_value(getattr(DEFAULT_FORMULA_SETTINGS, control.key))
                )

        def apply() -> None:
            try:
                values = {
                    control.key: format_formula_setting_value(getattr(self.formula_settings, control.key))
                    for control in FORMULA_SETTING_CONTROLS
                }
                values.update({key: value_var.get() for key, value_var in value_vars.items()})
                settings = parse_formula_settings_values(values)
            except ValueError as exc:
                messagebox.showerror("Settings", str(exc), parent=dialog)
                return
            self._apply_formula_settings(
                settings,
                save=True,
                status_message="Formula settings applied to all matrix tabs.",
            )
            dialog.destroy()

        buttons = ttk.Frame(container)
        buttons.grid(row=1, column=0, sticky="ew")
        if read_only:
            ttk.Label(
                buttons,
                text="These settings come from the mini PC so both PCs show the same numbers. Change them there.",
                foreground=NAVY,
            ).pack(side=tk.LEFT)
            ttk.Button(buttons, text="Close", command=dialog.destroy).pack(side=tk.RIGHT)
        else:
            ttk.Button(buttons, text="Reset editable defaults", command=reset_all).pack(side=tk.LEFT)
            ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
            ttk.Button(buttons, text="Apply", command=apply).pack(side=tk.RIGHT, padx=(0, 8))

        redraw_examples()
        self._draw_matrix_cell_settings_guide(cell_guide_canvas)
        fair_value_canvas.bind("<Configure>", redraw_examples)
        cell_guide_canvas.bind(
            "<Configure>",
            lambda _event: self._draw_matrix_cell_settings_guide(cell_guide_canvas),
        )
        dialog.bind("<Return>", lambda _event: apply())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.wait_visibility()
        dialog.focus_set()

    def _draw_fair_value_system_settings_examples(
        self,
        canvas: tk.Canvas,
        settings: FormulaSettings,
    ) -> None:
        canvas.delete("all")
        width = max(canvas.winfo_width(), 920)
        data = fair_value_settings_example_data(settings)

        plot_left = 42.0
        plot_right = min(560.0, width * 0.61)
        plot_top = 68.0
        plot_bottom = 205.0
        x_values = [point[0] for point in data.fitted_points]
        x_min, x_max = min(x_values), max(x_values)
        y_min, y_max = 0.0, 1.0

        def plot_x(value: float) -> float:
            return plot_left + ((value - x_min) / (x_max - x_min)) * (plot_right - plot_left)

        def plot_y(value: float) -> float:
            return plot_bottom - ((value - y_min) / (y_max - y_min)) * (plot_bottom - plot_top)

        canvas.create_text(
            plot_left,
            12,
            text="1  Normalized atomic mids -> robust leave-one-out decision fits",
            fill=INK,
            font=getattr(self, "prose_bold_font", self.grid_font_bold),
            anchor="w",
        )
        canvas.create_text(
            plot_left,
            28,
            text="If the candidate's direct mid is in calibration, hold it out; fit only the remaining eligible mids from the same option side.",
            fill=MUTED,
            font=getattr(self, "prose_font", self.detail_font),
            anchor="nw",
            width=plot_right - plot_left,
            justify=tk.LEFT,
        )
        canvas.create_line(plot_left, plot_bottom, plot_right, plot_bottom, fill=FAINT)
        canvas.create_line(plot_left, plot_top, plot_left, plot_bottom, fill=FAINT)
        for value in (0.0, 0.25, 0.50, 0.75, 1.0):
            y = plot_y(value)
            canvas.create_line(plot_left, y, plot_right, y, fill=RULE, dash=(2, 3))
            canvas.create_text(plot_left - 5, y, text=format_price(value), fill=MUTED, font=self.detail_font, anchor="e")

        fitted_coordinates: list[float] = []
        for x_value, fair_value in data.fitted_points:
            fitted_coordinates.extend((plot_x(x_value), plot_y(fair_value)))
        canvas.create_line(
            *fitted_coordinates,
            fill=SUBSCRIPTION_CONFIRMED_COLOR,
            width=3,
            smooth=True,
            splinesteps=20,
        )

        role_colors = {
            "market_mid": BID_COLOR,
            "wide_market_mid": SUBSCRIPTION_CONTEXT_COLOR,
            "held_out_candidate": SELL_COLOR,
        }
        for point in data.observation_points:
            x = plot_x(point.x)
            y = plot_y(point.value)
            color = role_colors[point.role]
            radius = 4
            canvas.create_oval(
                x - radius,
                y - radius,
                x + radius,
                y + radius,
                fill=GRID_BG if point.role == "held_out_candidate" else color,
                outline=color,
                width=2 if point.role == "held_out_candidate" else 1,
            )
        legend_entries = (
            ("Same-side atomic mid / interval", BID_COLOR, False),
            ("Wide-market same-side mid (eligible)", SUBSCRIPTION_CONTEXT_COLOR, False),
            ("Candidate held out from own fit", SELL_COLOR, True),
            ("Sample LOO bounded sigmoid", SUBSCRIPTION_CONFIRMED_COLOR, False),
        )
        legend_positions = (
            (plot_left, 222),
            (plot_left + 255, 222),
            (plot_left, 242),
            (plot_left + 240, 242),
        )
        for (label, color, hollow), (legend_x, legend_y) in zip(legend_entries, legend_positions):
            canvas.create_oval(
                legend_x,
                legend_y,
                legend_x + 8,
                legend_y + 8,
                fill=GRID_BG if hollow else color,
                outline=color,
                width=2 if hollow else 1,
            )
            canvas.create_text(
                legend_x + 12,
                legend_y + 4,
                text=label,
                fill=INK_2,
                font=getattr(self, "prose_font", self.detail_font),
                anchor="w",
            )
        canvas.create_text(
            (plot_left + plot_right) / 2,
            262,
            text="far OTM  <-  moneyness relative to the underlying  ->  ITM",
            fill=MUTED,
            font=getattr(self, "prose_font", self.detail_font),
            anchor="n",
        )
        canvas.create_text(
            (plot_left + plot_right) / 2,
            281,
            text="Min Regression Points applies after holdout; insufficient support = no FV/signal.",
            fill=INK_2,
            font=getattr(self, "prose_font", self.detail_font),
            anchor="n",
        )

        panel_left = max(plot_right + 28, width * 0.64)
        panel_right = width - 24
        panel_width = panel_right - panel_left
        canvas.create_line(panel_left - 14, 8, panel_left - 14, 288, fill=RULE)
        canvas.create_text(
            panel_left,
            12,
            text="2  Wider FV = sum of atomic LOO FVs",
            fill=INK,
            font=getattr(self, "prose_bold_font", self.grid_font_bold),
            anchor="w",
        )
        box_width = min(62.0, max(48.0, (panel_width - 64) / 3))
        group_width = (box_width * len(data.atomic_values)) + (24 * (len(data.atomic_values) - 1))
        group_left = panel_left + max(0.0, (panel_width - group_width) / 2)
        for index, atomic_value in enumerate(data.atomic_values):
            x = group_left + index * (box_width + 24)
            canvas.create_rectangle(x, 48, x + box_width, 86, fill=UP_TINT, outline=SUBSCRIPTION_CONFIRMED_COLOR)
            canvas.create_text(
                x + box_width / 2, 59, text=f"Adjacent #{index + 1}", fill=INK_2,
                font=getattr(self, "prose_font", self.detail_font), anchor="c",
            )
            canvas.create_text(x + box_width / 2, 75, text=format_price(atomic_value), fill=INK, font=self.grid_font_bold, anchor="c")
            if index + 1 < len(data.atomic_values):
                canvas.create_text(x + box_width + 12, 67, text="+", fill=MUTED, font=self.grid_font_bold, anchor="c")
        canvas.create_text(
            panel_left + (panel_width / 2),
            108,
            text=f"Width {data.composed_width:g} fair value = {format_price(data.composed_fair_value)}",
            fill=INK,
            font=getattr(self, "prose_bold_font", self.grid_font_bold),
            anchor="c",
        )
        canvas.create_text(
            panel_left + (panel_width / 2),
            126,
            text=f"Configured width-{data.composed_width:g} edge = {format_price(data.composed_edge)}",
            fill=MUTED,
            font=getattr(self, "prose_font", self.detail_font),
            anchor="c",
        )
        canvas.create_text(
            panel_left + (panel_width / 2),
            143,
            text=(
                f"BUY <= {format_price(data.buy_endpoint)}   |   "
                f"SELL >= {format_price(data.sell_endpoint)}"
            ),
            fill=INK_2,
            font=getattr(self, "prose_font", self.detail_font),
            anchor="c",
        )

        canvas.create_text(
            panel_left,
            164,
            text="3  Your edge inputs by spread width",
            fill=INK,
            font=getattr(self, "prose_bold_font", self.grid_font_bold),
            anchor="w",
        )
        chart_left = panel_left + 18
        chart_right = panel_right - 12
        chart_top = 184.0
        chart_bottom = 251.0
        max_edge = max((edge for _spread_width, edge in data.edge_schedule), default=0.01)
        edge_ceiling = max(0.01, max_edge * 1.25)
        edge_coordinates: list[float] = []
        for index, (spread_width, edge) in enumerate(data.edge_schedule):
            x = chart_left + (index / max(1, len(data.edge_schedule) - 1)) * (chart_right - chart_left)
            y = chart_bottom - (edge / edge_ceiling) * (chart_bottom - chart_top)
            edge_coordinates.extend((x, y))
            canvas.create_line(x, chart_bottom, x, y, fill=RULE)
            canvas.create_text(x, chart_bottom + 11, text=f"W{spread_width:g}", fill=MUTED, font=self.detail_font, anchor="n")
            canvas.create_text(x, y - 7, text=format_price(edge), fill=MID_COLOR, font=self.detail_font, anchor="s")
        canvas.create_line(*edge_coordinates, fill=MID_COLOR, width=2)
        for x, y in zip(edge_coordinates[::2], edge_coordinates[1::2]):
            canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=MID_COLOR, outline=MID_COLOR)
        canvas.create_text(
            panel_left + (panel_width / 2),
            287,
            text=(
                f"Widths <= 1 use W1; supported fractional widths interpolate: "
                f"W2.25 = {format_price(fair_value_edge_for_width(2.25, settings))}"
            ),
            fill=INK_2,
            font=getattr(self, "prose_font", self.detail_font),
            anchor="s",
        )

    def _draw_matrix_cell_settings_guide(self, canvas: tk.Canvas) -> None:
        canvas.delete("all")
        width = max(canvas.winfo_width(), 920)
        column_gap = 12.0
        outer_padding = 10.0
        column_width = (width - (outer_padding * 2) - (column_gap * 2)) / 3
        row_height = 132.0
        grouped_items = (
            (
                "FILL / POSITION / TAB STATE",
                [
                    item
                    for item in matrix_cell_visual_legend_items()
                    if item.category in {"fill", "position", "state"}
                ],
            ),
            ("SUBSCRIPTION BORDER", [item for item in matrix_cell_visual_legend_items() if item.category == "border"]),
            (
                "CELL TEXT / LIQUIDITY",
                [
                    item
                    for item in matrix_cell_visual_legend_items()
                    if item.category in {"text", "liquidity"}
                ],
            ),
        )
        panel_bottom = max(
            449.0,
            38.0 + (max(len(items) for _title, items in grouped_items) * row_height) + 4.0,
        )
        for column, (title, items) in enumerate(grouped_items):
            column_left = outer_padding + column * (column_width + column_gap)
            canvas.create_rectangle(
                column_left,
                4,
                column_left + column_width,
                panel_bottom,
                fill=RAISED,
                outline=RULE,
            )
            canvas.create_text(
                column_left + 8,
                16,
                text=title,
                fill=MUTED,
                font=getattr(self, "header_font", self.grid_font_bold),
                anchor="w",
            )
            for row, item in enumerate(items):
                y = 38 + (row * row_height)
                sample_left = column_left + 8
                sample_right = sample_left + 92
                sample_bottom = y + 30
                sample_fill = GRID_BG if item.category == "position" else item.fill or GRID_BG
                rectangle_options: dict[str, object] = {
                    "fill": sample_fill,
                    "outline": item.outline or GRID_LINE,
                    "width": item.border_width,
                    "dash": item.dash,
                }
                if item.key == "state_stale":
                    rectangle_options["stipple"] = "gray25"
                canvas.create_rectangle(sample_left, y, sample_right, sample_bottom, **rectangle_options)
                if item.hatch and hasattr(canvas, "create_line"):
                    one_desk.draw_hatch(
                        canvas, sample_left + 1, y + 1, sample_right - 1, sample_bottom - 1, spacing=item.hatch
                    )
                if item.key == "position_saved":
                    OptionsMatrixApp._draw_matrix_position_icon(
                        self, canvas, combo_x=sample_left, y=y,
                        row_height=30, has_position=True,
                    )
                    canvas.create_text(
                        sample_left + POSITION_ICON_LANE_WIDTH + 4, y + 15,
                        text=item.sample_text, fill=INK, font=self.detail_font, anchor="w",
                    )
                elif item.category == "position":
                    sample_middle = (sample_left + sample_right) / 2.0
                    canvas.create_rectangle(
                        sample_middle, y, sample_right, sample_bottom,
                        fill=item.fill, outline=GRID_LINE,
                    )
                    canvas.create_text(
                        sample_middle + ((sample_right - sample_middle) / 2.0),
                        y + 15,
                        text=item.sample_text,
                        fill=item.text_color,
                        font=self.detail_font,
                        anchor="c",
                    )
                elif item.category == "liquidity":
                    canvas.create_text(
                        sample_left + 5,
                        y + 15,
                        text=item.sample_text,
                        fill=item.text_color,
                        font=self.detail_font,
                        anchor="w",
                    )
                    sample_states = {
                        "liquidity_quote_width": MatrixLiquidityGaugeState(
                            quote_width_ratio=0.375,
                            bid_support=0.0,
                            ask_support=0.0,
                            size_comparable=True,
                            size_peer_count=8,
                        ),
                        "liquidity_relative_size": MatrixLiquidityGaugeState(
                            quote_width_ratio=0.25,
                            bid_support=0.75,
                            ask_support=0.5,
                            size_comparable=True,
                            size_peer_count=8,
                        ),
                        "liquidity_missing_size": MatrixLiquidityGaugeState(
                            quote_width_ratio=0.25,
                            bid_support=0.75,
                            ask_support=None,
                            size_comparable=True,
                            size_peer_count=8,
                        ),
                        "liquidity_unranked": MatrixLiquidityGaugeState(
                            quote_width_ratio=0.25,
                            size_comparable=False,
                            size_peer_count=2,
                        ),
                        "liquidity_overflow": MatrixLiquidityGaugeState(
                            quote_width_ratio=1.0,
                            quote_width_overflow=True,
                            bid_support=0.0,
                            ask_support=0.0,
                            size_comparable=True,
                            size_peer_count=8,
                        ),
                        "liquidity_crossed": MatrixLiquidityGaugeState(
                            quote_crossed=True,
                            size_comparable=False,
                            size_peer_count=8,
                        ),
                    }
                    OptionsMatrixApp._draw_matrix_liquidity_gauge(
                        self,
                        canvas,
                        signal_x=sample_left,
                        signal_width=sample_right - sample_left,
                        y=y + 5,
                        row_height=20,
                        state=sample_states[item.key],
                        muted=False,
                    )
                else:
                    canvas.create_text(
                        (sample_left + sample_right) / 2,
                        y + 15,
                        text=item.sample_text,
                        fill=item.text_color,
                        font=self.detail_font,
                        anchor="c",
                    )
                text_left = sample_right + 8
                canvas.create_text(
                    text_left,
                    y,
                    text=item.label,
                    fill=INK,
                    font=getattr(self, "prose_bold_font", self.grid_font_bold),
                    anchor="nw",
                )
                canvas.create_text(
                    text_left,
                    y + 16,
                    text=item.description,
                    fill=MUTED,
                    font=getattr(self, "prose_font", self.detail_font),
                    anchor="nw",
                    width=max(80, column_left + column_width - text_left - 8),
                )
        content_bbox = canvas.bbox("all")
        content_bottom = content_bbox[3] + 8 if content_bbox is not None else panel_bottom + 4
        canvas.configure(scrollregion=(0, 0, width, max(panel_bottom + 4, content_bottom)))

    def _populate_expirations(self) -> None:
        if self.search_result is None:
            return
        display_values = self._populate_expiration_values()
        if display_values:
            self.expiration_var.set(display_values[0])
            self._load_expiration()

    def _populate_expiration_values(self) -> list[str]:
        if self.search_result is None:
            return []
        raw_expirations = self.search_result.chain.expirations
        self.expiration_display_to_raw = {
            format_expiration(expiration): expiration for expiration in raw_expirations
        }
        display_values = list(self.expiration_display_to_raw.keys())
        self.expiration_combo["values"] = display_values
        return display_values

    def _sync_controls_from_matrix_input(self, matrix_input: MatrixInputState) -> None:
        self.expiration_var.set(matrix_input.display_expiration)
        records = OptionsMatrixApp._cached_option_records(self, matrix_input.raw_expiration)
        self.option_records = list(records) if records is not None else []
        self.side_var.set(matrix_input.side)
        self._populate_strike_controls()
        self.low_strike_var.set(format_strike(matrix_input.low_strike))
        self.high_strike_var.set(format_strike(matrix_input.high_strike))
        self._stage_width_band_preferences(matrix_input)
        self._refresh_width_band_controls()
        self._refresh_staged_market_data_quote_status()

    def _sync_controls_from_stale_matrix_input(self, matrix_input: MatrixInputState) -> None:
        self.ticker_var.set(matrix_input.ticker_symbol)
        display_expiration = matrix_input.display_expiration or format_expiration(matrix_input.raw_expiration)
        if display_expiration not in self.expiration_display_to_raw:
            self.expiration_display_to_raw[display_expiration] = matrix_input.raw_expiration
            self.expiration_combo["values"] = list(self.expiration_display_to_raw.keys())
        self.expiration_var.set(display_expiration)
        self.side_var.set(matrix_input.side)
        self.low_strike_var.set(format_strike(matrix_input.low_strike))
        self.high_strike_var.set(format_strike(matrix_input.high_strike))
        self._stage_width_band_preferences(matrix_input)
        self._refresh_width_band_controls()

    def _staged_raw_expiration(self) -> str | None:
        display_expiration = self.expiration_var.get().strip()
        return self.expiration_display_to_raw.get(display_expiration)

    def _populate_strike_controls(self) -> None:
        side_records = self._records_for_selected_side()
        strikes = sorted({_round_key(record.strike) for record in side_records})
        display_values = [format_strike(strike) for strike in strikes]
        self.low_strike_combo["values"] = display_values
        self.high_strike_combo["values"] = display_values
        if not display_values:
            self.low_strike_var.set("")
            self.high_strike_var.set("")
            self._refresh_width_band_controls()
            return

        if self.low_strike_var.get() not in display_values:
            self.low_strike_var.set(display_values[0])
        if self.high_strike_var.get() not in display_values:
            default_high_index = min(len(display_values) - 1, 10)
            self.high_strike_var.set(display_values[default_high_index])
        self._refresh_width_band_controls()

    def _all_matrix_tab_quote_selections(
        self,
    ) -> list[VerticalSelection]:
        return union_matrix_tab_quote_target_selections(self.matrix_tabs)

    @staticmethod
    def _tab_cell_selection(
        tab: MatrixTabState,
        cell_key: SelectedMatrixCellKey | None,
    ) -> VerticalSelection | None:
        if cell_key is None:
            return None
        if len(cell_key) == 3:
            right, lower_strike, width = cell_key
            side_state = tab.side_states.get(right)
            if side_state is None:
                return None
            selection = side_state.matrix_plan.visible.get((lower_strike, width))
        else:
            selection = tab.matrix_plan.visible.get(cell_key)
        return selection if selection is not None and is_supported_matrix_pair(selection.pair) else None

    @staticmethod
    def _tab_cell_snapshot(
        tab: MatrixTabState,
        cell_key: SelectedMatrixCellKey | None,
    ) -> MonitoredSideSnapshot | None:
        if cell_key is None:
            return None
        if len(cell_key) == 3:
            right, lower_strike, width = cell_key
            side_state = tab.side_states.get(right)
            if side_state is None:
                return None
            snapshot = side_state.matrix_snapshots.get((lower_strike, width))
        else:
            snapshot = tab.matrix_snapshots.get(cell_key)
        return snapshot if snapshot is not None and is_supported_matrix_pair(snapshot.selection.pair) else None

    def _ensure_tab_selected_cell(self, tab: MatrixTabState) -> None:
        if OptionsMatrixApp._tab_cell_selection(tab, tab.selected_cell_key) is not None:
            return
        if tab.ranked_signals:
            item = tab.ranked_signals[0]
            tab.selected_cell_key = (
                (item.selection.pair.right, item.lower_strike, item.width)
                if tab.side_states
                else (item.lower_strike, item.width)
            )
            return
        if tab.side_states:
            for right in (RIGHT_PUT, RIGHT_CALL):
                side_state = tab.side_states.get(right)
                if side_state is not None and side_state.matrix_plan.visible:
                    lower_strike, width = next(iter(side_state.matrix_plan.visible))
                    tab.selected_cell_key = (right, lower_strike, width)
                    return
        if tab.matrix_plan.visible:
            tab.selected_cell_key = next(iter(tab.matrix_plan.visible.keys()))
            return
        tab.selected_cell_key = None

    def _redraw_all(self) -> None:
        self._redraw_matrix()
        self._redraw_rank_table()
        self._redraw_selected_detail()
        self._refresh_recorder_controls()

    def _schedule_matrix_layout_redraw(self, _event: object | None = None) -> None:
        pending = getattr(self, "_matrix_layout_after_id", None)
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass
        self._matrix_layout_after_id = self.root.after(50, self._perform_matrix_layout_redraw)

    def _perform_matrix_layout_redraw(self) -> None:
        self._matrix_layout_after_id = None
        self._matrix_render_signature = None
        self._redraw_matrix()

    def _matrix_render_state_signature(self) -> tuple[object, ...]:
        tab = self._active_tab()
        canvas = self.matrix_canvas
        tab_input = getattr(tab, "matrix_input", None)
        return (
            id(canvas),
            getattr(tab, "tab_id", None),
            bool(getattr(tab, "is_stale", False)),
            normalize_matrix_view(tab_input.side) if tab_input is not None else None,
            tuple(self.row_strikes),
            tuple(self._applied_widths()),
            self._matrix_layout_slot_widths(),
            int(max(canvas.winfo_width(), 1)),
        )

    @staticmethod
    def _cell_fill(
        selection: VerticalSelection | None,
        *,
        selected: bool,
        is_itm: bool,
    ) -> str:
        return resolve_matrix_cell_fills(
            selection,
            selected=selected,
            is_itm=is_itm,
        )[0]


    @staticmethod
    def _tab_cell_plan(
        tab: MatrixTabState,
        cell_key: SelectedMatrixCellKey,
    ) -> MatrixSelectionPlan:
        if len(cell_key) == 3:
            right = cell_key[0]
            side_state = tab.side_states.get(right)
            if side_state is not None:
                return side_state.matrix_plan
        return tab.matrix_plan

    def _active_subscription_styles(
        self,
        tab: MatrixTabState,
    ) -> dict[SelectedMatrixCellKey, MatrixCellSubscriptionStyle]:
        diagnostics = getattr(self, "_subscription_diagnostics_by_con_id", None)
        if diagnostics is None:
            return {}
        failure_counts = getattr(self, "_subscription_failure_counts", {})
        underlying_states = [
            str(getattr(diagnostic, "state", "unknown"))
            for diagnostic in diagnostics.values()
            if getattr(diagnostic, "quote_kind", None) == QUOTE_KIND_UNDERLYING
        ]
        if not underlying_states:
            underlying_states = ["unknown"]

        plan_contract_ids: dict[int, frozenset[int]] = {}
        styles: dict[SelectedMatrixCellKey, MatrixCellSubscriptionStyle] = {}
        for cell_key in getattr(self, "_matrix_cell_items", {}):
            selection = OptionsMatrixApp._tab_cell_selection(tab, cell_key)
            if selection is None:
                styles[cell_key] = MatrixCellSubscriptionStyle()
                continue
            plan = OptionsMatrixApp._tab_cell_plan(tab, cell_key)
            plan_key = id(plan)
            all_contract_ids = plan_contract_ids.get(plan_key)
            if all_contract_ids is None:
                all_selections = [*flatten_matrix_context(plan), *plan.visible.values()]
                all_contract_ids = frozenset(
                    leg.contract.con_id
                    for context_selection in all_selections
                    for leg in context_selection.legs
                )
                plan_contract_ids[plan_key] = all_contract_ids
            direct_ids = frozenset(leg.contract.con_id for leg in selection.legs)

            def state_for(con_id: int) -> str:
                diagnostic = diagnostics.get(con_id)
                return str(getattr(diagnostic, "state", "unknown"))

            direct_states = [state_for(con_id) for con_id in direct_ids]
            context_states = [
                state_for(con_id)
                for con_id in all_contract_ids
                if con_id not in direct_ids
            ]
            context_states.extend(underlying_states)
            recurrent_failures = max(
                (failure_counts.get(con_id, 0) for con_id in direct_ids),
                default=0,
            )
            styles[cell_key] = resolve_matrix_cell_subscription_style(
                direct_states,
                context_states,
                recurrent_failures=recurrent_failures,
            )
        return styles

    def _apply_active_subscription_styles(self, tab: MatrixTabState) -> None:
        canvas = self.matrix_canvas
        if not hasattr(canvas, "itemconfigure"):
            return
        styles = OptionsMatrixApp._active_subscription_styles(self, tab)
        can_hatch = all(
            hasattr(canvas, method) for method in ("delete", "coords", "create_line", "tag_raise")
        )
        try:
            if can_hatch:
                canvas.delete(SUBSCRIPTION_HATCH_TAG)
            for cell_key, items in getattr(self, "_matrix_cell_items", {}).items():
                style = styles.get(cell_key, MatrixCellSubscriptionStyle())
                for rectangle_id in items.rectangle_ids:
                    canvas.itemconfigure(
                        rectangle_id,
                        outline=style.outline,
                        width=style.width,
                        dash=style.dash,
                    )
                    if can_hatch and style.hatch:
                        x0, y0, x1, y1 = canvas.coords(rectangle_id)
                        for line_id in one_desk.draw_hatch(
                            canvas, x0 + 1, y0 + 1, x1 - 1, y1 - 1,
                            spacing=style.hatch, tags=(SUBSCRIPTION_HATCH_TAG,),
                        ):
                            canvas.tag_raise(line_id, rectangle_id)  # under the cell text
        except tk.TclError:
            self._matrix_render_signature = None

    def _draw_matrix_liquidity_gauge(
        self,
        canvas: tk.Canvas,
        *,
        signal_x: float,
        signal_width: float,
        y: float,
        row_height: float,
        state: MatrixLiquidityGaugeState | None,
        muted: bool,
    ) -> MatrixLiquidityGaugeCanvasItems | None:
        if not all(
            hasattr(canvas, method)
            for method in ("create_line", "create_rectangle", "coords", "itemconfigure")
        ):
            return None
        gauge_width = min(
            LIQUIDITY_GAUGE_MAX_WIDTH,
            max(LIQUIDITY_GAUGE_MIN_WIDTH, signal_width * 0.36),
        )
        right = signal_x + signal_width - 4.0
        left = right - gauge_width
        quote_y = round(y + max(4.0, row_height * 0.28))
        size_top = round(y + max(10.0, row_height * 0.58))
        size_bottom = round(min(y + row_height - 3.0, size_top + 4.0))
        center = (left + right) / 2.0
        size_mid = (size_top + size_bottom) / 2.0

        raw_ids = (
            canvas.create_line(center, quote_y, center, quote_y),
            canvas.create_line(center, quote_y, center, quote_y),
            canvas.create_line(left, size_mid, right, size_mid),
            canvas.create_rectangle(center, size_top, center, size_bottom),
            canvas.create_rectangle(center, size_top, center, size_bottom),
            canvas.create_line(center, size_top - 1.0, center, size_bottom + 1.0),
        )
        if not all(isinstance(item_id, int) for item_id in raw_ids):
            return None
        items = MatrixLiquidityGaugeCanvasItems(
            quote_id=int(raw_ids[0]),
            auxiliary_id=int(raw_ids[1]),
            size_track_id=int(raw_ids[2]),
            bid_id=int(raw_ids[3]),
            ask_id=int(raw_ids[4]),
            center_id=int(raw_ids[5]),
            left=left,
            right=right,
            quote_y=quote_y,
            size_top=size_top,
            size_bottom=size_bottom,
        )
        OptionsMatrixApp._update_matrix_liquidity_gauge(
            self,
            canvas,
            items,
            state,
            muted=muted,
        )
        return items

    def _update_matrix_liquidity_gauge(
        self,
        canvas: tk.Canvas,
        items: MatrixLiquidityGaugeCanvasItems,
        state: MatrixLiquidityGaugeState | None,
        *,
        muted: bool,
    ) -> None:
        item_ids = (
            items.quote_id,
            items.auxiliary_id,
            items.size_track_id,
            items.bid_id,
            items.ask_id,
            items.center_id,
        )
        if state is None:
            for item_id in item_ids:
                canvas.itemconfigure(item_id, state="hidden")
            return
        for item_id in item_ids:
            canvas.itemconfigure(item_id, state="normal")

        left = items.left
        right = items.right
        center = (left + right) / 2.0
        quote_y = items.quote_y
        width_color = ITM_TEXT if muted else LIQUIDITY_WIDTH_COLOR
        track_color = ITM_TEXT if muted else LIQUIDITY_TRACK_COLOR
        bid_color = ITM_TEXT if muted else BID_COLOR
        ask_color = ITM_TEXT if muted else ASK_COLOR

        if state.quote_crossed:
            warning_color = ITM_TEXT if muted else SUBSCRIPTION_FAILED_COLOR
            canvas.coords(
                items.quote_id,
                center - 4.0,
                quote_y - 3.0,
                center + 4.0,
                quote_y + 3.0,
            )
            canvas.itemconfigure(items.quote_id, fill=warning_color, width=2, dash=())
            canvas.coords(
                items.auxiliary_id,
                center - 4.0,
                quote_y + 3.0,
                center + 4.0,
                quote_y - 3.0,
            )
            canvas.itemconfigure(items.auxiliary_id, fill=warning_color, width=2, dash=())
            for item_id in (
                items.size_track_id,
                items.bid_id,
                items.ask_id,
                items.center_id,
            ):
                canvas.itemconfigure(item_id, state="hidden")
            return
        elif state.quote_width_ratio is None:
            canvas.coords(items.quote_id, left + 2.0, quote_y, right - 2.0, quote_y)
            canvas.itemconfigure(items.quote_id, fill=track_color, width=1, dash=(2, 2))
            canvas.itemconfigure(items.auxiliary_id, state="hidden")
        else:
            span = max(2.0, (right - left) * min(max(state.quote_width_ratio, 0.0), 1.0))
            quote_left = center - (span / 2.0)
            quote_right = center + (span / 2.0)
            canvas.coords(
                items.quote_id,
                quote_left,
                quote_y - 2.0,
                quote_left,
                quote_y + 2.0,
                quote_left,
                quote_y,
                quote_right,
                quote_y,
                quote_right,
                quote_y - 2.0,
                quote_right,
                quote_y + 2.0,
            )
            canvas.itemconfigure(items.quote_id, fill=width_color, width=1, dash=())
            if state.quote_width_overflow:
                canvas.coords(
                    items.auxiliary_id,
                    right - 4.0,
                    quote_y - 3.0,
                    right,
                    quote_y,
                    right - 4.0,
                    quote_y + 3.0,
                )
                canvas.itemconfigure(
                    items.auxiliary_id,
                    state="normal",
                    fill=ITM_TEXT if muted else LIQUIDITY_OVERFLOW_COLOR,
                    width=2,
                    dash=(),
                )
            else:
                canvas.itemconfigure(items.auxiliary_id, state="hidden")

        size_mid = (items.size_top + items.size_bottom) / 2.0
        canvas.coords(items.size_track_id, left, size_mid, right, size_mid)
        canvas.itemconfigure(
            items.size_track_id,
            fill=track_color,
            width=1,
            dash=() if state.size_comparable else (2, 2),
        )
        canvas.coords(
            items.center_id,
            center,
            items.size_top - 1.0,
            center,
            items.size_bottom + 1.0,
        )
        canvas.itemconfigure(items.center_id, fill=track_color, width=1, dash=())

        if not state.size_comparable:
            canvas.itemconfigure(items.bid_id, state="hidden")
            canvas.itemconfigure(items.ask_id, state="hidden")
            return

        half_width = max(1.0, ((right - left) / 2.0) - 1.0)

        def configure_size(
            item_id: int,
            support: float | None,
            *,
            is_bid: bool,
            color: str,
        ) -> None:
            if support is None:
                side_left, side_right = (
                    (left, center - 1.0) if is_bid else (center + 1.0, right)
                )
                canvas.coords(
                    item_id,
                    side_left,
                    items.size_top,
                    side_right,
                    items.size_bottom,
                )
                canvas.itemconfigure(
                    item_id,
                    state="normal",
                    fill="",
                    outline=track_color,
                    width=1,
                    dash=(2, 1),
                )
                return
            bar_length = half_width * min(max(support, 0.0), 1.0)
            side_left, side_right = (
                (center - bar_length, center) if is_bid else (center, center + bar_length)
            )
            canvas.coords(
                item_id,
                side_left,
                items.size_top,
                side_right,
                items.size_bottom,
            )
            canvas.itemconfigure(
                item_id,
                state="normal" if bar_length > 0.0 else "hidden",
                fill=color,
                outline=color,
                width=1,
                dash=(),
            )

        configure_size(items.bid_id, state.bid_support, is_bid=True, color=bid_color)
        configure_size(items.ask_id, state.ask_support, is_bid=False, color=ask_color)

    def _draw_matrix_position_icon(
        self, canvas: tk.Canvas, *, combo_x: float, y: float,
        row_height: float, has_position: bool,
    ) -> int:
        """A passive saved-position marker; clicking still selects the cell."""
        return canvas.create_text(
            combo_x + (POSITION_ICON_LANE_WIDTH / 2) + 2,
            y + (row_height / 2),
            text=POSITION_ICON_TEXT if has_position else "",
            fill=POSITION_ICON_COLOR,
            font=self.grid_font,
            anchor="c",
            tags=("position_indicator",),
        )

    def _remember_matrix_cell_items(
        self,
        key: SelectedMatrixCellKey,
        rectangle_ids: tuple[object, object],
        combo_text_id: object,
        signal_text_id: object,
        liquidity_gauge: MatrixLiquidityGaugeCanvasItems | None = None,
        *,
        compact_signal: bool = False,
        position_icon_id: object = None,
    ) -> None:
        if not (
            all(isinstance(item_id, int) for item_id in rectangle_ids)
            and isinstance(combo_text_id, int)
            and isinstance(signal_text_id, int)
        ):
            return
        cell_items = getattr(self, "_matrix_cell_items", None)
        if cell_items is None:
            cell_items = {}
            self._matrix_cell_items = cell_items
        cell_items[key] = MatrixCanvasCellItems(
            rectangle_ids=(int(rectangle_ids[0]), int(rectangle_ids[1])),
            combo_text_id=int(combo_text_id),
            signal_text_id=int(signal_text_id),
            liquidity_gauge=liquidity_gauge,
            compact_signal=compact_signal,
            position_icon_id=position_icon_id if isinstance(position_icon_id, int) else None,
        )

    def _refresh_active_matrix_render(self) -> None:
        canvas = self.matrix_canvas
        signature = self._matrix_render_state_signature()
        if (
            signature != getattr(self, "_matrix_render_signature", None)
            or not getattr(self, "_matrix_cell_items", None)
            or not hasattr(canvas, "itemconfigure")
        ):
            self._redraw_matrix()
            return

        tab = self._active_tab()
        if tab is None or tab.is_stale:
            self._redraw_matrix()
            return
        liquidity_states = matrix_liquidity_gauge_states(tab)
        previous_liquidity = getattr(self, "_matrix_liquidity_render_state", {})
        current_liquidity: dict[
            SelectedMatrixCellKey,
            tuple[MatrixLiquidityGaugeState | None, bool],
        ] = {}
        try:
            for cell_key, items in self._matrix_cell_items.items():
                selection = OptionsMatrixApp._tab_cell_selection(tab, cell_key)
                snapshot = OptionsMatrixApp._tab_cell_snapshot(tab, cell_key)
                selected = cell_key == self.selected_cell_key
                is_itm = selection is not None and self._selection_is_itm(selection)
                combo_fill, signal_fill = resolve_matrix_cell_fills(
                    selection,
                    selected=selected,
                    is_itm=is_itm,
                    live=not getattr(tab, "is_stale", False),
                    exit_signal=getattr(position_exit_for_selection(self, selection), "signal", None),
                )
                canvas.itemconfigure(items.rectangle_ids[0], fill=combo_fill)
                canvas.itemconfigure(items.rectangle_ids[1], fill=signal_fill)
                if selection is None:
                    combo_text = ""
                    combo_color = INK
                    signal_text = ""
                    signal_color = INK
                else:
                    combo_text = pair_key_for_pair(selection.pair)
                    combo_color = ITM_TEXT if is_itm else INK
                    signal_text, signal_color = self._cell_signal_text(snapshot, muted=is_itm)
                    if bool(getattr(items, "compact_signal", False)):
                        signal_text = compact_matrix_signal_text(signal_text)
                canvas.itemconfigure(items.combo_text_id, text=combo_text, fill=combo_color)
                canvas.itemconfigure(items.signal_text_id, text=signal_text, fill=signal_color)
                position_icon_id = getattr(items, "position_icon_id", None)
                if position_icon_id is not None:
                    canvas.itemconfigure(
                        position_icon_id,
                        text=POSITION_ICON_TEXT if position_record_for_selection(self, selection) is not None else "",
                    )
                gauge_state = (
                    liquidity_states.get(cell_key)
                    if snapshot is not None and snapshot.synthetic_quote is not None
                    else None
                )
                gauge_signature = (gauge_state, is_itm)
                current_liquidity[cell_key] = gauge_signature
                gauge_items = getattr(items, "liquidity_gauge", None)
                if gauge_items is not None and previous_liquidity.get(cell_key) != gauge_signature:
                    OptionsMatrixApp._update_matrix_liquidity_gauge(
                        self,
                        canvas,
                        gauge_items,
                        gauge_state,
                        muted=is_itm,
                    )
            self._matrix_liquidity_render_state = current_liquidity
            OptionsMatrixApp._apply_active_subscription_styles(self, tab)
        except tk.TclError:
            self._redraw_matrix()

    def _redraw_matrix(self) -> None:
        canvas = self.matrix_canvas
        canvas.delete("all")
        cell_items = getattr(self, "_matrix_cell_items", None)
        if cell_items is None:
            self._matrix_cell_items = {}
        else:
            cell_items.clear()
        self._matrix_liquidity_render_state = {}
        self._matrix_render_signature = OptionsMatrixApp._matrix_render_state_signature(self)
        self.matrix_hitboxes.clear()
        self.matrix_reload_hitbox = None

        active_tab = self._active_tab()
        if (
            active_tab is not None
            and getattr(active_tab, "matrix_input", None) is not None
            and normalize_matrix_view(active_tab.matrix_input.side) == MATRIX_VIEW_BOTH
        ):
            self._redraw_split_matrix(canvas, active_tab)
            if not active_tab.is_stale:
                OptionsMatrixApp._apply_active_subscription_styles(self, active_tab)
            return

        widths = self._applied_widths()
        canvas_width = max(canvas.winfo_width(), 1)
        layout = matrix_grid_layout(
            canvas_width,
            widths,
            slot_widths=self._matrix_layout_slot_widths(),
        )
        strike_width = layout.strike_width
        group_width = layout.group_width
        combo_width = layout.combo_width
        signal_width = layout.signal_width
        is_stale = active_tab is not None and active_tab.is_stale
        compact_signal = signal_width < LIQUIDITY_COMPACT_SIGNAL_THRESHOLD
        liquidity_states = (
            matrix_liquidity_gauge_states(active_tab)
            if (
                active_tab is not None
                and not is_stale
                and getattr(active_tab, "matrix_input", None) is not None
            )
            else {}
        )
        liquidity_render_state: dict[
            SelectedMatrixCellKey,
            tuple[MatrixLiquidityGaugeState | None, bool],
        ] = {}
        header_one = 24
        header_two = 24
        row_height = 20
        header_height = header_one + header_two
        total_width = layout.total_width
        total_height = header_height + (max(1, len(self.row_strikes)) * row_height)
        if hasattr(canvas, "configure"):
            canvas.configure(scrollregion=(0, 0, total_width, total_height))

        canvas.create_rectangle(0, 0, total_width, total_height, fill=GRID_BG, outline=GRID_LINE)

        canvas.create_rectangle(0, 0, strike_width, header_height, fill=HEADER_BG, outline=GRID_LINE, width=2)
        canvas.create_text(
            strike_width / 2,
            header_height / 2,
            text="STRIKE",
            font=getattr(self, "header_font", self.grid_font),
            fill=MUTED,
            anchor="c",
        )

        for slot in layout.slots:
            if not slot.selected:
                continue
            width = slot.width
            x = slot.x
            canvas.create_rectangle(x, 0, x + group_width, header_one, fill=HEADER_BG, outline=GRID_LINE, width=2)
            canvas.create_text(
                x + (group_width / 2),
                header_one / 2,
                text=f"WIDTH {width:.2f}",
                font=self.grid_font,
                fill=INK,
                anchor="c",
            )
            canvas.create_rectangle(x, header_one, x + combo_width, header_height, fill=GRID_BG, outline=GRID_LINE)
            canvas.create_rectangle(
                x + combo_width,
                header_one,
                x + group_width,
                header_height,
                fill=GRID_BG,
                outline=GRID_LINE,
            )
            canvas.create_text(
                x + (combo_width / 2),
                header_one + (header_two / 2),
                text="COMBO",
                font=getattr(self, "header_font", self.grid_font),
                fill=MUTED,
                anchor="c",
            )
            canvas.create_text(
                x + combo_width + (signal_width / 2),
                header_one + (header_two / 2),
                text="SIGNAL/PRICE",
                font=getattr(self, "header_font", self.grid_font),
                fill=MUTED,
                anchor="c",
            )

        rows = self.row_strikes if self.row_strikes else [0.0]
        for row_index, strike in enumerate(rows):
            y = header_height + (row_index * row_height)
            canvas.create_rectangle(0, y, strike_width, y + row_height, fill=GRID_BG, outline=GRID_LINE)
            strike_text = "" if not self.row_strikes else format_strike(strike)
            canvas.create_text(
                strike_width / 2, y + row_height / 2, text=strike_text, font=self.grid_font, fill=INK, anchor="c"
            )

            for slot in layout.slots:
                if not slot.selected:
                    continue
                width = slot.width
                x = slot.x
                cell_key = (_round_key(strike), _round_key(width))
                selection = self.matrix_plan.visible.get(cell_key) if self.matrix_plan is not None else None
                snapshot = self.matrix_snapshots.get(cell_key)
                if selection is not None and not is_supported_matrix_pair(selection.pair):
                    selection = snapshot = None
                is_itm = selection is not None and self._selection_is_itm(selection)
                combo_fill, signal_fill = resolve_matrix_cell_fills(
                    selection,
                    selected=cell_key == self.selected_cell_key,
                    is_itm=is_itm,
                    live=not is_stale,
                    exit_signal=getattr(position_exit_for_selection(self, selection), "signal", None),
                )

                combo_rectangle_id = canvas.create_rectangle(
                    x,
                    y,
                    x + combo_width,
                    y + row_height,
                    fill=combo_fill,
                    outline=GRID_LINE,
                )
                signal_rectangle_id = canvas.create_rectangle(
                    x + combo_width,
                    y,
                    x + group_width,
                    y + row_height,
                    fill=signal_fill,
                    outline=GRID_LINE,
                )
                combo = ""
                combo_color = INK
                signal_text = ""
                signal_color = INK
                if selection is not None and not is_stale:
                    combo = pair_key_for_pair(selection.pair)
                    signal_text, signal_color = self._cell_signal_text(snapshot, muted=is_itm)
                    if compact_signal:
                        signal_text = compact_matrix_signal_text(signal_text)
                    combo_color = ITM_TEXT if is_itm else INK
                    self.matrix_hitboxes.append((x, y, x + group_width, y + row_height, cell_key))
                position_icon_id = OptionsMatrixApp._draw_matrix_position_icon(
                    self, canvas, combo_x=x, y=y, row_height=row_height,
                    has_position=position_record_for_selection(self, selection) is not None,
                )
                combo_text_id = canvas.create_text(
                    x + POSITION_ICON_LANE_WIDTH + 4,
                    y + row_height / 2,
                    text=combo,
                    fill=combo_color,
                    font=self.grid_font,
                    anchor="w",
                )
                signal_text_id = canvas.create_text(
                    x + combo_width + 4,
                    y + row_height / 2,
                    text=signal_text,
                    fill=signal_color,
                    font=self.grid_font,
                    anchor="w",
                )
                gauge_state = (
                    liquidity_states.get(cell_key)
                    if snapshot is not None and snapshot.synthetic_quote is not None
                    else None
                )
                liquidity_gauge = (
                    OptionsMatrixApp._draw_matrix_liquidity_gauge(
                        self,
                        canvas,
                        signal_x=x + combo_width,
                        signal_width=signal_width,
                        y=y,
                        row_height=row_height,
                        state=gauge_state,
                        muted=is_itm,
                    )
                    if selection is not None and not is_stale
                    else None
                )
                if selection is not None and not is_stale:
                    liquidity_render_state[cell_key] = (gauge_state, is_itm)
                OptionsMatrixApp._remember_matrix_cell_items(
                    self,
                    cell_key,
                    (combo_rectangle_id, signal_rectangle_id),
                    combo_text_id,
                    signal_text_id,
                    liquidity_gauge,
                    compact_signal=compact_signal,
                    position_icon_id=position_icon_id,
                )

        self._matrix_liquidity_render_state = liquidity_render_state

        if active_tab is not None and not is_stale:
            OptionsMatrixApp._apply_active_subscription_styles(self, active_tab)

        if is_stale:
            self._draw_stale_matrix_overlay(canvas, total_width, total_height)
        elif not self.row_strikes:
            canvas.create_text(
                max(240, total_width / 2),
                header_height + 28,
                text="Load a ticker, expiration, strike range, and matrix.",
                fill=MUTED,
                font=self.grid_font,
                anchor="c",
            )

    def _redraw_split_matrix(self, canvas: tk.Canvas, tab: MatrixTabState) -> None:
        widths = self._applied_widths()
        layout = split_matrix_grid_layout(
            max(canvas.winfo_width(), 1),
            widths,
            slot_widths=self._matrix_layout_slot_widths(),
        )
        side_header_height = 24
        width_header_height = 24
        field_header_height = 24
        header_height = side_header_height + width_header_height + field_header_height
        row_height = 20
        compact_signal = layout.signal_width < LIQUIDITY_COMPACT_SIGNAL_THRESHOLD
        liquidity_states = matrix_liquidity_gauge_states(tab) if not tab.is_stale else {}
        liquidity_render_state: dict[
            SelectedMatrixCellKey,
            tuple[MatrixLiquidityGaugeState | None, bool],
        ] = {}
        rows = self.row_strikes if self.row_strikes else [0.0]
        total_height = header_height + (max(1, len(self.row_strikes)) * row_height)
        total_width = layout.total_width
        canvas.configure(scrollregion=(0, 0, total_width, total_height))
        canvas.create_rectangle(0, 0, total_width, total_height, fill=GRID_BG, outline=GRID_LINE)

        left_panel_right = layout.strike_x
        right_panel_left = layout.strike_x + layout.strike_width
        canvas.create_rectangle(
            0,
            0,
            left_panel_right,
            side_header_height,
            fill=HEADER_BG,
            outline=GRID_LINE,
            width=2,
        )
        canvas.create_text(
            left_panel_right / 2,
            side_header_height / 2,
            text="CALLS",
            font=getattr(self, "header_font", self.grid_font_bold),
            fill=INK,
            anchor="c",
        )
        canvas.create_rectangle(
            right_panel_left,
            0,
            total_width,
            side_header_height,
            fill=HEADER_BG,
            outline=GRID_LINE,
            width=2,
        )
        canvas.create_text(
            right_panel_left + ((total_width - right_panel_left) / 2),
            side_header_height / 2,
            text="PUTS",
            font=getattr(self, "header_font", self.grid_font_bold),
            fill=INK,
            anchor="c",
        )
        canvas.create_rectangle(
            layout.strike_x,
            0,
            right_panel_left,
            header_height,
            fill=HEADER_BG,
            outline=GRID_LINE,
            width=2,
        )
        canvas.create_text(
            layout.strike_x + (layout.strike_width / 2),
            header_height / 2,
            text="STRIKE",
            font=getattr(self, "header_font", self.grid_font_bold),
            fill=MUTED,
            anchor="c",
        )

        def draw_width_header(right: str, slot: MatrixWidthSlot) -> None:
            if not slot.selected:
                return
            x = slot.x
            y_one = side_header_height
            y_two = side_header_height + width_header_height
            combo_x = x if right == RIGHT_PUT else x + layout.signal_width
            signal_x = x + layout.combo_width if right == RIGHT_PUT else x
            canvas.create_rectangle(
                x,
                y_one,
                x + layout.group_width,
                y_two,
                fill=HEADER_BG,
                outline=GRID_LINE,
                width=2,
            )
            canvas.create_text(
                x + (layout.group_width / 2),
                y_one + (width_header_height / 2),
                text=f"WIDTH {slot.width:.2f}",
                font=self.grid_font,
                fill=INK,
                anchor="c",
            )
            canvas.create_rectangle(
                combo_x,
                y_two,
                combo_x + layout.combo_width,
                header_height,
                fill=GRID_BG,
                outline=GRID_LINE,
            )
            canvas.create_rectangle(
                signal_x,
                y_two,
                signal_x + layout.signal_width,
                header_height,
                fill=GRID_BG,
                outline=GRID_LINE,
            )
            canvas.create_text(
                combo_x + (layout.combo_width / 2),
                y_two + (field_header_height / 2),
                text="COMBO",
                font=getattr(self, "header_font", self.grid_font),
                fill=MUTED,
                anchor="c",
            )
            canvas.create_text(
                signal_x + (layout.signal_width / 2),
                y_two + (field_header_height / 2),
                text="SIGNAL/PRICE",
                font=getattr(self, "header_font", self.grid_font),
                fill=MUTED,
                anchor="c",
            )

        for slot in layout.call_slots:
            draw_width_header(RIGHT_CALL, slot)
        for slot in layout.put_slots:
            draw_width_header(RIGHT_PUT, slot)

        side_slots = (
            (RIGHT_CALL, layout.call_slots),
            (RIGHT_PUT, layout.put_slots),
        )
        for row_index, strike in enumerate(rows):
            y = header_height + (row_index * row_height)
            canvas.create_rectangle(
                layout.strike_x,
                y,
                right_panel_left,
                y + row_height,
                fill=GRID_BG,
                outline=GRID_LINE,
            )
            strike_text = "" if not self.row_strikes else format_strike(strike)
            canvas.create_text(
                layout.strike_x + (layout.strike_width / 2),
                y + (row_height / 2),
                text=strike_text,
                font=self.grid_font,
                fill=INK,
                anchor="c",
            )
            for right, slots in side_slots:
                side_state = tab.side_states.get(right)
                for slot in slots:
                    if not slot.selected:
                        continue
                    local_key = (_round_key(strike), _round_key(slot.width))
                    view_key: MatrixViewCellKey = (right, local_key[0], local_key[1])
                    selection = (
                        side_state.matrix_plan.visible.get(local_key)
                        if side_state is not None
                        else None
                    )
                    snapshot = (
                        side_state.matrix_snapshots.get(local_key)
                        if side_state is not None
                        else None
                    )
                    if selection is not None and not is_supported_matrix_pair(selection.pair):
                        selection = snapshot = None
                    selected = view_key == self.selected_cell_key
                    is_itm = selection is not None and self._selection_is_itm(selection)
                    combo_fill, signal_fill = resolve_matrix_cell_fills(
                        selection,
                        selected=selected,
                        is_itm=is_itm,
                        live=not tab.is_stale,
                        exit_signal=getattr(position_exit_for_selection(self, selection), "signal", None),
                    )
                    x = slot.x
                    combo_x = x if right == RIGHT_PUT else x + layout.signal_width
                    signal_x = x + layout.combo_width if right == RIGHT_PUT else x
                    combo_rectangle_id = canvas.create_rectangle(
                        combo_x,
                        y,
                        combo_x + layout.combo_width,
                        y + row_height,
                        fill=combo_fill,
                        outline=GRID_LINE,
                    )
                    signal_rectangle_id = canvas.create_rectangle(
                        signal_x,
                        y,
                        signal_x + layout.signal_width,
                        y + row_height,
                        fill=signal_fill,
                        outline=GRID_LINE,
                    )
                    combo = ""
                    combo_color = INK
                    signal_text = ""
                    signal_color = INK
                    if selection is not None and not tab.is_stale:
                        combo = pair_key_for_pair(selection.pair)
                        combo_color = ITM_TEXT if is_itm else INK
                        signal_text, signal_color = self._cell_signal_text(snapshot, muted=is_itm)
                        if compact_signal:
                            signal_text = compact_matrix_signal_text(signal_text)
                        self.matrix_hitboxes.append(
                            (x, y, x + layout.group_width, y + row_height, view_key)
                        )
                    position_icon_id = OptionsMatrixApp._draw_matrix_position_icon(
                        self, canvas, combo_x=combo_x, y=y, row_height=row_height,
                        has_position=position_record_for_selection(self, selection) is not None,
                    )
                    combo_text_id = canvas.create_text(
                        combo_x + POSITION_ICON_LANE_WIDTH + 4,
                        y + (row_height / 2),
                        text=combo,
                        fill=combo_color,
                        font=self.grid_font,
                        anchor="w",
                    )
                    signal_text_id = canvas.create_text(
                        signal_x + 4,
                        y + (row_height / 2),
                        text=signal_text,
                        fill=signal_color,
                        font=self.grid_font,
                        anchor="w",
                    )
                    gauge_state = (
                        liquidity_states.get(view_key)
                        if snapshot is not None and snapshot.synthetic_quote is not None
                        else None
                    )
                    liquidity_gauge = (
                        OptionsMatrixApp._draw_matrix_liquidity_gauge(
                            self,
                            canvas,
                            signal_x=signal_x,
                            signal_width=layout.signal_width,
                            y=y,
                            row_height=row_height,
                            state=gauge_state,
                            muted=is_itm,
                        )
                        if selection is not None and not tab.is_stale
                        else None
                    )
                    if selection is not None and not tab.is_stale:
                        liquidity_render_state[view_key] = (gauge_state, is_itm)
                    OptionsMatrixApp._remember_matrix_cell_items(
                        self,
                        view_key,
                        (combo_rectangle_id, signal_rectangle_id),
                        combo_text_id,
                        signal_text_id,
                        liquidity_gauge,
                        compact_signal=compact_signal,
                        position_icon_id=position_icon_id,
                    )

        self._matrix_liquidity_render_state = liquidity_render_state

        if tab.is_stale:
            self._draw_stale_matrix_overlay(canvas, total_width, total_height)
        elif not self.row_strikes:
            canvas.create_text(
                total_width / 2,
                header_height + 28,
                text="Load a ticker, expiration, strike range, and matrix.",
                fill=MUTED,
                font=self.grid_font,
                anchor="c",
            )

    def _draw_stale_matrix_overlay(self, canvas: tk.Canvas, total_width: float, total_height: float) -> None:
        visible_width = max(canvas.winfo_width(), total_width, 360)
        visible_height = max(canvas.winfo_height(), total_height, 180)
        canvas.create_rectangle(
            0,
            0,
            visible_width,
            visible_height,
            fill=SUNKEN,
            outline="",
            stipple="gray25",
        )
        center_x = visible_width / 2
        center_y = max(90, visible_height / 2)
        canvas.create_text(
            center_x,
            center_y - 34,
            text="Cached matrix needs live data",
            fill=INK_2,
            font=self.grid_font_bold,
            anchor="c",
        )
        button_width = 150
        button_height = 34
        x1 = center_x - (button_width / 2)
        y1 = center_y - (button_height / 2)
        x2 = center_x + (button_width / 2)
        y2 = center_y + (button_height / 2)
        canvas.create_rectangle(x1, y1, x2, y2, fill=SURFACE, outline=INK_2, width=2)
        canvas.create_text(center_x, center_y, text="Reload matrix", fill=INK, font=self.grid_font_bold)
        self.matrix_reload_hitbox = (x1, y1, x2, y2)

    def _selection_is_itm(self, selection: VerticalSelection) -> bool:
        active_tab = self._active_tab()
        return selection_has_itm_strike(
            selection,
            matrix_tab_underlying_reference_price(active_tab, self.underlying_quote),
        )

    def _cell_signal_text(
        self,
        snapshot: MonitoredSideSnapshot | None,
        *,
        muted: bool = False,
    ) -> tuple[str, str]:
        if snapshot is None:
            return "...", MUTED
        position_exit = position_exit_for_selection(self, snapshot.selection)
        if position_exit is not None and position_exit.signal is not None:
            return (
                f"EXIT {position_exit.signal} {position_price(position_exit.target_price)}",
                BUY_COLOR if position_exit.signal == SIGNAL_BUY else SELL_COLOR,
            )
        signal = compute_matrix_signal(snapshot, signal_method=self.signal_method)
        if signal is not None:
            prefix = "B" if signal.signal == SIGNAL_BUY else "S"
            color = ITM_TEXT if muted else ANOMALY_COLOR if signal.kind == SIGNAL_KIND_ANOMALY else (
                BUY_COLOR if signal.signal == SIGNAL_BUY else SELL_COLOR
            )
            return f"{prefix} {format_price(signal.price)} ({format_price(signal.edge)})", color
        mid = effective_mid(snapshot)
        if mid is None:
            return "--", MUTED
        if fair_value_edge_curve_values(snapshot) is None:
            return f"M {format_price(mid)} FV?", SUBSCRIPTION_CONTEXT_COLOR
        return f"M {format_price(mid)}", ITM_TEXT if muted else INK

    def _rank_table_state_signature(self) -> tuple[object, ...]:
        return (
            self.rank_sort_state.column,
            self.rank_sort_state.direction,
            tuple(
                (
                    item.rank,
                    item.tab_id,
                    item.tab_label,
                    item.selection.pair.right,
                    item.lower_strike,
                    item.width,
                    item.pair_key,
                    item.signal.signal,
                    item.signal.kind,
                    item.signal.price,
                    item.signal.edge,
                )
                for item in self.ranked_signals
            ),
        )

    def _redraw_rank_table_if_changed(self) -> None:
        signature = self._rank_table_state_signature()
        if signature == getattr(self, "_rank_render_signature", None):
            return
        self._redraw_rank_table()

    def _redraw_rank_table(self) -> None:
        self._redraw_rank_headings()
        self.rank_item_cells.clear()
        for item_id in self.rank_tree.get_children():
            self.rank_tree.delete(item_id)

        tab_has_split_sides = {tab.tab_id: bool(tab.side_states) for tab in self.matrix_tabs}
        for item in sorted_ranked_signals(self.ranked_signals, self.rank_sort_state):
            signal_text = "BUY" if item.signal.signal == SIGNAL_BUY else "SELL"
            item_id = self.rank_tree.insert(
                "",
                "end",
                values=(
                    item.rank,
                    item.tab_label,
                    item.selection.pair.right,
                    item.pair_key,
                    f"{item.width:.2f}",
                    signal_text,
                    format_price(item.signal.price),
                    format_price(item.signal.edge),
                ),
            )
            selected_key: SelectedMatrixCellKey = (
                (item.selection.pair.right, item.lower_strike, item.width)
                if tab_has_split_sides.get(item.tab_id, False)
                else (item.lower_strike, item.width)
            )
            self.rank_item_cells[item_id] = (item.tab_id, selected_key)
        self._rank_render_signature = OptionsMatrixApp._rank_table_state_signature(self)

    def _redraw_rank_headings(self) -> None:
        for column, label in self.rank_heading_labels.items():
            marker = ""
            if self.rank_sort_state.column == column:
                if self.rank_sort_state.direction == RANK_SORT_ASC:
                    marker = " ^"
                elif self.rank_sort_state.direction == RANK_SORT_DESC:
                    marker = " v"
            self.rank_tree.heading(
                column,
                text=f"{label}{marker}",
                command=lambda selected_column=column: self._on_rank_heading_clicked(selected_column),
            )

    def _redraw_selected_detail(self) -> None:
        if hasattr(self, "positions"):
            self._refresh_position_summary()
        canvas = self.detail_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 520)
        height = max(canvas.winfo_height(), 104)
        active_tab = self._active_tab()
        if active_tab is not None and active_tab.is_stale:
            self.selected_header_var.set("SELECTED SPREAD: (TYPE) --- 0W (EXPIRY) - -- (--)")
            return

        snapshot = (
            self._tab_cell_snapshot(active_tab, self.selected_cell_key)
            if active_tab is not None
            else None
        )
        selection = (
            self._tab_cell_selection(active_tab, self.selected_cell_key)
            if active_tab is not None
            else None
        )
        if snapshot is None and selection is None:
            self.selected_header_var.set("SELECTED SPREAD: (TYPE) --- 0W (EXPIRY) - -- (--)")
            canvas.create_text(width / 2, height / 2, text="No selected spread", fill=FAINT, font=self.grid_font)
            return

        selected_selection = snapshot.selection if snapshot is not None else selection
        signal = (
            compute_matrix_signal(snapshot, signal_method=self.signal_method)
            if snapshot is not None
            else None
        )
        side = selected_selection.pair.right
        expiry = (
            self.applied_matrix_input.display_expiration
            if self.applied_matrix_input is not None
            else self.expiration_var.get() or "EXPIRY"
        )
        combo = pair_key_for_pair(selected_selection.pair)
        price_text = format_price(signal.price) if signal is not None else "--"
        edge_text = format_price(signal.edge) if signal is not None else "--"
        signal_type = signal.signal if signal is not None else "TYPE"
        self.selected_header_var.set(
            f"SELECTED SPREAD: ({signal_type}) {side} {combo} {selected_selection.pair.width:.2f}W "
            f"({expiry}) - {price_text} ({edge_text})"
        )

        if snapshot is None:
            canvas.create_text(width / 2, height / 2, text="Waiting for live quotes", fill=FAINT, font=self.grid_font)
            return

        values = self._detail_values(snapshot)
        numeric_values = detail_display_values(snapshot)
        if not numeric_values:
            canvas.create_text(width / 2, height / 2, text="Waiting for live quotes", fill=FAINT, font=self.grid_font)
            return

        display_min = min(numeric_values)
        display_max = max(numeric_values)
        if display_max <= display_min:
            display_min -= 0.05
            display_max += 0.05
        padding = max(0.05, (display_max - display_min) * 0.12)
        display_min -= padding
        display_max += padding

        left = 70
        right = width - 24
        lanes = (
            ("ANOM", 22),
            ("FV", 48),
            ("SYN", 76),
        )
        fair_value_message = fair_value_unavailable_message(
            snapshot,
            underlying_price=matrix_tab_underlying_reference_price(active_tab, self.underlying_quote),
            min_points=self.formula_settings.fair_value_edge_curve_min_points,
        )
        for label, y in lanes:
            canvas.create_text(8, y, text=label, fill=MUTED, font=self.detail_font_bold, anchor="w")
            if label != "FV" or fair_value_message is None:
                canvas.create_line(left, y, right, y, fill=RULE, width=1)
        lane_y = dict(lanes)

        def value_to_x(value: float) -> float:
            normalized = (value - display_min) / (display_max - display_min)
            return left + ((right - left) * max(0.0, min(1.0, normalized)))

        if fair_value_message is None:
            self._draw_fair_value_edge_curve(canvas, snapshot, value_to_x, lane_y["FV"])
        else:
            canvas.create_text(
                left + 8,
                lane_y["FV"],
                text=fair_value_message,
                fill=SUBSCRIPTION_CONTEXT_COLOR,
                font=self.detail_font,
                anchor="w",
            )

        for lane, value, color, label in values:
            if value is None:
                continue
            y = lane_y[lane]
            x = value_to_x(value)
            canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline=color)
            anchor = "s" if lane == "ANOM" else "n"
            offset = -7 if anchor == "s" else 7
            canvas.create_text(
                x,
                y + offset,
                text=label,
                fill=color,
                font=self.detail_font,
                anchor=anchor,
            )

    def _detail_values(self, snapshot: MonitoredSideSnapshot) -> list[tuple[str, float | None, str, str]]:
        values: list[tuple[str, float | None, str, str]] = []
        synthetic = snapshot.synthetic_quote
        synthetic_mid = snapshot.synthetic_mid

        if snapshot.anomaly_price is not None:
            prefix = "B" if snapshot.anomaly_direction == SIGNAL_BUY else "S"
            values.append(("ANOM", snapshot.anomaly_price, ANOMALY_COLOR, f"{prefix} {format_price(snapshot.anomaly_price)}"))

        if synthetic is not None:
            values.append(("SYN", synthetic.bid, BID_COLOR, self._quote_label("B", synthetic.bid, synthetic.bid_size)))
            values.append(("SYN", synthetic_mid, MID_COLOR, format_price(synthetic_mid)))
            values.append(("SYN", synthetic.ask, ASK_COLOR, self._quote_label("A", synthetic.ask, synthetic.ask_size)))

        return values

    def _draw_fair_value_edge_curve(
        self,
        canvas: tk.Canvas,
        snapshot: MonitoredSideSnapshot,
        value_to_x: Callable[[float], float],
        y: int,
    ) -> None:
        values = fair_value_edge_curve_values(snapshot)
        if values is None:
            return

        buy_endpoint_x = value_to_x(values.buy_endpoint)
        fair_value_x = value_to_x(values.fair_value_mid)
        sell_endpoint_x = value_to_x(values.sell_endpoint)

        canvas.create_line(
            buy_endpoint_x,
            y,
            sell_endpoint_x,
            y,
            fill=FAIR_VALUE_COLOR,
            width=2,
            capstyle=tk.ROUND,
        )
        self._draw_detail_notch(canvas, buy_endpoint_x, y, BUY_COLOR, half_height=4, width=1)
        self._draw_detail_notch(canvas, sell_endpoint_x, y, SELL_COLOR, half_height=4, width=1)
        self._draw_detail_dot(canvas, fair_value_x, y, FAIR_VALUE_COLOR, radius=4)
        canvas.create_text(
            fair_value_x,
            y - 8,
            text=f"FV {format_price(values.fair_value_mid)}",
            fill=FAIR_VALUE_COLOR,
            font=self.detail_font,
            anchor="s",
        )

        if snapshot.opportunity_price is None:
            return

        opportunity_x = value_to_x(snapshot.opportunity_price)
        is_buy = snapshot.opportunity_direction == SIGNAL_BUY
        letter = "B" if is_buy else "S"
        color = BUY_COLOR if is_buy else SELL_COLOR
        self._draw_detail_lettered_dot(canvas, opportunity_x, y, color, letter)
        canvas.create_text(
            opportunity_x,
            y - 20,
            text=format_price(snapshot.opportunity_price),
            fill=color,
            font=self.detail_font,
            anchor="s",
        )

    @staticmethod
    def _draw_detail_notch(
        canvas: tk.Canvas,
        x: float,
        y: float,
        color: str,
        *,
        half_height: int,
        width: int = 2,
    ) -> None:
        canvas.create_line(
            x,
            y - half_height,
            x,
            y + half_height,
            fill=color,
            width=width,
            capstyle=tk.ROUND,
        )

    @staticmethod
    def _draw_detail_dot(
        canvas: tk.Canvas,
        x: float,
        y: float,
        color: str,
        *,
        radius: int = 4,
    ) -> None:
        canvas.create_oval(
            x - radius,
            y - radius,
            x + radius,
            y + radius,
            fill=color,
            outline=GRID_BG,
            width=1,
        )

    def _draw_detail_lettered_dot(
        self,
        canvas: tk.Canvas,
        x: float,
        y: float,
        color: str,
        letter: str,
        *,
        radius: int = 6,
    ) -> None:
        self._draw_detail_dot(canvas, x, y, color, radius=radius)
        canvas.create_text(
            x,
            y,
            text=letter,
            fill=GRID_BG,
            font=self.detail_font_bold,
            anchor="c",
        )

    @staticmethod
    def _quote_label(prefix: str, value: float | None, size: float | None) -> str:
        if value is None:
            return "--"
        if size is None:
            return f"{prefix} {format_price(value)}"
        return f"{prefix} {format_price(value)} ({format_size(size)})"

    def _on_matrix_click(self, event: tk.Event[tk.Canvas]) -> None:
        canvas = getattr(self, "matrix_canvas", None)
        x = canvas.canvasx(event.x) if canvas is not None and hasattr(canvas, "canvasx") else event.x
        y = canvas.canvasy(event.y) if canvas is not None and hasattr(canvas, "canvasy") else event.y
        if self.matrix_reload_hitbox is not None:
            x1, y1, x2, y2 = self.matrix_reload_hitbox
            if x1 <= x <= x2 and y1 <= y <= y2:
                self._reload_active_matrix_tab()
                return
        for x1, y1, x2, y2, cell_key in self.matrix_hitboxes:
            if x1 <= x <= x2 and y1 <= y <= y2:
                tab = self._active_tab()
                if tab is not None:
                    tab.selected_cell_key = cell_key
                    self._sync_view_from_active_tab()
                self._redraw_all()
                return


    def _on_rank_selected(self, _event: tk.Event[ttk.Treeview]) -> None:
        selection = self.rank_tree.selection()
        if not selection:
            return
        cell_key = self.rank_item_cells.get(selection[0])
        if cell_key is None:
            return
        tab_id, selected_cell_key = cell_key
        tab = next((candidate for candidate in self.matrix_tabs if candidate.tab_id == tab_id), None)
        if tab is not None:
            self.active_tab_id = tab.tab_id
            tab.selected_cell_key = selected_cell_key
            self._sync_view_from_active_tab()
            self._redraw_matrix_tabs()
        self._redraw_all()

    def _on_rank_heading_clicked(self, column: str) -> None:
        self.rank_sort_state = next_rank_sort_state(self.rank_sort_state, column)
        self._redraw_rank_table()

    def _clear_loaded_chain(self, *, clear_matrix: bool = True) -> None:
        self.search_result = None
        self.expiration_display_to_raw.clear()
        self.option_records_by_expiration.clear()
        self.expiration_combo["values"] = []
        self.expiration_var.set("")
        self.underlying_quote = None
        self.option_records = []
        self.low_strike_combo["values"] = []
        self.high_strike_combo["values"] = []
        self.low_strike_var.set("")
        self.high_strike_var.set("")
        self._refresh_width_band_controls()
        self._pending_matrix_input = None
        if clear_matrix:
            self._clear_matrix_state()

    def _clear_matrix_state(self) -> None:
        self._stop_recording(silent=True)
        self.matrix_tabs = []
        self.active_tab_id = None
        self._clear_active_view_state()
        self.active_market_data_quote_counts = None
        self._combo_target_key = None
        self._set_market_data_quote_status(None)
        self._redraw_matrix_tabs()
        self._redraw_all()

    def _install_tooltip(
        self,
        widget: tk.Widget,
        text_source: str | Callable[[], str],
    ) -> None:
        popup: tk.Toplevel | None = None

        def hide(_event: tk.Event | None = None) -> None:
            nonlocal popup
            if popup is not None:
                popup.destroy()
                popup = None

        def show(_event: tk.Event | None = None) -> None:
            nonlocal popup
            hide()
            text = text_source() if callable(text_source) else text_source
            text = str(text).strip()
            if not text:
                return
            popup = tk.Toplevel(widget)
            popup.wm_overrideredirect(True)
            tk.Label(
                popup,
                text=text,
                justify="left",
                background=TOOLTIP_BG,
                foreground=TOOLTIP_FG,
                relief="solid",
                borderwidth=1,
                padx=6,
                pady=4,
                wraplength=560,
            ).pack()
            popup.update_idletasks()
            popup_width = popup.winfo_reqwidth()
            popup_height = popup.winfo_reqheight()
            screen_left = widget.winfo_vrootx()
            screen_top = widget.winfo_vrooty()
            screen_right = screen_left + widget.winfo_vrootwidth()
            screen_bottom = screen_top + widget.winfo_vrootheight()
            x = min(
                max(screen_left + 4, widget.winfo_rootx() + 8),
                max(screen_left + 4, screen_right - popup_width - 4),
            )
            below_y = widget.winfo_rooty() + widget.winfo_height() + 4
            y = (
                max(screen_top + 4, widget.winfo_rooty() - popup_height - 4)
                if below_y + popup_height > screen_bottom
                else below_y
            )
            popup.wm_geometry(f"+{x}+{y}")

        widget.bind("<Enter>", show, add="+")
        widget.bind("<Leave>", hide, add="+")
        widget.bind("<ButtonPress>", hide, add="+")

    def _connection_tooltip_text(self) -> str:
        detail = str(getattr(self, "_connection_status_message", "")).strip()
        badge = str(self.connection_var.get()).strip()
        return f"{badge}\n{detail}" if detail and detail != badge else badge

    def _auth_tooltip_text(self) -> str:
        auth_text = str(getattr(self, "_auth_status_detail", "")).strip()
        if auth_text in {"", "Connected"}:
            return "Schwab authorization is current."
        return auth_text

    def _market_data_quote_tooltip_text(self) -> str:
        status = getattr(self, "_market_data_quote_status", None)
        detail = format_market_data_quote_status(status)
        return (
            "L = option legs; U = underlying streams; S = synthetic spreads.\n"
            "LIVE counts are configured subscription targets; server confirmation may be "
            "pending while reconnecting. PENDING counts are staged matrix changes.\n"
            f"{detail}"
        )

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)
        OptionsMatrixApp._update_status_display(self)

    def _on_status_label_configure(self, event: tk.Event) -> None:
        self._status_label_width = max(0, int(getattr(event, "width", 0)))
        OptionsMatrixApp._update_status_display(self)

    def _update_status_display(self) -> None:
        display_var = getattr(self, "status_display_var", None)
        if display_var is None:
            return
        message = " ".join(str(self.status_var.get()).split())
        width = int(getattr(self, "_status_label_width", 0))
        if width <= 0:
            display_var.set(message)
            return
        # Prefer the actual scaled Tk font measurement so the ellipsis remains
        # visible at non-default Windows DPI settings.
        header_font = getattr(self, "grid_font", None)
        measure = getattr(header_font, "measure", None)
        if callable(measure):
            available_width = max(1, width - 8)
            if measure(message) <= available_width:
                display_var.set(message)
                return
            low = 0
            high = len(message)
            while low < high:
                middle = (low + high + 1) // 2
                candidate = f"{message[:middle].rstrip()}…"
                if measure(candidate) <= available_width:
                    low = middle
                else:
                    high = middle - 1
            display_var.set(f"{message[:low].rstrip()}…")
            return

        # Headless test harnesses do not have a Tk font, so retain a
        # conservative character estimate as a deterministic fallback.
        character_limit = max(12, width // 7)
        if len(message) <= character_limit:
            display_var.set(message)
        else:
            display_var.set(f"{message[: max(1, character_limit - 1)].rstrip()}…")

    def _set_market_data_quote_status(self, status: MarketDataQuoteStatus | None) -> None:
        self._market_data_quote_status = status
        self.market_data_quote_status_var.set(format_compact_market_data_quote_status(status))

    def _set_connected_state(self, connected: bool) -> None:
        self._service_connected = bool(connected)
        if hasattr(self, "positions") and hasattr(self, "position_summary_var"):
            if self._refresh_position_alerts():
                self._refresh_active_matrix_render()
        if connected:
            self._connection_active = True
        connection_active = bool(getattr(self, "_connection_active", connected))
        token_issued_at = refresh_token_issued_at()
        connecting = bool(getattr(self, "_connect_inflight", False))
        disconnecting = bool(getattr(self, "_disconnect_inflight", False))
        connection_text = compact_connection_status_text(
            connected,
            connection_active,
            connecting=connecting,
            disconnecting=disconnecting,
            message=str(getattr(self, "_connection_status_message", "")),
        )
        self.connection_var.set(connection_text)
        connection_label = getattr(self, "connection_status_label", None)
        if connection_label is not None:
            if "STOPPED" in connection_text:
                connection_color = CONNECTION_FAILED_COLOR
            elif connecting or disconnecting or (connection_active and not connected):
                connection_color = CONNECTION_PENDING_COLOR
            elif connected:
                connection_color = CONNECTION_LIVE_COLOR
            else:
                connection_color = CONNECTION_OFFLINE_COLOR
            connection_label.configure(foreground=connection_color)

        auth_text = compact_auth_status_text(token_issued_at)
        auth_var = getattr(self, "auth_status_var", None)
        if auth_var is not None:
            auth_var.set(auth_text)
        self._auth_status_detail = (
            connection_status_text(True, token_issued_at)
            if token_issued_at is not None
            else "Schwab authorization timestamp is unavailable."
        )
        auth_label = getattr(self, "auth_status_label", None)
        if auth_label is not None:
            auth_label.configure(
                foreground=(
                    CONNECTION_FAILED_COLOR
                    if auth_text == "⚠ AUTH DUE"
                    else CONNECTION_PENDING_COLOR
                )
            )
        action_inflight = bool(
            getattr(self, "_connect_inflight", False)
            or getattr(self, "_disconnect_inflight", False)
        )
        self.connect_button.configure(
            state=tk.DISABLED if connection_active or action_inflight else tk.NORMAL
        )
        self.disconnect_button.configure(
            state=tk.DISABLED if not connection_active or action_inflight else tk.NORMAL
        )

    def _on_close(self) -> None:
        if self._shutdown_in_progress:
            return
        self._shutdown_in_progress = True
        self._close_after_recorder_stop = True
        self._save_current_matrix_cache()
        self._stop_recording(silent=True)
        if getattr(self, "recorder", None) is not None or getattr(self, "_shutdown_finalized", False):
            return
        OptionsMatrixApp._finalize_close(self)

    def _finalize_close(self) -> None:
        if getattr(self, "_shutdown_finalized", False):
            return
        self._shutdown_finalized = True
        position_tick = getattr(self, "_position_tick_id", None)
        if position_tick is not None:
            self.root.after_cancel(position_tick)
            self._position_tick_id = None
        self._close_after_recorder_stop = False
        try:
            service = self.service
            connection_active = getattr(
                service,
                "connection_active",
                getattr(service, "connected", False),
            )
            if connection_active:
                service.disconnect()
        finally:
            self.root.destroy()

    def start_services(self) -> None:
        """Start sharing with viewers and the autopilot (tests never call this)."""
        if self.services is not None:
            return
        from .studio_services import StudioServices

        self.services = StudioServices(self)
        self.services.start()

    def stop_services(self) -> None:
        services = getattr(self, "services", None)
        if services is not None:
            services.stop()

    def run(self) -> None:
        self.start_services()
        try:
            self.root.mainloop()
        finally:
            self.stop_services()


REFRESH_TOKEN_LIFETIME_DAYS = 7
REFRESH_TOKEN_WARN_AGE_DAYS = 5
_RECONNECT_SCHEDULE_PATTERN = re.compile(
    r"reconnecting\s+in\s+(?P<delay>\d+(?:\.\d+)?)s\s+"
    r"\(attempt\s+(?P<attempt>\d+)/(?P<maximum>\d+)\)",
    re.IGNORECASE,
)


def is_routine_reconnect_schedule_message(message: str) -> bool:
    return _RECONNECT_SCHEDULE_PATTERN.search(str(message)) is not None


def compact_connection_status_text(
    connected: bool,
    connection_active: bool,
    *,
    connecting: bool = False,
    disconnecting: bool = False,
    message: str = "",
) -> str:
    if disconnecting:
        return "◐ DISCONNECTING"
    if connected:
        return "● LIVE"
    if connecting:
        return "◐ CONNECTING"
    if not connection_active:
        return "○ OFFLINE"

    normalized_message = str(message).strip()
    if "reconnect attempts exhausted" in normalized_message.lower():
        return "! RETRY STOPPED"
    match = _RECONNECT_SCHEDULE_PATTERN.search(normalized_message)
    if match is not None:
        delay_value = float(match.group("delay"))
        delay = f"{delay_value:g}"
        return (
            f"↻ RETRY {match.group('attempt')}/{match.group('maximum')} "
            f"· {delay}s"
        )
    return "↻ RETRY"


def compact_auth_status_text(
    token_issued_at: datetime | None,
    *,
    now: datetime | None = None,
) -> str:
    if token_issued_at is None:
        return ""
    current = now or datetime.now(timezone.utc)
    age_days = (current - token_issued_at).days
    if age_days < REFRESH_TOKEN_WARN_AGE_DAYS:
        return ""
    days_left = REFRESH_TOKEN_LIFETIME_DAYS - age_days
    if days_left <= 0:
        return "⚠ AUTH DUE"
    return f"⚠ AUTH {days_left}d"


def connection_status_text(
    connected: bool,
    token_issued_at: datetime | None,
    *,
    now: datetime | None = None,
) -> str:
    if not connected:
        return "Not connected"
    if token_issued_at is None:
        return "Connected"
    current = now or datetime.now(timezone.utc)
    age_days = (current - token_issued_at).days
    if age_days < REFRESH_TOKEN_WARN_AGE_DAYS:
        return "Connected"
    days_left = REFRESH_TOKEN_LIFETIME_DAYS - age_days
    if days_left <= 0:
        return "Connected - Schwab re-auth overdue (run schwab_auth/authorize_schwab.bat)"
    day_text = "day" if days_left == 1 else "days"
    return f"Connected - Schwab re-auth needed within {days_left} {day_text}"


def _round_key(value: float) -> float:
    return round(float(value), 4)


def run() -> None:
    app = OptionsMatrixApp()
    app.run()
