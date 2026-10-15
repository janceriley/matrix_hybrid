"""Load, validate and query the One Desk tokens (``tokens.json``).

Everything visual in the suite comes from this one file. Adapters never hold
their own hex values; they ask for a :class:`Palette` and the other token groups
here.

    >>> from onedesk.tokens import load
    >>> t = load()
    >>> t.palette("light").navy
    '#0d3b70'
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from . import color as _color

MODES: tuple[str, ...] = ("light", "dark")
AUTO = "auto"
COLOR_GROUPS: tuple[str, ...] = ("chrome", "accent", "data", "status")
FORMAT_KEYS: tuple[str, ...] = ("missing", "separator", "ellipsis", "minus", "width_prefix")


class TokenError(ValueError):
    """Raised when ``tokens.json`` is malformed or a token is unknown."""


class Palette(Mapping[str, str]):
    """Every color token for one mode, by key (``p["navy"]``) or attribute (``p.navy``)."""

    __slots__ = ("mode", "_colors")

    def __init__(self, mode: str, colors: Mapping[str, str]) -> None:
        self.mode = mode
        self._colors = dict(colors)

    def __getitem__(self, name: str) -> str:
        try:
            return self._colors[name]
        except KeyError:
            raise KeyError(f"unknown color token: {name!r}") from None

    def __iter__(self) -> Iterator[str]:
        return iter(self._colors)

    def __len__(self) -> int:
        return len(self._colors)

    def __getattr__(self, name: str) -> str:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._colors[name]
        except KeyError:
            raise AttributeError(f"unknown color token: {name!r}") from None

    def resolve(self, value: str) -> str:
        """A token name (``"navy"``) or a literal hex, as ``#rrggbb``."""
        return self._colors[value] if value in self._colors else _color.normalize(value)

    def mix(self, a: str, b: str, t: float) -> str:
        """CSS-style blend of two tokens or hex values; ``t=1`` gives ``a``."""
        return _color.mix(self.resolve(a), self.resolve(b), t)

    def as_dict(self) -> dict[str, str]:
        return dict(self._colors)

    def __repr__(self) -> str:
        return f"Palette({self.mode!r}, {len(self._colors)} colors)"


@dataclass(frozen=True)
class FontSpec:
    """One type role. ``css`` is a fallback stack; ``tk`` is a list of (family, weight) candidates."""

    role: str
    use: str
    css: tuple[str, ...]
    tk: tuple[tuple[str, str], ...]
    weight: int
    stretch: str | None = None
    letter_spacing_em: float | None = None

    def css_stack(self) -> str:
        """The CSS ``font-family`` value, quoting names that need it."""
        generic = {"serif", "sans-serif", "monospace", "system-ui", "ui-monospace", "-apple-system"}
        return ", ".join(name if name in generic or name.replace("-", "").isalnum() else f'"{name}"'
                         for name in self.css)


@dataclass(frozen=True)
class TypeSize:
    px: int
    pt: int
    use: str


