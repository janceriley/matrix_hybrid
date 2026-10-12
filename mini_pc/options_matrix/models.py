from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime
from math import exp, isfinite, log, sqrt
from typing import Callable, Iterable, Mapping, Sequence

RIGHT_CALL = "CALL"
RIGHT_PUT = "PUT"

DIRECTION_DEBIT = "DEBIT"
DIRECTION_CREDIT = "CREDIT"

SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE = "Fair Value Edge Curve"
SIGNAL_METHODS = (SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE,)
DEFAULT_SIGNAL_METHOD = SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE

SIGNAL_BUY = "BUY"
SIGNAL_SELL = "SELL"

QUOTE_KIND_LEG = "leg"
QUOTE_KIND_UNDERLYING = "underlying"
LIVE_MARKET_DATA_TYPE = 1

BUY_FLOOR_BID = 0.01
FAIR_VALUE_EDGE_WIDTH_RATE = 0.10
FAIR_VALUE_EDGE_WIDTH_2 = 0.14
FAIR_VALUE_EDGE_WIDTH_3 = 0.17
FAIR_VALUE_EDGE_WIDTH_4 = 0.20
FAIR_VALUE_EDGE_WIDTH_5 = 0.22
FAIR_VALUE_EDGE_CURVE_MIN_POINTS = 5
FAIR_VALUE_OUTER_CONTEXT_SEGMENTS = 2
FAIR_VALUE_MONEY_BRIDGE_SEGMENTS = 2

_FAIR_VALUE_LOGIT_EPSILON = 1e-4
_FAIR_VALUE_LOGIT_LIMIT = 9.0
_FAIR_VALUE_ROBUST_ITERATIONS = 12
_FAIR_VALUE_HUBER_MULTIPLIER = 1.5


@dataclass(frozen=True)
class FormulaSettings:
    fair_value_edge_width_rate: float = FAIR_VALUE_EDGE_WIDTH_RATE
    fair_value_edge_width_2: float = FAIR_VALUE_EDGE_WIDTH_2
    fair_value_edge_width_3: float = FAIR_VALUE_EDGE_WIDTH_3
    fair_value_edge_width_4: float = FAIR_VALUE_EDGE_WIDTH_4
    fair_value_edge_width_5: float = FAIR_VALUE_EDGE_WIDTH_5
    fair_value_edge_curve_min_points: float = FAIR_VALUE_EDGE_CURVE_MIN_POINTS


DEFAULT_FORMULA_SETTINGS = FormulaSettings()


def fair_value_edge_for_width(
    width: float,
    settings_or_base: FormulaSettings | float | None = None,
    *,
    base_rate: float | None = None,
) -> float:
    """Return the minimum fair-value edge for a spread width.

    FormulaSettings provides explicit width-one through width-five knots;
    fractional widths interpolate linearly between them and values outside the
    supported range clamp to the nearest endpoint.  A numeric second argument
    (or ``base_rate=``) retains the earlier square-root helper behavior for
    callers that have not yet migrated to the explicit schedule.
    """
    if not _is_finite_number(width):
        return 0.0
    normalized_width = float(width)

    legacy_base = base_rate
    if legacy_base is None and isinstance(settings_or_base, (int, float)):
        legacy_base = float(settings_or_base)
    if legacy_base is not None:
        if not _is_finite_number(legacy_base):
            return 0.0
        return round(
            max(0.0, float(legacy_base)) * sqrt(max(0.0, normalized_width)),
            2,
        )

    settings = (
        settings_or_base
        if isinstance(settings_or_base, FormulaSettings)
        else DEFAULT_FORMULA_SETTINGS
    )
    edges = (
        settings.fair_value_edge_width_rate,
        settings.fair_value_edge_width_2,
        settings.fair_value_edge_width_3,
        settings.fair_value_edge_width_4,
        settings.fair_value_edge_width_5,
    )
    normalized_edges = [
        max(0.0, float(edge)) if _is_finite_number(edge) else 0.0
        for edge in edges
    ]
    if normalized_width <= 1.0:
        return round(normalized_edges[0], 4)
    if normalized_width >= 5.0:
        return round(normalized_edges[4], 4)

    lower_width = int(normalized_width)
    fraction = normalized_width - lower_width
    lower_edge = normalized_edges[lower_width - 1]
    upper_edge = normalized_edges[lower_width]
    return round(lower_edge + ((upper_edge - lower_edge) * fraction), 4)


def normalize_right(value: str) -> str:
    raw = (value or "").strip().upper()
    if raw in {"C", RIGHT_CALL}:
        return RIGHT_CALL
    if raw in {"P", RIGHT_PUT}:
        return RIGHT_PUT
    raise ValueError(f"Unsupported option right: {value!r}")


def normalize_direction(value: str) -> str:
    raw = (value or "").strip().upper()
    if raw not in {DIRECTION_DEBIT, DIRECTION_CREDIT}:
        raise ValueError(f"Unsupported spread direction: {value!r}")
    return raw


def normalize_signal_method(value: str | None) -> str:
    raw = (value or DEFAULT_SIGNAL_METHOD).strip()
    for method in SIGNAL_METHODS:
        if raw.lower() == method.lower():
            return method
    raise ValueError(f"Unsupported signal method: {value!r}")


