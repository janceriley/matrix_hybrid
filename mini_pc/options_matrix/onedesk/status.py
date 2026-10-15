"""The One Desk status grammar: a glyph, a caps word and an optional detail.

Every program shows state the same way, for example ``● LIVE mini PC``,
``⚠ AUTH 2d`` or ``✕ BLOCKED 1``. Healthy tags stay outlined; tags that need
you get a tint. Status colors live in tags, never in the data area.

    >>> from onedesk import status
    >>> status.live("mini PC").text()
    '● LIVE · mini PC'
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from .tokens import Palette
from .tokens import load as _load

_T = _load()
ON: str = _T.glyph("on")                # ●
OFF_GLYPH: str = _T.glyph("off")        # ○
WORKING_GLYPH: str = _T.glyph("working")  # ◐
RETRYING: str = _T.glyph("retrying")    # ↻
ATTENTION: str = _T.glyph("attention")  # ⚠
FAILED: str = _T.glyph("failed")        # ✕
ERROR: str = _T.glyph("error")          # !
SHARING: str = _T.glyph("sharing")      # ⮂ (⇄ read as "=" at 9 pt)
MISSING: str = _T.glyph("missing")      # —
SEP: str = _T.format_token("separator")


class Kind(str, Enum):
    """What a tag means. The kind picks the colors; the glyph can be overridden."""

    OK = "ok"            # live, ready, passed
    WORKING = "working"  # connecting, armed, loading: in progress, nothing to do yet
    WARN = "warn"        # needs you: auth due, needs sync, reconnecting
    BAD = "bad"          # failed, blocked, wrong password, error
    OFF = "off"          # offline, idle, paused, no data
    REC = "rec"          # recording
    HIT = "hit"          # a target was reached (exit buy)
    NEUTRAL = "neutral"  # plain information (1 viewer)
    STALE = "stale"      # data you should not trust
    DEMO = "demo"        # made-up data


DEFAULT_GLYPH: dict[Kind, str | None] = {
    Kind.OK: ON,
    Kind.WORKING: WORKING_GLYPH,
    Kind.WARN: ATTENTION,
    Kind.BAD: FAILED,
    Kind.OFF: OFF_GLYPH,
    Kind.REC: ON,
    Kind.HIT: None,
    Kind.NEUTRAL: None,
    Kind.STALE: None,
    Kind.DEMO: None,
}

# Higher is more urgent; used to summarise many tags into one.
SEVERITY: dict[Kind, int] = {
    Kind.BAD: 90,
    Kind.WARN: 80,
    Kind.HIT: 70,
    Kind.STALE: 60,
    Kind.DEMO: 50,
    Kind.WORKING: 40,
    Kind.OFF: 30,
    Kind.REC: 20,
    Kind.OK: 10,
    Kind.NEUTRAL: 0,
}


@dataclass(frozen=True)
class Status:
    """One status tag.

    ``glyph=None`` uses the kind's default glyph; ``glyph=""`` shows none.
    """

    word: str
    kind: Kind = Kind.NEUTRAL
    detail: str | None = None
    glyph: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", Kind(self.kind))
        if not self.word.strip():
            raise ValueError("a status needs a word")

    @property
    def shown_glyph(self) -> str | None:
        if self.glyph is None:
            return DEFAULT_GLYPH[self.kind]
        return self.glyph or None

    @property
    def label(self) -> str:
        """The status word in caps, as tags show it."""
        return self.word.upper()

    @property
    def needs_attention(self) -> bool:
        return self.kind in (Kind.WARN, Kind.BAD)

    def text(self, sep: str = SEP) -> str:
        """Plain-text form for places without styling: ``● LIVE · mini PC``."""
        head = f"{self.shown_glyph} {self.label}" if self.shown_glyph else self.label
        return f"{head}{sep}{self.detail}" if self.detail else head


@dataclass(frozen=True)
class TagColors:
    """Resolved colors for drawing a tag in one mode."""

    text: str
    glyph: str
    background: str
    border: str
    pattern: str | None = None        # "hatch" | "stripes" | None
    pattern_colors: tuple[str, ...] = ()


def tag_colors(kind: Kind | str, palette: Palette) -> TagColors:
    """Colors for a tag of ``kind`` on ``palette``. Quiet when healthy, tinted when it needs you."""
    p = palette
    k = Kind(kind)
    outline = p.rule_strong
    if k is Kind.OK:
        return TagColors(p.ink, p.ok, p.surface, outline)
    if k is Kind.WORKING:
        return TagColors(p.ink, p.warn, p.surface, outline)
    if k is Kind.WARN:
        return TagColors(p.ink, p.warn, p.warn_tint, p.mix("warn", "surface", 0.45))
    if k is Kind.BAD:
        return TagColors(p.ink, p.bad, p.bad_tint, p.mix("bad", "surface", 0.45))
    if k is Kind.OFF:
        return TagColors(p.muted, p.off, p.surface, outline)
    if k is Kind.REC:
        return TagColors(p.ink, p.rec, p.surface, outline)
    if k is Kind.HIT:
        return TagColors(p.up, p.up, p.up_tint, p.mix("up", "surface", 0.45))
    if k is Kind.STALE:
        return TagColors(p.ink_2, p.ink_2, p.surface, outline, "hatch", (p.hatch,))
    if k is Kind.DEMO:
        return TagColors(p.ink, p.ink, p.surface, p.stripe, "stripes", (p.stripe, p.stripe_2))
    return TagColors(p.ink, p.ink, p.surface, outline)


def worst(statuses: Iterable[Status]) -> Status | None:
    """The most urgent status in ``statuses`` (for summarising into one tag), or ``None``."""
    items = list(statuses)
    return max(items, key=lambda s: SEVERITY[s.kind]) if items else None


# --- the shared vocabulary -------------------------------------------------------
# Words every program uses the same way. App-specific statuses are built with
# Status(...) directly, e.g. Status("390 pace", Kind.OK, "343/day").

def live(detail: str | None = None) -> Status:
    return Status("Live", Kind.OK, detail)


def connecting(detail: str | None = None) -> Status:
    return Status("Connecting", Kind.WORKING, detail)


def reconnecting(detail: str | None = None) -> Status:
    return Status("Reconnecting", Kind.WARN, detail, RETRYING)


def offline(detail: str | None = None) -> Status:
    return Status("Offline", Kind.OFF, detail)


def recording(detail: str | None = None) -> Status:
    return Status("Rec", Kind.REC, detail)


def armed(detail: str | None = None) -> Status:
    return Status("Armed", Kind.WORKING, detail)


def rec_off() -> Status:
    return Status("Rec off", Kind.OFF)


def ready(detail: str | None = None) -> Status:
    return Status("Ready", Kind.OK, detail)


def loading(detail: str | None = None) -> Status:
    return Status("Loading", Kind.WORKING, detail)


def needs(word: str, detail: str | None = None) -> Status:
    """Something needs the user: ``needs("Auth", "2d")`` -> ``⚠ AUTH 2d``."""
    return Status(word, Kind.WARN, detail)


def failed(word: str, detail: str | None = None) -> Status:
    """``failed("Blocked", "1")`` -> ``✕ BLOCKED 1``."""
    return Status(word, Kind.BAD, detail)


def error(word: str, detail: str | None = None) -> Status:
    """``error("Rec error")`` -> ``! REC ERROR``."""
    return Status(word, Kind.BAD, detail, ERROR)


def sharing(viewers: int) -> Status:
    """``⮂ 1 VIEWER`` / ``⮂ 0 VIEWERS`` (an idle share is shown as off)."""
    word = f"{viewers} viewer" if viewers == 1 else f"{viewers} viewers"
    return Status(word, Kind.NEUTRAL if viewers else Kind.OFF, None, SHARING)


def stale(detail: str | None = None) -> Status:
    return Status("Stale", Kind.STALE, detail)


def no_data(detail: str | None = None) -> Status:
    return Status("No data", Kind.OFF, detail, MISSING)


def demo() -> Status:
    return Status("Demo data", Kind.DEMO)


def hit(word: str, detail: str | None = None) -> Status:
    """A reached target: ``hit("Exit buy", "target reached")``."""
    return Status(word, Kind.HIT, detail)
