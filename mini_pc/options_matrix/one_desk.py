"""One Desk for Options Matrix Studio and Viewer: colors, fonts and the ttk theme.

The Viewer refuses to show anything when its copy of ``app.py`` or ``models.py``
differs from the mini PC's (``share_protocol.MATH_SOURCE_FILES``). Every look
decision therefore lives here, with the One Desk package itself vendored in the
``onedesk`` folder next to this file, so restyling never changes that
fingerprint. ``app.py`` imports its color names and fonts from this module.

Both programs open light. Set the ``OPTIONS_MATRIX_THEME`` environment variable
to ``dark`` to open dark, or to ``auto`` to follow the Windows app setting.
"""

from __future__ import annotations

import os
import tkinter as tk
from collections.abc import Sequence
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk

from .onedesk import color as _color
from .onedesk import tk_theme
from .onedesk.tokens import TokenError, resolve_mode
from .onedesk.tokens import load as _load_tokens

THEME_ENV_VAR = "OPTIONS_MATRIX_THEME"


def theme_mode() -> str:
    """``"light"`` unless the override asks for ``dark`` (or ``auto``, the Windows app setting)."""

    value = os.environ.get(THEME_ENV_VAR, "").strip().lower() or "light"
    try:
        return resolve_mode(value)
    except TokenError:
        return "light"


MODE = theme_mode()
_tokens = _load_tokens()
PALETTE = _tokens.palette(MODE)
_p = PALETTE

# The sharing badge's glyph comes from the One Desk token, so app.py never spells it out.
SHARING_GLYPH = _tokens.glyph("sharing")

# Text and surfaces.
INK = _p.ink
INK_2 = _p.ink_2
MUTED = _p.muted
FAINT = _p.faint
NAVY = _p.navy
SURFACE = _p.surface
RAISED = _p.raised
SUNKEN = _p.sunken
RULE = _p.rule
RULE_STRONG = _p.rule_strong
UP_TINT = _p.up_tint
TOOLTIP_BG = _p.ink
TOOLTIP_FG = _p.surface

# The matrix. Healthy cells keep plain hairlines; only states that need you add color.
GRID_LINE = _p.rule_strong
GRID_BG = _p.surface
HEADER_BG = _p.raised
SELECTED_BG = _p.navy_tint
MISSING_BG = _p.raised
ITM_BG = _p.itm
ITM_SELECTED_BG = _color.mix(_p.navy, _p.itm, 0.15)
ITM_TEXT = _p.ink_2
HATCH = _p.hatch  # failed streams: their quotes are not trustworthy
POSITION_ICON_COLOR = _p.navy

# Stream health. Confirmed is green in the Settings charts and legend, never as a cell border.
SUBSCRIPTION_CONFIRMED_COLOR = _p.ok
SUBSCRIPTION_PENDING_COLOR = _p.navy
SUBSCRIPTION_RECONCILING_COLOR = _p.navy  # drawn dashed, so it reads apart from pending
SUBSCRIPTION_CONTEXT_COLOR = _p.warn
SUBSCRIPTION_FAILED_COLOR = _p.bad

# Status words: glyph and caps word in one color.
RECORDER_OFF_COLOR = _p.muted
RECORDER_ACTIVE_COLOR = _p.rec
RECORDER_PENDING_COLOR = _p.warn
CONNECTION_OFFLINE_COLOR = _p.muted
CONNECTION_LIVE_COLOR = _p.ok
CONNECTION_PENDING_COLOR = _p.warn
CONNECTION_FAILED_COLOR = _p.bad
ERROR_TEXT = _p.bad

# Signals, quotes and the liquidity gauge.
BUY_COLOR = _p.up
SELL_COLOR = _p.down
ANOMALY_COLOR = _p.model
MID_COLOR = _p.ink
BID_COLOR = _p.quote
ASK_COLOR = _p.quote_2
FAIR_VALUE_COLOR = _p.model
LIQUIDITY_TRACK_COLOR = _p.rule_strong
LIQUIDITY_WIDTH_COLOR = _p.quote_2
LIQUIDITY_OVERFLOW_COLOR = _p.warn
POSITION_BUY_BG = _p.up_tint
POSITION_SELL_BG = _p.down_tint

# Position table rows with a live exit alert.
POSITION_ROW_BUY_BG = _p.up_tint
POSITION_ROW_BUY_FG = _p.up
POSITION_ROW_SELL_BG = _p.down_tint
POSITION_ROW_SELL_FG = _p.down

# Dialog section headings (a named font, so it exists once the theme is applied).
TITLE_FONT = tk_theme.FONTS["title"][0]

FAILED_HATCH_SPACING = 6
RECURRENT_FAILED_HATCH_SPACING = 4