def format_expiration(expiration: str) -> str:
    raw = (expiration or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def format_strike(strike: float) -> str:
    if float(strike).is_integer():
        return str(int(strike))
    return f"{strike:.2f}".rstrip("0").rstrip(".")


def format_price(price: float | None) -> str:
    if price is None:
        return "--"
    return f"{price:.2f}"


def format_size(size: float | None) -> str:
    if size is None:
        return "--"
    if float(size).is_integer():
        return str(int(size))
    return f"{size:.2f}".rstrip("0").rstrip(".")


def compute_mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return round((bid + ask) / 2.0, 4)


def compute_width(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return round(ask - bid, 4)


@dataclass(frozen=True)
class UnderlyingContract:
    symbol: str
    con_id: int
    exchange: str
    primary_exchange: str
    currency: str


@dataclass(frozen=True)
class OptionChainDefinition:
    symbol: str
    underlying_con_id: int
    exchange: str
    trading_class: str
    multiplier: str
    expirations: tuple[str, ...]
    strikes: tuple[float, ...]


@dataclass(frozen=True)
class SearchResult:
    underlying: UnderlyingContract
    chain: OptionChainDefinition


@dataclass(frozen=True)
class OptionContractRecord:
    con_id: int
    symbol: str
    expiration: str
    strike: float
    right: str
    exchange: str
    currency: str
    trading_class: str
    multiplier: str
    local_symbol: str


@dataclass(frozen=True)
class VerticalPair:
    right: str
    lower: OptionContractRecord
    upper: OptionContractRecord

    @property
    def label(self) -> str:
        return f"{self.right} {format_strike(self.lower.strike)}/{format_strike(self.upper.strike)}"

    @property
    def width(self) -> float:
        return round(self.upper.strike - self.lower.strike, 4)


@dataclass(frozen=True)
class SpreadLeg:
    contract: OptionContractRecord
    action: str


@dataclass(frozen=True)
class VerticalSelection:
    pair: VerticalPair
    direction: str
    legs: tuple[SpreadLeg, SpreadLeg]

    @property
    def label(self) -> str:
        return (
            f"{self.pair.right} {self.direction.title()} "
            f"{format_strike(self.pair.lower.strike)}/{format_strike(self.pair.upper.strike)}"
        )


@dataclass(frozen=True)
class QuoteSnapshot:
    label: str
    kind: str = QUOTE_KIND_LEG
    contract_con_id: int | None = None
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    mark: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    implied_volatility: float | None = None
    model_option_price: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    market_data_type: int | None = None
    quote_time: datetime | None = None
    updated_at: datetime | None = None
    bid_update_received_at: datetime | None = None
    ask_update_received_at: datetime | None = None
    # Explicit valid streamed prices, including repeats; independent of changes.
    bid_confirmed_at: datetime | None = None
    ask_confirmed_at: datetime | None = None

    @property
    def mid(self) -> float | None:
        return compute_mid(self.bid, self.ask)

    @property
    def width(self) -> float | None:
        return compute_width(self.bid, self.ask)


@dataclass(frozen=True)
class SyntheticSpreadQuote:
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None

    @property
    def mid(self) -> float | None:
        return compute_mid(self.bid, self.ask)

    @property
    def width(self) -> float | None:
        return compute_width(self.bid, self.ask)


@dataclass(frozen=True)
class MonitoredSelectionSide:
    selection: VerticalSelection
    is_center: bool = False


@dataclass
class MonitoredSideSnapshot:
    title: str
    pair_key: str
    selection: VerticalSelection
    is_center: bool = False
    synthetic_quote: SyntheticSpreadQuote | None = None
    fair_value_edge_curve_x: float | None = None
    fair_value_edge_curve_mid: float | None = None
    fair_value_edge_curve_edge: float | None = None
    fair_value_edge_curve_buy_endpoint: float | None = None
    fair_value_edge_curve_sell_endpoint: float | None = None
    fair_value_edge_curve_residual: float | None = None
    fair_value_edge_curve_fit_points: int = 0
    fair_value_edge_curve_fit_error: float | None = None
    opportunity_direction: str | None = None
    opportunity_price: float | None = None
    anomaly_direction: str | None = None
    anomaly_price: float | None = None

    @property
    def synthetic_mid(self) -> float | None:
        if self.synthetic_quote is None:
            return None
        return self.synthetic_quote.mid

    @property
    def width(self) -> float:
        return self.selection.pair.width


@dataclass(frozen=True)
class MatrixSelectionPlan:
    visible: dict[tuple[float, float], VerticalSelection]
    context_by_width: dict[float, tuple[VerticalSelection, ...]]


@dataclass(frozen=True)
class MatrixSignal:
    signal: str
    price: float
    edge: float
    kind: str


@dataclass(frozen=True)
class RankedMatrixSignal:
    rank: int
    lower_strike: float
    width: float
    pair_key: str
    selection: VerticalSelection
    snapshot: MonitoredSideSnapshot
    signal: MatrixSignal


SIGNAL_KIND_ANOMALY = "ANOMALY"
SIGNAL_KIND_OPPORTUNITY = "OPPORTUNITY"


def _strike_key(value: float) -> float:
    return round(float(value), 4)


def _width_key(value: float) -> float:
    return round(float(value), 4)


def is_supported_spread_width(width: float) -> bool:
    """Exclude widths ending in .50 while retaining their legs for other pairs."""
    if not _is_finite_number(width):
        return False
    normalized_width = _width_key(width)
    return normalized_width > 0.0 and normalized_width % 1.0 != 0.5


def is_supported_matrix_strike(strike: float) -> bool:
    """Keep .50 strike endpoints out of displayed and recorded matrix spreads."""
    return _is_finite_number(strike) and _strike_key(strike) % 1.0 != 0.5


def is_supported_matrix_pair(pair: VerticalPair) -> bool:
    """Apply both the width and endpoint rules to a matrix cell."""
    return (
        is_supported_spread_width(pair.width)
        and is_supported_matrix_strike(pair.lower.strike)
        and is_supported_matrix_strike(pair.upper.strike)
    )


def _selection_cell_key(selection: VerticalSelection) -> tuple[float, float]:
    pair = selection.pair
    return _strike_key(pair.lower.strike), _width_key(pair.width)


def build_exact_width_vertical_pairs(
    contracts: Iterable[OptionContractRecord],
    width: float,
) -> list[VerticalPair]:
    if not is_supported_spread_width(width):
        return []
    unique_by_strike: dict[float, OptionContractRecord] = {}
    for contract in contracts:
        unique_by_strike.setdefault(_strike_key(contract.strike), contract)

    if len(unique_by_strike) < 2:
        return []

    rights = {contract.right for contract in unique_by_strike.values()}
    if len(rights) != 1:
        raise ValueError("Vertical pair generation expects a single option right.")

    right = next(iter(rights))
    normalized_width = _width_key(width)
    pairs: list[VerticalPair] = []
    for lower_strike in sorted(unique_by_strike):
        upper_strike = _strike_key(lower_strike + normalized_width)
        lower = unique_by_strike.get(lower_strike)
        upper = unique_by_strike.get(upper_strike)
        if lower is None or upper is None:
            continue
        pairs.append(VerticalPair(right=right, lower=lower, upper=upper))
    return pairs


def build_matrix_selection_plan(
    contracts: Iterable[OptionContractRecord],
    visible_lower_strikes: Sequence[float],
    widths: Sequence[float],
    *,
    context_neighbors: int = 2,
    pair_filter: Callable[[VerticalPair], bool] | None = None,
) -> MatrixSelectionPlan:
    visible_keys = {_strike_key(strike) for strike in visible_lower_strikes}
    visible: dict[tuple[float, float], VerticalSelection] = {}
    context_by_width: dict[float, tuple[VerticalSelection, ...]] = {}

    contracts = tuple(contracts)
    for raw_width in widths:
        if not is_supported_spread_width(raw_width):
            continue
        width = _width_key(raw_width)
        pairs = build_exact_width_vertical_pairs(contracts, width)
        if pair_filter is not None:
            pairs = [pair for pair in pairs if pair_filter(pair)]
        if not pairs:
            context_by_width[width] = ()
            continue

        selected_indices = [
            index
            for index, pair in enumerate(pairs)
            if _strike_key(pair.lower.strike) in visible_keys
        ]
        for index in selected_indices:
            selection = resolve_vertical_selection(pairs[index], DIRECTION_DEBIT)
            visible[(_strike_key(pairs[index].lower.strike), width)] = selection

        if not selected_indices:
            context_by_width[width] = ()
            continue

        start = max(0, min(selected_indices) - max(0, context_neighbors))
        end = min(len(pairs), max(selected_indices) + max(0, context_neighbors) + 1)
        context_by_width[width] = tuple(
            resolve_vertical_selection(pair, DIRECTION_DEBIT)
            for pair in pairs[start:end]
        )

    return MatrixSelectionPlan(visible=visible, context_by_width=context_by_width)


def flatten_matrix_context(plan: MatrixSelectionPlan) -> list[VerticalSelection]:
    selections: list[VerticalSelection] = []
    seen: set[tuple[tuple[str, int], ...]] = set()
    for width in sorted(plan.context_by_width):
        for selection in plan.context_by_width[width]:
            if not is_supported_spread_width(selection.pair.width):
                continue
            key = tuple((leg.action, leg.contract.con_id) for leg in selection.legs)
            if key in seen:
                continue
            seen.add(key)
            selections.append(selection)
    return selections


def _resolve_formula_settings(
    formula_settings: FormulaSettings | None = None,
) -> FormulaSettings:
    return formula_settings or DEFAULT_FORMULA_SETTINGS


def build_matrix_snapshots(
    plan: MatrixSelectionPlan,
    leg_quotes: Mapping[int, QuoteSnapshot],
    *,
    formula_settings: FormulaSettings | None = None,
    signal_method: str | None = None,
    underlying_price: float | None = None,
) -> dict[tuple[float, float], MonitoredSideSnapshot]:
    snapshots_by_cell: dict[tuple[float, float], MonitoredSideSnapshot] = {}
    for context_snapshots in build_matrix_context_snapshots(
        plan,
        leg_quotes,
        formula_settings=formula_settings,
        signal_method=signal_method,
        underlying_price=underlying_price,
    ).values():
        for snapshot in context_snapshots:
            cell_key = _selection_cell_key(snapshot.selection)
            if cell_key in plan.visible:
                snapshots_by_cell[cell_key] = snapshot

    return snapshots_by_cell


def build_matrix_context_snapshots(
    plan: MatrixSelectionPlan,
    leg_quotes: Mapping[int, QuoteSnapshot],
    *,
    formula_settings: FormulaSettings | None = None,
    signal_method: str | None = None,
    underlying_price: float | None = None,
) -> dict[float, tuple[MonitoredSideSnapshot, ...]]:
    resolved_settings = _resolve_formula_settings(formula_settings)
    resolved_signal_method = normalize_signal_method(signal_method)
    visible_labels_by_width: dict[float, set[str]] = {}
    for cell_key, selection in plan.visible.items():
        if not is_supported_spread_width(selection.pair.width):
            continue
        visible_labels_by_width.setdefault(cell_key[1], set()).add(selection.label)

    visible_by_width: dict[float, list[VerticalSelection]] = {}
    for cell_key, selection in plan.visible.items():
        if not is_supported_spread_width(selection.pair.width):
            continue
        visible_by_width.setdefault(cell_key[1], []).append(selection)

    snapshots_by_width: dict[float, tuple[MonitoredSideSnapshot, ...]] = {}
    all_snapshots: list[MonitoredSideSnapshot] = []
    widths = sorted(set(plan.context_by_width) | set(visible_by_width))
    for width in widths:
        if not is_supported_spread_width(width):
            continue
        context_selections = [
            selection
            for selection in plan.context_by_width.get(width, ())
            if is_supported_spread_width(selection.pair.width)
        ]
        context_keys = {_selection_cell_key(selection) for selection in context_selections}
        # A visible selection is an evaluation target even if a hand-built or
        # restored plan omitted it from context.  This also guarantees the
        # recorder contract that every visible cell receives a snapshot when
        # its quote is missing.
        context_selections.extend(
            selection
            for selection in visible_by_width.get(width, ())
            if _selection_cell_key(selection) not in context_keys
        )
        if not context_selections:
            snapshots_by_width[width] = ()
            continue
        sides = [
            MonitoredSelectionSide(
                selection=selection,
                is_center=selection.label in visible_labels_by_width.get(width, set()),
            )
            for selection in context_selections
        ]
        group_snapshots = _build_side_snapshots(sides, leg_quotes)
        snapshots_by_width[width] = tuple(group_snapshots)
        all_snapshots.extend(group_snapshots)

    if resolved_signal_method == SIGNAL_METHOD_FAIR_VALUE_EDGE_CURVE and all_snapshots:
        # One base curve per side: all widths share it (see _apply_fair_value_edge_curve).
        _apply_fair_value_edge_curve(
            all_snapshots,
            leg_quotes,
            underlying_price,
            formula_settings=resolved_settings,
        )
    return snapshots_by_width


def compute_matrix_signal(
    snapshot: MonitoredSideSnapshot,
    *,
    signal_method: str | None = None,
) -> MatrixSignal | None:
    normalize_signal_method(signal_method)
    if not is_supported_spread_width(snapshot.width):
        return None
    current_mid = _synthetic_spread_mid(snapshot)
    if current_mid is None:
        return None

    if snapshot.anomaly_direction is not None and snapshot.anomaly_price is not None:
        edge = _compute_signal_edge(snapshot.anomaly_direction, snapshot.anomaly_price, current_mid)
        if edge is None:
            return None
        return MatrixSignal(
            signal=snapshot.anomaly_direction,
            price=snapshot.anomaly_price,
            edge=edge,
            kind=SIGNAL_KIND_ANOMALY,
        )

    if snapshot.opportunity_direction is not None and snapshot.opportunity_price is not None:
        edge = _compute_signal_edge(snapshot.opportunity_direction, snapshot.opportunity_price, current_mid)
        if edge is None:
            return None
        return MatrixSignal(
            signal=snapshot.opportunity_direction,
            price=snapshot.opportunity_price,
            edge=edge,
            kind=SIGNAL_KIND_OPPORTUNITY,
        )

    return None


def _compute_signal_edge(signal: str, signal_price: float, effective_mid: float) -> float | None:
    if signal == SIGNAL_BUY:
        return round(signal_price - effective_mid, 4)
    if signal == SIGNAL_SELL:
        return round(effective_mid - signal_price, 4)
    return None


def build_ranked_matrix_signals(
    snapshots_by_cell: Mapping[tuple[float, float], MonitoredSideSnapshot],
    *,
    signal_method: str | None = None,
) -> list[RankedMatrixSignal]:
    resolved_signal_method = normalize_signal_method(signal_method)
    ranked: list[RankedMatrixSignal] = []
    for cell_key, snapshot in snapshots_by_cell.items():
        signal = compute_matrix_signal(snapshot, signal_method=resolved_signal_method)
        if signal is None:
            continue
        ranked.append(
            RankedMatrixSignal(
                rank=0,
                lower_strike=cell_key[0],
                width=cell_key[1],
                pair_key=pair_key_for_pair(snapshot.selection.pair),
                selection=snapshot.selection,
                snapshot=snapshot,
                signal=signal,
            )
        )

    kind_priority = {SIGNAL_KIND_ANOMALY: 0, SIGNAL_KIND_OPPORTUNITY: 1}
    ranked.sort(
        key=lambda item: (
            kind_priority.get(item.signal.kind, 2),
            -item.signal.edge,
            item.lower_strike,
            item.width,
        )
    )
    return [
        RankedMatrixSignal(
            rank=index,
            lower_strike=item.lower_strike,
            width=item.width,
            pair_key=item.pair_key,
            selection=item.selection,
            snapshot=item.snapshot,
            signal=item.signal,
        )
        for index, item in enumerate(ranked, start=1)
    ]


def effective_mid(snapshot: MonitoredSideSnapshot) -> float | None:
    return _synthetic_spread_mid(snapshot)


def resolve_vertical_selection(pair: VerticalPair, direction: str) -> VerticalSelection:
    normalized_direction = normalize_direction(direction)
    right = normalize_right(pair.right)

    if right == RIGHT_CALL and normalized_direction == DIRECTION_DEBIT:
        legs = (
            SpreadLeg(contract=pair.lower, action="BUY"),
            SpreadLeg(contract=pair.upper, action="SELL"),
        )
    elif right == RIGHT_CALL and normalized_direction == DIRECTION_CREDIT:
        legs = (
            SpreadLeg(contract=pair.lower, action="SELL"),
            SpreadLeg(contract=pair.upper, action="BUY"),
        )
    elif right == RIGHT_PUT and normalized_direction == DIRECTION_DEBIT:
        legs = (
            SpreadLeg(contract=pair.upper, action="BUY"),
            SpreadLeg(contract=pair.lower, action="SELL"),
        )
    else:
        legs = (
            SpreadLeg(contract=pair.upper, action="SELL"),
            SpreadLeg(contract=pair.lower, action="BUY"),
        )

    return VerticalSelection(pair=pair, direction=normalized_direction, legs=legs)


def pair_key_from_strikes(lower_strike: float, upper_strike: float) -> str:
    return f"{format_strike(lower_strike)}/{format_strike(upper_strike)}"


def pair_key_for_pair(pair: VerticalPair) -> str:
    return pair_key_from_strikes(pair.lower.strike, pair.upper.strike)


def build_synthetic_vertical_quote(
    selection: VerticalSelection,
    leg_quotes: Mapping[int, QuoteSnapshot],
) -> SyntheticSpreadQuote | None:
    if not is_supported_spread_width(selection.pair.width):
        return None
    bid_total = 0.0
    ask_total = 0.0
    bid_sizes: list[float] = []
    ask_sizes: list[float] = []
    bid_sizes_complete = True
    ask_sizes_complete = True

    for leg in selection.legs:
        quote = leg_quotes.get(leg.contract.con_id)
        if quote is None:
            return None

        if leg.action == "BUY":
            if quote.bid is None or quote.ask is None:
                return None
            bid_total += quote.bid
            ask_total += quote.ask
            if quote.bid_size is None:
                bid_sizes_complete = False
            else:
                bid_sizes.append(quote.bid_size)
            if quote.ask_size is None:
                ask_sizes_complete = False
            else:
                ask_sizes.append(quote.ask_size)
        else:
            if quote.bid is None or quote.ask is None:
                return None
            bid_total -= quote.ask
            ask_total -= quote.bid
            if quote.ask_size is None:
                bid_sizes_complete = False
            else:
                bid_sizes.append(quote.ask_size)
            if quote.bid_size is None:
                ask_sizes_complete = False
            else:
                ask_sizes.append(quote.bid_size)

    return SyntheticSpreadQuote(
        bid=round(bid_total, 4),
        ask=round(ask_total, 4),
        bid_size=min(bid_sizes) if bid_sizes_complete and bid_sizes else None,
        ask_size=min(ask_sizes) if ask_sizes_complete and ask_sizes else None,
    )


def _build_side_snapshots(
    selection_sides: Sequence[MonitoredSelectionSide],
    leg_quotes: Mapping[int, QuoteSnapshot],
) -> list[MonitoredSideSnapshot]:
    snapshots = [
        MonitoredSideSnapshot(
            title=side.selection.pair.label,
            pair_key=pair_key_for_pair(side.selection.pair),
            selection=side.selection,
            is_center=side.is_center,
            synthetic_quote=build_synthetic_vertical_quote(side.selection, leg_quotes),
        )
        for side in selection_sides
    ]

    for snapshot in snapshots:
        anomaly_direction, anomaly_price = _build_anomaly_signal(snapshot)
        snapshot.anomaly_direction = anomaly_direction
        snapshot.anomaly_price = anomaly_price

    return snapshots


@dataclass
class _FairValueSegment:
    lower_strike: float
    upper_strike: float
    width: float
    x_value: float
    # Every eligible same-side calibration observation has equal initial weight.
    density: float | None = None


@dataclass(frozen=True)
class _FairValueObservation:
    density: float


_MONEYNESS_OTM = "OTM"
_MONEYNESS_CROSSING = "CROSSING"
_MONEYNESS_ITM = "ITM"
_MONEYNESS_UNKNOWN = "UNKNOWN"


def _qualified_vertical_observation(
    pair: VerticalPair,
    leg_quotes: Mapping[int, QuoteSnapshot],
) -> _FairValueObservation | None:
    """Return a structurally usable debit-vertical midpoint.

    A wide synthetic market is uncertain but still contains midpoint
    information.  Quote width therefore neither excludes nor weights an
    observation.  Only malformed leg markets and physically impossible
    vertical midpoints are rejected.
    """
    width = _width_key(pair.width)
    if not is_supported_spread_width(width):
        return None
    lower_leg = leg_quotes.get(pair.lower.con_id)
    upper_leg = leg_quotes.get(pair.upper.con_id)
    for quote in (lower_leg, upper_leg):
        if quote is None:
            return None
        if not all(_is_finite_number(value) for value in (quote.bid, quote.ask)):
            return None
        bid = float(quote.bid)
        ask = float(quote.ask)
        if bid < 0.0 or ask < bid:
            return None

    assert lower_leg is not None and upper_leg is not None
    lower_mid = lower_leg.mid
    upper_mid = upper_leg.mid
    if lower_mid is None or upper_mid is None:
        return None
    right = normalize_right(pair.right)
    spread_mid = upper_mid - lower_mid if right == RIGHT_PUT else lower_mid - upper_mid
    if not _is_finite_number(spread_mid):
        return None
    # Debit verticals are bounded by zero and strike distance.  A tiny
    # tolerance absorbs quote rounding; genuine bound violations are rejected.
    tolerance = min(0.01, width * 0.01)
    if spread_mid < -tolerance or spread_mid > width + tolerance:
        return None
    bounded_mid = min(width, max(0.0, float(spread_mid)))
    return _FairValueObservation(density=bounded_mid / width)


def _interval_moneyness(
    right: str,
    lower_strike: float,
    upper_strike: float,
    underlying_price: float | None,
) -> str:
    if not _is_finite_number(underlying_price):
        return _MONEYNESS_UNKNOWN
    normalized_right = normalize_right(right)
    price = float(underlying_price)
    lower = float(lower_strike)
    upper = float(upper_strike)
    if normalized_right == RIGHT_CALL:
        if price <= lower:
            return _MONEYNESS_OTM
        if price < upper:
            return _MONEYNESS_CROSSING
        return _MONEYNESS_ITM
    if price >= upper:
        return _MONEYNESS_OTM
    if price > lower:
        return _MONEYNESS_CROSSING
    return _MONEYNESS_ITM


def _distance_from_interval(
    lower_strike: float,
    upper_strike: float,
    underlying_price: float,
) -> float:
    price = float(underlying_price)
    if lower_strike <= price <= upper_strike:
        return 0.0
    return min(abs(price - lower_strike), abs(price - upper_strike))


def _fair_value_interval_bounds(strikes: Sequence[float]) -> list[tuple[float, float]]:
    """Build adjacent calibration intervals without any widths ending in .50.

    Skip excluded boundaries until a supported interval can be formed. Merge
    a trailing remainder only if the combined width is also supported.
    Otherwise its endpoints use the last supported interval's fitted density,
    without constructing or fitting an excluded spread.
    """
    if len(strikes) < 2:
        return []
    bounds: list[tuple[float, float]] = []
    lower = strikes[0]
    for upper in strikes[1:]:
        if is_supported_spread_width(upper - lower):
            bounds.append((lower, upper))
            lower = upper
    if (
        lower != strikes[-1]
        and bounds
        and is_supported_spread_width(strikes[-1] - bounds[-1][0])
    ):
        bounds[-1] = (bounds[-1][0], strikes[-1])
    return bounds


def _base_curve_segments(
    snapshots: Sequence[MonitoredSideSnapshot],
    leg_quotes: Mapping[int, QuoteSnapshot],
    underlying_price: float | None,
) -> list[_FairValueSegment]:
    """Build atomic outputs and the intentionally small calibration universe.

    Direct same-side OTM mids inside the selected range, two same-side OTM
    context intervals, and the same-side near-money bridge are retained
    whenever their leg markets are structurally usable.  Quote width, size,
    and freshness do not decide eligibility or statistical weight.
    """
    contract_by_strike: dict[float, OptionContractRecord] = {}
    right: str | None = None
    visible_pairs: list[VerticalPair] = []
    for snapshot in snapshots:
        pair = snapshot.selection.pair
        if not is_supported_spread_width(pair.width):
            continue
        normalized_pair_right = normalize_right(pair.right)
        if right is None:
            right = normalized_pair_right
        elif right != normalized_pair_right:
            raise ValueError("A matrix fair-value curve expects one display right.")
        if snapshot.is_center:
            visible_pairs.append(pair)
        for contract in (pair.lower, pair.upper):
            contract_by_strike.setdefault(_strike_key(contract.strike), contract)
    if right is None or len(contract_by_strike) < 2:
        return []

    strikes = sorted(contract_by_strike)
    segments: list[_FairValueSegment] = []
    direct_observations: dict[tuple[float, float], _FairValueObservation] = {}
    direct_states: dict[tuple[float, float], str] = {}
    for lower_strike, upper_strike in _fair_value_interval_bounds(strikes):
        width = _width_key(upper_strike - lower_strike)
        pair = VerticalPair(
            right=right,
            lower=contract_by_strike[lower_strike],
            upper=contract_by_strike[upper_strike],
        )
        bounds = (lower_strike, upper_strike)
        observation = _qualified_vertical_observation(pair, leg_quotes)
        if observation is not None:
            direct_observations[bounds] = observation
        direct_states[bounds] = _interval_moneyness(
            right, lower_strike, upper_strike, underlying_price
        )
        segments.append(
            _FairValueSegment(
                lower_strike=lower_strike,
                upper_strike=upper_strike,
                width=width,
                x_value=_fair_value_edge_curve_x_for_strikes(
                    right, lower_strike, upper_strike, underlying_price
                ),
            )
        )

    segment_by_bounds = {
        (segment.lower_strike, segment.upper_strike): segment
        for segment in segments
    }

    # Without spot there is no honest OTM/ITM classification.  The matrix can
    # still load, display, and record every output, but fair value waits for the
    # first underlying quote instead of fitting potentially distorting ITM data.
    if not _is_finite_number(underlying_price):
        return segments

    price = float(underlying_price)
    visible_lower = (
        min(_strike_key(pair.lower.strike) for pair in visible_pairs)
        if visible_pairs
        else None
    )
    visible_upper = (
        max(_strike_key(pair.upper.strike) for pair in visible_pairs)
        if visible_pairs
        else None
    )

    # Direct OTM intervals inside the selected span are the calibration core.
    # Same-right context beyond the visible OTM edge remains capped at two
    # atomic intervals so a long subscription tail cannot dominate the view.
    outer_otm: list[tuple[float, float]] = []
    for bounds, observation in direct_observations.items():
        if direct_states[bounds] != _MONEYNESS_OTM:
            continue
        lower_strike, upper_strike = bounds
        inside_visible = (
            visible_lower is None
            or visible_upper is None
            or (lower_strike >= visible_lower and upper_strike <= visible_upper)
        )
        if inside_visible:
            segment = segment_by_bounds[bounds]
            segment.density = observation.density
            continue
        if right == RIGHT_CALL and visible_upper is not None and lower_strike >= visible_upper:
            outer_otm.append(bounds)
        elif right == RIGHT_PUT and visible_lower is not None and upper_strike <= visible_lower:
            outer_otm.append(bounds)

    outer_otm.sort(
        key=lambda bounds: (
            _distance_from_interval(bounds[0], bounds[1], price),
            bounds[0],
        )
    )
    for bounds in outer_otm[:FAIR_VALUE_OUTER_CONTEXT_SEGMENTS]:
        observation = direct_observations[bounds]
        segment = segment_by_bounds[bounds]
        segment.density = observation.density

    crossing_intervals: set[tuple[float, float]] = set()
    for bounds, state in direct_states.items():
        if state != _MONEYNESS_CROSSING:
            continue
        direct = direct_observations.get(bounds)
        if direct is not None:
            segment_by_bounds[bounds].density = direct.density
            crossing_intervals.add(bounds)

    # Complete the two-interval near-money bridge with the nearest usable
    # same-side ITM observations. Deeper ITM quotes remain evaluation targets
    # because their market mids are commonly the least representative.
    direct_itm_slots = max(
        0,
        FAIR_VALUE_MONEY_BRIDGE_SEGMENTS - len(crossing_intervals),
    )
    if direct_itm_slots > 0:
        direct_itm_bounds = [
            bounds
            for bounds, state in direct_states.items()
            if state == _MONEYNESS_ITM
            and bounds in direct_observations
            and segment_by_bounds[bounds].density is None
        ]
        direct_itm_bounds.sort(
            key=lambda bounds: (
                _distance_from_interval(bounds[0], bounds[1], price),
                bounds[0],
            )
        )
        for bounds in direct_itm_bounds[:direct_itm_slots]:
            observation = direct_observations[bounds]
            segment = segment_by_bounds[bounds]
            segment.density = observation.density

    return segments


def _apply_fair_value_edge_curve(
    snapshots: Sequence[MonitoredSideSnapshot],
    leg_quotes: Mapping[int, QuoteSnapshot],
    underlying_price: float | None,
    *,
    formula_settings: FormulaSettings | None = None,
) -> None:
    """Fit leave-one-out atomic fair values and derive all wider widths.

    Each calibrated supported segment is priced by the same robust, bounded
    curve with that segment's direct observation removed.  Non-calibration
    outputs use the full reference curve when it has the configured support,
    because they have no self-observation to remove.  Every wider spread is
    the sum of the resulting atomic fair values it spans, preserving
    fair(A,C) == fair(A,B) + fair(B,C). Endpoints inside supported segments use
    interpolated cumulative fair value. A trailing excluded remainder extends
    the last supported segment's density. No midpoint for a width ending in
    .50 is constructed or fitted.
    """
    snapshots = [
        snapshot for snapshot in snapshots
        if is_supported_spread_width(snapshot.width)
    ]
    settings = formula_settings or DEFAULT_FORMULA_SETTINGS
    min_points = max(1, int(settings.fair_value_edge_curve_min_points))
    segments = _base_curve_segments(
        snapshots,
        leg_quotes,
        underlying_price,
    )
    calibration_indexes = [
        index
        for index, segment in enumerate(segments)
        if _is_finite_number(segment.density)
    ]
    fit_points = len(calibration_indexes)

    for snapshot in snapshots:
        x_value = _fair_value_edge_curve_x_value(snapshot, underlying_price)
        snapshot.fair_value_edge_curve_x = round(x_value, 6) if _is_finite_number(x_value) else None
        snapshot.fair_value_edge_curve_fit_points = fit_points

    if not segments or fit_points == 0:
        # With no structurally usable market observation there is nothing to
        # anchor even the deterministic sparse-data fallback.
        return

    x_values = [segment.x_value for segment in segments]
    densities = [segment.density for segment in segments]
    reference_fair_densities, fit_error, _fit_points = _fit_fair_value_edge_curve(
        x_values,
        densities,
        minimum_points=min_points,
    )

    # Price each atomic calibration observation out of sample. Removing its
    # direct observation leaves only the other eligible same-side observations
    # in the candidate's fit. When the configured support target is not met by
    # the applicable full or leave-one-out fit, that atomic fair value is
    # unavailable rather than silently producing an under-supported signal.
    support_target = max(2, min_points)
    decision_fair_densities: list[float | None] = [None] * len(segments)
    for index, segment in enumerate(segments):
        if not _is_finite_number(segment.density):
            if fit_points >= support_target:
                decision_fair_densities[index] = reference_fair_densities[index]
            continue
        if fit_points - 1 < support_target:
            decision_fair_densities[index] = None
            continue
        leave_one_out_densities = list(densities)
        leave_one_out_densities[index] = None
        candidate_fair_densities, _candidate_error, candidate_fit_points = (
            _fit_fair_value_edge_curve(
                x_values,
                leave_one_out_densities,
                minimum_points=min_points,
            )
        )
        decision_fair_densities[index] = (
            candidate_fair_densities[index]
            if candidate_fit_points >= support_target
            else None
        )

    # Supported intervals form a contiguous partition. Cumulative values cover
    # interior fractional endpoints and extend the last interval's density to
    # an excluded trailing remainder, preserving additivity without its quote.
    lower_bounds = [segment.lower_strike for segment in segments]
    upper_bounds = [segment.upper_strike for segment in segments]
    if lower_bounds[1:] != upper_bounds[:-1]:
        return
    atomic_values = [
        None if density is None else round(min(1.0, max(0.0, density)) * segment.width, 4)
        for segment, density in zip(segments, decision_fair_densities)
    ]

    fair_prefix = [0.0]
    missing_prefix = [0]
    for value in atomic_values:
        fair_prefix.append(fair_prefix[-1] + (value or 0.0))
        missing_prefix.append(missing_prefix[-1] + int(value is None))
    rounded_error = round(fit_error, 6) if fit_error is not None else None

    for snapshot in snapshots:
        snapshot.fair_value_edge_curve_fit_error = rounded_error
        pair = snapshot.selection.pair
        lower = _strike_key(pair.lower.strike)
        upper = _strike_key(pair.upper.strike)
        start = min(bisect_right(lower_bounds, lower) - 1, len(segments) - 1)
        end = min(bisect_left(upper_bounds, upper), len(segments) - 1)
        if (
            lower < lower_bounds[0]
            or start > end
            or missing_prefix[end + 1] != missing_prefix[start]
        ):
            continue

        lower_value = round(
            fair_prefix[start]
            + float(atomic_values[start]) * (lower - lower_bounds[start]) / segments[start].width,
            4,
        )
        upper_value = round(
            fair_prefix[end]
            + float(atomic_values[end]) * (upper - lower_bounds[end]) / segments[end].width,
            4,
        )
        fair_value_mid = round(upper_value - lower_value, 4)
        edge = fair_value_edge_for_width(
            snapshot.width,
            settings,
        )
        buy_endpoint = round(fair_value_mid - edge, 4)
        sell_endpoint = round(fair_value_mid + edge, 4)

        snapshot.fair_value_edge_curve_mid = fair_value_mid
        snapshot.fair_value_edge_curve_edge = edge
        snapshot.fair_value_edge_curve_buy_endpoint = buy_endpoint
        snapshot.fair_value_edge_curve_sell_endpoint = sell_endpoint
        synthetic_mid = _synthetic_spread_mid(snapshot)
        snapshot.fair_value_edge_curve_residual = (
            round(synthetic_mid - fair_value_mid, 4) if synthetic_mid is not None else None
        )

        if synthetic_mid is None:
            continue
        if synthetic_mid <= buy_endpoint:
            snapshot.opportunity_direction = SIGNAL_BUY
            snapshot.opportunity_price = buy_endpoint
        elif synthetic_mid >= sell_endpoint:
            snapshot.opportunity_direction = SIGNAL_SELL
            snapshot.opportunity_price = sell_endpoint


def _fair_value_edge_curve_x_value(
    snapshot: MonitoredSideSnapshot,
    underlying_price: float | None,
) -> float:
    pair = snapshot.selection.pair
    return _fair_value_edge_curve_x_for_strikes(
        pair.right,
        pair.lower.strike,
        pair.upper.strike,
        underlying_price,
    )


def _fair_value_edge_curve_x_for_strikes(
    right: str,
    lower_strike: float,
    upper_strike: float,
    underlying_price: float | None,
) -> float:
    spread_midpoint = (float(lower_strike) + float(upper_strike)) / 2.0
    if _is_finite_number(underlying_price):
        price = float(underlying_price)
        if right == RIGHT_CALL:
            return price - spread_midpoint
        if right == RIGHT_PUT:
            return spread_midpoint - price
    return spread_midpoint


def _fit_fair_value_edge_curve(
    x_values: Sequence[float],
    mids: Sequence[float | None],
    *,
    minimum_points: int = FAIR_VALUE_EDGE_CURVE_MIN_POINTS,
) -> tuple[list[float | None], float | None, int]:
    """Fit one robust two-parameter logistic curve in density space.

    The sigmoid is smooth, nondecreasing, and bounded between zero and one by
    construction.  Observations begin with equal weight in production; Huber
    reweighting only limits points whose density residual disagrees sharply
    with the common curve.  Below the configured support target, a bounded
    robust-center curve keeps fair value market-anchored instead of disabling
    it or forcing an under-supported slope.
    """
    entries = _fair_value_curve_entries(x_values, mids)
    if not entries:
        return [None for _value in x_values], None, 0

    fit_points = len(entries)
    unique_x = {round(entry[1], 10) for entry in entries}
    observed_range = max(entry[2] for entry in entries) - min(
        entry[2] for entry in entries
    )
    support_target = max(2, int(minimum_points))

    if (
        fit_points < support_target
        or len(unique_x) < 2
        or observed_range <= 1e-12
    ):
        fair_values = _constant_fair_value_curve(
            x_values,
            _robust_density_center(entries),
        )
    else:
        parameters = _fit_robust_logistic_parameters(entries)
        if parameters is None:
            fair_values = _constant_fair_value_curve(
                x_values,
                _robust_density_center(entries),
            )
        else:
            intercept, slope, center, scale = parameters
            fair_values = [
                _bounded_sigmoid(
                    intercept + (slope * ((float(x_value) - center) / scale))
                )
                if _is_finite_number(x_value)
                else None
                for x_value in x_values
            ]

    residuals = [
        abs(entry[2] - float(fair_values[entry[0]]))
        for entry in entries
        if fair_values[entry[0]] is not None
    ]
    fit_error = sum(residuals) / len(residuals) if residuals else 0.0
    return fair_values, fit_error, fit_points


def _fair_value_curve_entries(
    x_values: Sequence[float],
    mids: Sequence[float | None],
) -> list[tuple[int, float, float]]:
    entries: list[tuple[int, float, float]] = []
    for index, (x_value, mid) in enumerate(zip(x_values, mids)):
        if not _is_finite_number(x_value) or not _is_finite_number(mid):
            continue
        entries.append(
            (
                index,
                float(x_value),
                min(1.0, max(0.0, float(mid))),
            )
        )
    return entries


def _constant_fair_value_curve(
    x_values: Sequence[float],
    density: float,
) -> list[float | None]:
    bounded = min(1.0, max(0.0, float(density)))
    return [
        bounded if _is_finite_number(x_value) else None
        for x_value in x_values
    ]


def _robust_density_center(
    entries: Sequence[tuple[int, float, float]],
) -> float:
    """Return the deterministic median density used by the sparse fallback."""
    return _median([entry[2] for entry in entries])


def _fit_robust_logistic_parameters(
    entries: Sequence[tuple[int, float, float]],
) -> tuple[float, float, float, float] | None:
    """Fit ``sigmoid(intercept + slope*x)`` with density-space Huber IRLS."""
    point_count = len(entries)
    if point_count <= 0:
        return None
    center = sum(entry[1] for entry in entries) / point_count
    scale = max(abs(entry[1] - center) for entry in entries) or 1.0
    rows = [
        (
            (entry[1] - center) / scale,
            entry[2],
        )
        for entry in entries
    ]

    # A clipped-logit line is only the deterministic starting point.  All
    # robust residuals and optimization below are computed in density space.
    mean_x = sum(x_value for x_value, _mid in rows) / point_count
    logits = [
        _density_logit(mid)
        for _x_value, mid in rows
    ]
    mean_logit = sum(logits) / point_count
    variance_x = sum(
        (x_value - mean_x) ** 2
        for x_value, _mid in rows
    )
    covariance = sum(
        (x_value - mean_x) * (logit - mean_logit)
        for logit, (x_value, _mid) in zip(logits, rows)
    )
    slope = max(0.0, covariance / variance_x) if variance_x > 1e-12 else 0.0
    intercept = mean_logit - (slope * mean_x)
    if slope <= 1e-12:
        intercept = _density_logit(_robust_density_center(entries))

    for _iteration in range(_FAIR_VALUE_ROBUST_ITERATIONS):
        probabilities = [
            _bounded_sigmoid(intercept + (slope * x_value))
            for x_value, _mid in rows
        ]
        residuals = [
            mid - probability
            for probability, (_x_value, mid) in zip(probabilities, rows)
        ]
        residual_center = _median(residuals)
        mad = _median(
            [abs(residual - residual_center) for residual in residuals]
        )
        residual_scale = 1.4826 * mad
        huber_delta = max(0.025, _FAIR_VALUE_HUBER_MULTIPLIER * residual_scale)

        normal_00 = 1e-9
        normal_01 = 0.0
        normal_11 = 1e-9
        rhs_0 = 0.0
        rhs_1 = 0.0
        for probability, residual, (x_value, _mid) in zip(
            probabilities,
            residuals,
            rows,
        ):
            robust_weight = min(1.0, huber_delta / max(abs(residual), 1e-12))
            derivative = probability * (1.0 - probability)
            derivative_x = derivative * x_value
            normal_00 += robust_weight * derivative * derivative
            normal_01 += robust_weight * derivative * derivative_x
            normal_11 += robust_weight * derivative_x * derivative_x
            rhs_0 += robust_weight * derivative * residual
            rhs_1 += robust_weight * derivative_x * residual

        determinant = (normal_00 * normal_11) - (normal_01 * normal_01)
        if determinant <= 1e-15:
            break
        intercept_step = (
            (rhs_0 * normal_11) - (rhs_1 * normal_01)
        ) / determinant
        slope_step = (
            (normal_00 * rhs_1) - (normal_01 * rhs_0)
        ) / determinant
        intercept_step = min(2.0, max(-2.0, intercept_step))
        slope_step = min(2.0, max(-2.0, slope_step))

        current_loss = _huber_density_loss(
            rows,
            intercept,
            slope,
            huber_delta,
        )
        accepted = False
        step_fraction = 1.0
        next_intercept = intercept
        next_slope = slope
        for _attempt in range(8):
            candidate_intercept = intercept + (step_fraction * intercept_step)
            candidate_slope = max(0.0, slope + (step_fraction * slope_step))
            candidate_loss = _huber_density_loss(
                rows,
                candidate_intercept,
                candidate_slope,
                huber_delta,
            )
            if candidate_loss <= current_loss + 1e-15:
                next_intercept = candidate_intercept
                next_slope = candidate_slope
                accepted = True
                break
            step_fraction *= 0.5
        if not accepted:
            break
        if (
            abs(next_intercept - intercept) <= 1e-8
            and abs(next_slope - slope) <= 1e-8
        ):
            intercept = next_intercept
            slope = next_slope
            break
        intercept = next_intercept
        slope = next_slope

    return intercept, max(0.0, slope), center, scale


def _huber_density_loss(
    rows: Sequence[tuple[float, float]],
    intercept: float,
    slope: float,
    delta: float,
) -> float:
    total = 0.0
    for x_value, mid in rows:
        residual = abs(mid - _bounded_sigmoid(intercept + (slope * x_value)))
        if residual <= delta:
            loss = 0.5 * residual * residual
        else:
            loss = delta * (residual - (0.5 * delta))
        total += loss
    return total


def _density_logit(density: float) -> float:
    bounded = min(
        1.0 - _FAIR_VALUE_LOGIT_EPSILON,
        max(_FAIR_VALUE_LOGIT_EPSILON, float(density)),
    )
    return log(bounded / (1.0 - bounded))


def _bounded_sigmoid(value: float) -> float:
    bounded_value = min(
        _FAIR_VALUE_LOGIT_LIMIT,
        max(-_FAIR_VALUE_LOGIT_LIMIT, float(value)),
    )
    return 1.0 / (1.0 + exp(-bounded_value))


def _median(values: Sequence[float]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _build_anomaly_signal(snapshot: MonitoredSideSnapshot) -> tuple[str | None, float | None]:
    current_mid = _synthetic_spread_mid(snapshot)
    width = snapshot.width
    if current_mid is None:
        return None, None

    if current_mid <= 0:
        buy_floor = BUY_FLOOR_BID if width >= BUY_FLOOR_BID else width
        return SIGNAL_BUY, round(buy_floor, 4)

    if current_mid >= width:
        return SIGNAL_SELL, round(max(0.0, width - BUY_FLOOR_BID), 4)

    return None, None


def _synthetic_spread_mid(snapshot: MonitoredSideSnapshot) -> float | None:
    if snapshot.synthetic_quote is None:
        return None
    synthetic_mid = snapshot.synthetic_quote.mid
    return synthetic_mid if _is_finite_number(synthetic_mid) else None


def _is_finite_number(value: object) -> bool:
    try:
        return isfinite(float(value))
    except (TypeError, ValueError):
        return False
