"""One way to write each thing, shared by all four programs.

Rules (from the One Desk "Words and numbers" table):

* Unknown values are an em dash, never zero: ``—``.
* Separators are a middle dot with spaces: ``" · "``.
* Negative numbers use a true minus sign (U+2212), which lines up in mono faces.
* P&L is always signed; fees are always negative.
* Dates are written ``Tue Oct 13 '26`` (sessions) and ``Oct 17 '26`` (expiries);
  ISO dates are only for file names and inputs.
* Times are never converted. Each program keeps its own zone (the Matrix shows
  ET, Playback shows Central without a label), so callers pass the zone label
  they already use, or none.

All functions are pure and locale-independent.
"""

from __future__ import annotations

import datetime as _dt
import math
from decimal import Decimal
from numbers import Real

from .tokens import load as _load

_T = _load()
MISSING: str = _T.format_token("missing")      # "—"
SEP: str = _T.format_token("separator")        # " · "
ELLIPSIS: str = _T.format_token("ellipsis")    # "…"
MINUS: str = _T.format_token("minus")          # "−" (U+2212)
WIDTH_PREFIX: str = _T.format_token("width_prefix")  # "W"

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def is_missing(value: object) -> bool:
    """True for ``None``, NaN and pandas/NumPy missing markers."""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    # pandas.NA / NaT and numpy.nan without importing either package
    name = type(value).__name__
    if name in ("NAType", "NaTType"):
        return True
    try:
        return bool(value != value)  # NaN-like objects are not equal to themselves
    except (TypeError, ValueError):
        return False


def _number(value: object) -> float | None:
    if is_missing(value):
        return None
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        raise TypeError(f"expected a number, got {type(value).__name__}")
    return float(value)


def _signed(text: str, negative: bool, positive: bool, signed: bool) -> str:
    if negative:
        return MINUS + text
    if positive and signed:
        return "+" + text
    return text


def money(value: object, *, signed: bool = False, decimals: int = 2) -> str:
    """``-1234.5`` -> ``−$1,234.50``; ``signed=True`` adds ``+`` to gains. Zero is ``$0.00``."""
    v = _number(value)
    if v is None:
        return MISSING
    v = round(v, decimals)
    body = f"${abs(v):,.{decimals}f}"
    return _signed(body, v < 0, v > 0, signed)


def pnl(value: object, *, decimals: int = 2) -> str:
    """P&L is always signed: ``+$412.50``, ``−$20.60``, ``$0.00``."""
    return money(value, signed=True, decimals=decimals)


def fee(value: object, *, decimals: int = 2) -> str:
    """Fees are always shown as money lost: ``86.4`` and ``-86.4`` both give ``−$86.40``."""
    v = _number(value)
    if v is None:
        return MISSING
    return money(-abs(v), decimals=decimals)


def price(value: object, *, decimals: int = 2, compact: bool = False, signed: bool = False) -> str:
    """Option prices: ``0.42``. ``compact=True`` drops the leading zero (``.42``) for tight matrix cells."""
    v = _number(value)
    if v is None:
        return MISSING
    v = round(v, decimals)
    body = f"{abs(v):.{decimals}f}"
    if compact and body.startswith("0."):
        body = body[1:]
    return _signed(body, v < 0, v > 0, signed)


def strike(value: object) -> str:
    """Strikes without trailing zeros: ``590``, ``592.5``, ``77.25``."""
    v = _number(value)
    if v is None:
        return MISSING
    text = f"{abs(v):.2f}".rstrip("0").rstrip(".")
    return _signed(text, v < 0, False, False)


def pair(lower: object, upper: object) -> str:
    """A strike pair: ``76/79``."""
    return f"{strike(lower)}/{strike(upper)}"


def width(value: object) -> str:
    """Spread width in the one proposed notation: ``W1.00``."""
    v = _number(value)
    if v is None:
        return MISSING
    return f"{WIDTH_PREFIX}{v:.2f}"


def count(value: object) -> str:
    """Whole counts with thousands separators: ``12,345``."""
    v = _number(value)
    if v is None:
        return MISSING
    n = round(v)
    return _signed(f"{abs(n):,}", n < 0, False, False)