@dataclass(frozen=True)
class MatrixFonts:
    """The named One Desk fonts the matrix canvases draw with."""

    grid: tkfont.Font  # numbers in cells: Cascadia Mono
    grid_bold: tkfont.Font
    detail: tkfont.Font  # small numbers in the detail and Settings charts
    detail_bold: tkfont.Font  # small caps labels
    header: tkfont.Font  # caps column heads: Bahnschrift SemiBold
    prose: tkfont.Font  # sentences on canvases (the Settings guide): Segoe UI Variable
    prose_bold: tkfont.Font


def apply(root: tk.Tk) -> tk_theme.TkTheme:
    """Style ``root`` with One Desk: ttk "clam" styles, the faces and, in dark mode, a dark title bar."""

    return tk_theme.apply(root, MODE)


def matrix_fonts(theme: tk_theme.TkTheme) -> MatrixFonts:
    fonts = theme.fonts
    return MatrixFonts(
        grid=fonts["num"],
        grid_bold=fonts["num_strong"],
        detail=fonts["num_small"],
        detail_bold=fonts["label"],
        header=fonts["label"],
        prose=fonts["ui_small"],
        prose_bold=fonts["ui_strong"],
    )


def caps_labels(root: tk.Misc) -> int:
    """Give fixed ALL-CAPS ttk labels (TICKER, EXPIRY, STATUS) the One Desk caps-label style.

    Labels bound to a text variable show live status words and keep their own
    colors. Returns how many labels changed.
    """

    changed = 0
    stack: list[tk.Misc] = [root]
    while stack:
        widget = stack.pop()
        stack.extend(widget.winfo_children())
        if not isinstance(widget, ttk.Label) or str(widget.cget("textvariable")):
            continue
        text = str(widget.cget("text")).strip()
        if text and text.upper() == text and any(ch.isalpha() for ch in text) and str(widget.cget("style")) in ("", "TLabel"):
            widget.configure(style="Caps.TLabel")
            changed += 1
    return changed


def draw_hatch(canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float, *, spacing: float,
               tags: str | tuple[str, ...] = ()) -> list[int]:
    """The One Desk hatch inside a cell: diagonal hairlines that mark untrusted quotes."""

    return tk_theme.draw_hatch(canvas, x0, y0, x1, y1, color=HATCH, spacing=spacing, tags=tags)


def spread_labels(centers: Sequence[float], widths: Sequence[float], *, low: float, high: float,
                  gap: float = 6.0) -> list[float]:
    """New centers for labels on one line, moved apart just enough that none overlap.

    The detail chart prints bid, mid and ask under their dots; when the prices
    are close, a crowded run of labels spreads out evenly around its dots.
    Labels keep their left-to-right order and stay inside ``low``..``high``
    when they fit.
    """

    def offsets(run: list[int]) -> list[float]:
        # Each label's center relative to the run's first label, packed ``gap`` apart.
        steps = [0.0]
        for left, right in zip(run, run[1:]):
            steps.append(steps[-1] + (widths[left] + widths[right]) / 2 + gap)
        return steps

    def first_center(run: list[int]) -> float:
        # Where the run's labels move least, all told (least squares).
        return sum(centers[index] - step for index, step in zip(run, offsets(run))) / len(run)

    def right_edge(run: list[int]) -> float:
        return first_center(run) + offsets(run)[-1] + widths[run[-1]] / 2

    runs: list[list[int]] = []
    for index in sorted(range(len(centers)), key=lambda i: centers[i]):
        runs.append([index])
        while len(runs) > 1 and first_center(runs[-1]) - widths[runs[-1][0]] / 2 < right_edge(runs[-2]) + gap:
            runs[-2:] = [runs[-2] + runs[-1]]

    firsts = [first_center(run) for run in runs]
    edge = low  # push runs right off the low bound, then left off the high bound
    for number, run in enumerate(runs):
        firsts[number] = max(firsts[number], edge + widths[run[0]] / 2)
        edge = firsts[number] + offsets(run)[-1] + widths[run[-1]] / 2 + gap
    edge = high
    for number in reversed(range(len(runs))):
        run = runs[number]
        firsts[number] = min(firsts[number], edge - offsets(run)[-1] - widths[run[-1]] / 2)
        edge = firsts[number] - widths[run[0]] / 2 - gap

    placed = [float(center) for center in centers]
    for run, first in zip(runs, firsts):
        for index, step in zip(run, offsets(run)):
            placed[index] = first + step
    return placed


__all__ = [name for name in dir() if name.isupper()] + [
    "MatrixFonts",
    "apply",
    "caps_labels",
    "draw_hatch",
    "matrix_fonts",
    "spread_labels",
    "theme_mode",
]
