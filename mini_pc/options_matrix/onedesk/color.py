"""Small, dependency-free color math used by every adapter.

Hex strings are the lingua franca: tokens are stored as ``#rrggbb`` and every
function here accepts ``#rgb`` or ``#rrggbb`` (case-insensitive) and returns
lowercase ``#rrggbb``.
"""

from __future__ import annotations

import re

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def is_hex(value: object) -> bool:
    """True for ``#rgb`` or ``#rrggbb`` strings."""
    return isinstance(value, str) and bool(_HEX.match(value))


def normalize(value: str) -> str:
    """Return ``value`` as lowercase ``#rrggbb``; raise ``ValueError`` otherwise."""
    if not is_hex(value):
        raise ValueError(f"not a hex color: {value!r}")
    digits = value[1:]
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits.lower()


def to_rgb(value: str) -> tuple[int, int, int]:
    """``#rrggbb`` -> ``(r, g, b)`` with 0-255 channels."""
    digits = normalize(value)[1:]
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def to_hex(rgb: tuple[float, float, float]) -> str:
    """``(r, g, b)`` with 0-255 channels (clamped, rounded) -> ``#rrggbb``."""
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def rgba(value: str, alpha: float) -> str:
    """CSS/Plotly ``rgba(r,g,b,a)`` string for a token at the given opacity."""
    r, g, b = to_rgb(value)
    return f"rgba({r},{g},{b},{alpha:g})"


def mix(a: str, b: str, t: float) -> str:
    """Blend like CSS ``color-mix(in srgb, a t*100%, b)``: ``t=1`` gives ``a``."""
    if not 0.0 <= t <= 1.0:
        raise ValueError("t must be between 0 and 1")
    ra, rb = to_rgb(a), to_rgb(b)
    return to_hex(tuple(ca * t + cb * (1.0 - t) for ca, cb in zip(ra, rb)))


# --- luminance and contrast (WCAG 2.x) ------------------------------------

def _to_linear(channel: int) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _to_gamma(channel: float) -> float:
    c = max(0.0, min(1.0, channel))
    c = 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return c * 255.0


def luminance(value: str) -> float:
    """WCAG relative luminance, 0 (black) to 1 (white)."""
    r, g, b = (_to_linear(c) for c in to_rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two colors, 1.0 to 21.0."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# --- OKLab, for perceptually even ramps -------------------------------------

def to_oklab(value: str) -> tuple[float, float, float]:
    r, g, b = (_to_linear(c) for c in to_rgb(value))
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (x ** (1 / 3) if x >= 0 else -((-x) ** (1 / 3)) for x in (l, m, s))
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def from_oklab(lab: tuple[float, float, float]) -> str:
    L, a, b = lab
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    bl = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return to_hex((_to_gamma(r), _to_gamma(g), _to_gamma(bl)))


def mix_oklab(a: str, b: str, t: float) -> str:
    """Perceptual blend in OKLab: ``t=1`` gives ``a``, ``t=0`` gives ``b``."""
    if not 0.0 <= t <= 1.0:
        raise ValueError("t must be between 0 and 1")
    la, lb = to_oklab(a), to_oklab(b)
    return from_oklab(tuple(x * t + y * (1.0 - t) for x, y in zip(la, lb)))


def delta_e(a: str, b: str) -> float:
    """Euclidean distance in OKLab x100 (the data-viz method's Delta E)."""
    la, lb = to_oklab(a), to_oklab(b)
    return 100.0 * sum((x - y) ** 2 for x, y in zip(la, lb)) ** 0.5