def plural(n: object, singular: str, plural_form: str | None = None) -> str:
    """``1 file`` / ``21 files`` (never ``file(s)``)."""
    v = _number(n)
    if v is None:
        return f"{MISSING} {plural_form or singular + 's'}"
    word = singular if round(v) == 1 else (plural_form or singular + "s")
    return f"{count(v)} {word}"


def percent(value: object, *, decimals: int = 1, signed: bool = False, ratio: bool = False) -> str:
    """``17.3%``. Pass ``ratio=True`` when the value is a 0-1 fraction."""
    v = _number(value)
    if v is None:
        return MISSING
    if ratio:
        v *= 100.0
    v = round(v, decimals)
    return _signed(f"{abs(v):.{decimals}f}%", v < 0, v > 0, signed)


# --- dates and times -----------------------------------------------------------

def _as_date(value: object) -> _dt.date | None:
    if is_missing(value):
        return None
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value
    raise TypeError(f"expected a date or datetime, got {type(value).__name__}")


def session_date(value: object) -> str:
    """Trading sessions: ``Tue Oct 13 '26``."""
    d = _as_date(value)
    if d is None:
        return MISSING
    return f"{_DAYS[d.weekday()]} {_MONTHS[d.month - 1]} {d.day} '{d.year % 100:02d}"


def expiry(value: object) -> str:
    """Option expiries: ``Oct 17 '26``."""
    d = _as_date(value)
    if d is None:
        return MISSING
    return f"{_MONTHS[d.month - 1]} {d.day} '{d.year % 100:02d}"


def iso_date(value: object) -> str:
    """``2026-10-13``: only for file names and inputs."""
    d = _as_date(value)
    if d is None:
        return MISSING
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def _as_time(value: object) -> _dt.time | None:
    if is_missing(value):
        return None
    if isinstance(value, _dt.datetime):
        return value.time()
    if isinstance(value, _dt.time):
        return value
    raise TypeError(f"expected a time or datetime, got {type(value).__name__}")


def time_of_day(value: object, *, seconds: bool = True, centiseconds: bool = False,
                zone: str | None = None) -> str:
    """24-hour data times: ``12:42:10``, ``11:42:10.25``, or ``12:42:10 ET`` with ``zone="ET"``.

    The value is shown as given; no time-zone conversion happens here.
    """
    t = _as_time(value)
    if t is None:
        return MISSING
    text = f"{t.hour:02d}:{t.minute:02d}"
    if seconds or centiseconds:
        text += f":{t.second:02d}"
    if centiseconds:
        text += f".{t.microsecond // 10000:02d}"
    return f"{text} {zone}" if zone else text


def clock(value: object, *, zone: str | None = None) -> str:
    """12-hour times for sentences: ``4:00 PM`` or ``4:00 PM ET``."""
    t = _as_time(value)
    if t is None:
        return MISSING
    hour = t.hour % 12 or 12
    text = f"{hour}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"
    return f"{text} {zone}" if zone else text


def duration(seconds: object) -> str:
    """Hold times: ``45s``, ``3m 07s``, ``1h 05m``."""
    v = _number(seconds)
    if v is None or v < 0:
        return MISSING
    total = int(round(v))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def elapsed(seconds: object, *, decimals: int = 2) -> str:
    """Work timings for status bars: ``1.84 s``."""
    v = _number(seconds)
    if v is None or v < 0:
        return MISSING
    return f"{v:.{decimals}f} s"


# --- text --------------------------------------------------------------------

def join(*parts: object, sep: str = SEP) -> str:
    """Join non-empty parts with the One Desk separator: ``a · b · c``."""
    return sep.join(str(p) for p in parts if p is not None and str(p) != "")


def ellipsize(text: str, max_chars: int) -> str:
    """Cut ``text`` to ``max_chars`` including a trailing ``…``."""
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + ELLIPSIS


def caps(text: str) -> str:
    """Caps for labels, column heads and status words (never for buttons or sentences)."""
    return text.upper()