class Tokens:
    """Validated, read-only view of ``tokens.json``."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        _validate(data)
        self._data = data
        self.name: str = data["name"]
        self.version: str = data["version"]
        self.modes: tuple[str, ...] = tuple(data["modes"])
        self.color_names: tuple[str, ...] = tuple(data["color"])
        self._palettes = {
            mode: Palette(mode, {name: _color.normalize(spec[mode]) for name, spec in data["color"].items()})
            for mode in MODES
        }
        self._fonts = {
            role: FontSpec(
                role=role,
                use=spec.get("use", ""),
                css=tuple(spec["css"]),
                tk=tuple((c["family"], c["weight"]) for c in spec["tk"]),
                weight=int(spec["weight"]),
                stretch=spec.get("stretch"),
                letter_spacing_em=spec.get("letter_spacing_em"),
            )
            for role, spec in data["font"].items()
        }
        self._type = {role: TypeSize(int(s["px"]), int(s["pt"]), s.get("use", ""))
                      for role, s in data["size"]["type"].items()}

    # -- colors ---------------------------------------------------------------
    def palette(self, mode: str = "light") -> Palette:
        """The palette for ``"light"`` or ``"dark"`` (``"auto"`` follows Windows)."""
        return self._palettes[resolve_mode(mode)]

    def describe(self, name: str) -> dict[str, str]:
        """Group, light and dark values, and the plain-language use of a color token."""
        try:
            spec = self._data["color"][name]
        except KeyError:
            raise TokenError(f"unknown color token: {name!r}") from None
        return {"name": name, "group": spec["group"], "light": spec["light"], "dark": spec["dark"], "use": spec["use"]}

    def group(self, group: str) -> tuple[str, ...]:
        """Color token names in one group (chrome, accent, data, status), in file order."""
        if group not in COLOR_GROUPS:
            raise TokenError(f"unknown color group: {group!r}")
        return tuple(n for n, s in self._data["color"].items() if s["group"] == group)

    # -- chart series --------------------------------------------------------
    def categorical(self, mode: str = "light") -> tuple[str, ...]:
        """Validated categorical slots, in fixed order. Never cycle past the end."""
        return tuple(_color.normalize(c) for c in self._data["series"]["categorical"][resolve_mode(mode)])

    @property
    def categorical_limits(self) -> dict[str, int]:
        cat = self._data["series"]["categorical"]
        return {"adjacent": int(cat["max_adjacent"]), "all_pairs": int(cat["max_all_pairs"])}

    def sequential(self, mode: str = "light") -> tuple[str, ...]:
        """Ten-step one-hue ramp for magnitude; dark mode runs dark to light."""
        ramp = tuple(_color.normalize(c) for c in self._data["series"]["sequential"]["light"])
        return ramp if resolve_mode(mode) == "light" else tuple(reversed(ramp))

    def diverging_mid(self, mode: str = "light") -> str:
        return _color.normalize(self._data["series"]["diverging_mid"][resolve_mode(mode)])

    def diverging(self, mode: str = "light", steps: int = 10) -> tuple[str, ...]:
        """Loss-to-gain scale: ``down`` -> neutral midpoint -> ``up``, equal steps per arm."""
        if steps < 3:
            raise ValueError("a diverging scale needs at least 3 steps")
        p = self.palette(mode)
        mid = self.diverging_mid(mode)
        half = steps // 2
        down_arm = [_color.mix_oklab(p.down, mid, 1 - i / half) for i in range(half)]
        up_arm = [_color.mix_oklab(p.up, mid, 1 - i / half) for i in reversed(range(half))]
        return tuple(down_arm + ([mid] if steps % 2 else []) + up_arm)

    # -- type ------------------------------------------------------------------
    @property
    def font_roles(self) -> tuple[str, ...]:
        return tuple(self._fonts)

    def font(self, role: str) -> FontSpec:
        try:
            return self._fonts[role]
        except KeyError:
            raise TokenError(f"unknown font role: {role!r}") from None

    def type_size(self, role: str) -> TypeSize:
        try:
            return self._type[role]
        except KeyError:
            raise TokenError(f"unknown type size: {role!r}") from None

    @property
    def type_roles(self) -> tuple[str, ...]:
        return tuple(self._type)

    # -- size, shape, glyphs, formats -------------------------------------------
    @property
    def space(self) -> tuple[int, ...]:
        return tuple(self._data["size"]["space"])

    def radius(self, kind: str = "web") -> int:
        return int(self._data["size"]["radius"][kind])

    def rule(self, kind: str = "hairline") -> int:
        return int(self._data["size"]["rule"][kind])

    def height(self, name: str) -> int:
        return int(self._data["size"]["height"][name])

    def row_padding(self, kind: str) -> int:
        return int(self._data["size"]["row_padding"][kind])

    @property
    def rail(self) -> int:
        return int(self._data["size"]["rail"])

    def glyph(self, name: str) -> str:
        try:
            return self._data["glyph"][name]
        except KeyError:
            raise TokenError(f"unknown glyph: {name!r}") from None

    @property
    def glyphs(self) -> dict[str, str]:
        return dict(self._data["glyph"])

    def format_token(self, name: str) -> str:
        return self._data["format"][name]

    def as_json(self) -> str:
        """The token file as a JSON string (for tools outside Python)."""
        return json.dumps(self._data, ensure_ascii=False, indent=2)


def _fail(path: str, problem: str) -> None:
    raise TokenError(f"tokens.json {path}: {problem}")


def _validate(data: Mapping[str, Any]) -> None:
    for key in ("name", "version", "modes", "color", "series", "font", "size", "glyph", "format"):
        if key not in data:
            _fail(key, "missing")
    if tuple(data["modes"]) != MODES:
        _fail("modes", f"must be {list(MODES)}")
    if not data["color"]:
        _fail("color", "empty")
    for name, spec in data["color"].items():
        if spec.get("group") not in COLOR_GROUPS:
            _fail(f"color.{name}.group", f"must be one of {COLOR_GROUPS}")
        for mode in MODES:
            if not _color.is_hex(spec.get(mode)):
                _fail(f"color.{name}.{mode}", f"not a hex color: {spec.get(mode)!r}")
        if not str(spec.get("use", "")).strip():
            _fail(f"color.{name}.use", "describe what the token is for")
    cat = data["series"].get("categorical", {})
    for mode in MODES:
        values = cat.get(mode)
        if not values or not all(_color.is_hex(v) for v in values):
            _fail(f"series.categorical.{mode}", "needs a list of hex colors")
    if len(cat["light"]) != len(cat["dark"]):
        _fail("series.categorical", "light and dark need the same number of slots")
    for limit in ("max_adjacent", "max_all_pairs"):
        if not 1 <= int(cat.get(limit, 0)) <= len(cat["light"]):
            _fail(f"series.categorical.{limit}", "must be between 1 and the number of slots")
    seq = data["series"].get("sequential", {}).get("light", [])
    if len(seq) != 10 or not all(_color.is_hex(v) for v in seq):
        _fail("series.sequential.light", "needs exactly ten hex colors")
    mid = data["series"].get("diverging_mid", {})
    for mode in MODES:
        if not _color.is_hex(mid.get(mode)):
            _fail(f"series.diverging_mid.{mode}", "not a hex color")
    for role, spec in data["font"].items():
        if not spec.get("css") or not all(isinstance(n, str) and n for n in spec["css"]):
            _fail(f"font.{role}.css", "needs a non-empty list of family names")
        if not spec.get("tk"):
            _fail(f"font.{role}.tk", "needs at least one candidate")
        for i, cand in enumerate(spec["tk"]):
            if not cand.get("family") or cand.get("weight") not in ("normal", "bold"):
                _fail(f"font.{role}.tk[{i}]", "needs a family and a weight of 'normal' or 'bold'")
        if not 100 <= int(spec.get("weight", 0)) <= 900:
            _fail(f"font.{role}.weight", "must be 100-900")
    for role, spec in data["size"]["type"].items():
        if int(spec.get("px", 0)) <= 0 or int(spec.get("pt", 0)) <= 0:
            _fail(f"size.type.{role}", "px and pt must be positive")
    for key in FORMAT_KEYS:
        if not data["format"].get(key):
            _fail(f"format.{key}", "missing")
    for name, value in data["glyph"].items():
        if not isinstance(value, str) or not value:
            _fail(f"glyph.{name}", "must be a non-empty string")


@lru_cache(maxsize=None)
def load(path: str | None = None) -> Tokens:
    """Load and validate the tokens. ``path`` overrides the packaged ``tokens.json``."""
    if path is None:
        text = resources.files(__package__).joinpath("tokens.json").read_text(encoding="utf-8")
    else:
        text = Path(path).read_text(encoding="utf-8")
    return Tokens(json.loads(text))


def palette(mode: str = "light") -> Palette:
    """Shortcut for ``load().palette(mode)``."""
    return load().palette(mode)


def system_prefers_dark() -> bool:
    """True when Windows is set to dark mode for apps; False elsewhere or if unknown."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) == 0
    except (OSError, ValueError):
        return False


def resolve_mode(mode: str | None) -> str:
    """``"light"`` / ``"dark"`` unchanged; ``"auto"`` or ``None`` follows the Windows app setting."""
    if mode in MODES:
        return mode  # type: ignore[return-value]
    if mode in (None, AUTO):
        return "dark" if system_prefers_dark() else "light"
    raise TokenError(f"mode must be 'light', 'dark' or 'auto', not {mode!r}")
