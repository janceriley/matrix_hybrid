"""Tkinter adapter: One Desk colors, faces and widget styles for Options Matrix Studio and Viewer.

    import tkinter as tk
    from onedesk import tk_theme

    root = tk.Tk()
    theme = tk_theme.apply(root, mode="light")      # or "dark" / "auto"
    ttk.Button(root, text="Load matrix", style="Primary.TButton")
    ttk.Label(root, text="STRIKE", style="Caps.TLabel")
    theme.on_mode_change(redraw_matrix)               # canvases redraw from theme.palette

Notes for the Matrix:

* Keep this module (or a vendored copy) outside ``app.py`` and ``models.py``.
  The Viewer fingerprints those two files, so styling inside them would make it
  show "UPDATE NEEDED" until both PCs match.
* ttk's native "vista" look ignores custom colors, so the theme switches ttk to
  "clam" and styles every widget class from the palette.
* Classic ``tk`` widgets and canvas items keep whatever colors the app passes.
  New widgets get palette defaults through the option database, and
  :meth:`TkTheme.set_mode` recolors existing ones whose colors are tokens.
* Keep a reference to the returned theme (it also pins itself to ``root``);
  its named fonts are deleted when it is garbage-collected.
"""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
import weakref
from collections.abc import Callable, Iterable
from tkinter import ttk

from . import status as _status
from .tokens import FontSpec, Palette, Tokens, resolve_mode
from .tokens import load as _load

# role -> (named font, font spec role, type size role)
FONTS: dict[str, tuple[str, str, str]] = {
    "ui": ("OneDeskUI", "ui", "body"),
    "ui_small": ("OneDeskUISmall", "ui", "caption"),
    "ui_strong": ("OneDeskUIStrong", "ui_strong", "body"),
    "control": ("OneDeskControl", "ui", "control"),
    "title": ("OneDeskTitle", "display", "title"),
    "heading": ("OneDeskHeading", "display", "heading"),
    "label": ("OneDeskLabel", "label", "caption"),
    "label_large": ("OneDeskLabelLarge", "label", "control"),
    "num": ("OneDeskNum", "num", "body"),
    "num_small": ("OneDeskNumSmall", "num", "caption"),
    "num_strong": ("OneDeskNumStrong", "num_strong", "body"),
    "readout": ("OneDeskReadout", "label", "readout"),
}

# Tk's own named fonts -> (font spec role, type size role)
TK_DEFAULT_FONTS: dict[str, tuple[str, str]] = {
    "TkDefaultFont": ("ui", "body"),
    "TkTextFont": ("ui", "body"),
    "TkMenuFont": ("ui", "body"),
    "TkHeadingFont": ("ui_strong", "body"),
    "TkCaptionFont": ("display", "title"),
    "TkSmallCaptionFont": ("ui", "caption"),
    "TkIconFont": ("ui", "body"),
    "TkTooltipFont": ("ui", "caption"),
    "TkFixedFont": ("num", "body"),
}

# Label styles: role -> (named-font role, palette color)
_LABEL_ROLES: dict[str, tuple[str, str]] = {
    "AppName": ("label_large", "ink"),
    "Caps": ("label", "muted"),
    "Muted": ("ui", "muted"),
    "Ink2": ("ui", "ink_2"),
    "Status": ("ui_small", "ink_2"),
    "Num": ("num", "ink"),
    "NumStrong": ("num_strong", "ink"),
    "Up": ("num_strong", "up"),
    "Down": ("num_strong", "down"),
    "Model": ("num", "model"),
    "Title": ("title", "ink"),
    "Heading": ("heading", "ink"),
    "Readout": ("readout", "ink"),
    "ReadoutUp": ("readout", "up"),
    "ReadoutDown": ("readout", "down"),
}

# Surface prefixes for labels, frames and check/radio buttons placed on panels.
_SURFACES: dict[str, str] = {"": "canvas", "Surface.": "surface", "Raised.": "raised", "Sunken.": "sunken"}

_COLOR_OPTIONS = (
    "background", "foreground", "activebackground", "activeforeground", "disabledforeground",
    "highlightbackground", "highlightcolor", "selectbackground", "selectforeground",
    "insertbackground", "troughcolor", "readonlybackground", "selectcolor", "disabledbackground",
)
_ITEM_COLOR_OPTIONS = ("fill", "outline", "activefill", "activeoutline", "disabledfill", "disabledoutline")

_OPTION_PRIORITY = "startupFile"  # below the default ("interactive"), so app option_add calls win


def available_families(root: tk.Misc) -> dict[str, str]:
    """Installed font families for ``root``'s Tk, keyed by lowercase name."""
    return {name.lower(): name for name in tkfont.families(root)}


def resolve_font(root: tk.Misc, spec: FontSpec, families: dict[str, str] | None = None) -> tuple[str, str]:
    """First installed (family, weight) candidate for ``spec``; Tk's default family as a last resort."""
    families = families if families is not None else available_families(root)
    for family, weight in spec.tk:
        if family.lower() in families:
            return families[family.lower()], weight
    fallback = tkfont.nametofont("TkDefaultFont", root=root).actual("family")
    return fallback, spec.tk[-1][1]


def enable_dpi_awareness() -> bool:
    """Make this process DPI-aware on Windows so Tk draws crisp text on scaled displays.

    Call before creating ``tk.Tk()``. Fonts then scale with the display, so any
    geometry in fixed pixels must come from font metrics (see :meth:`TkTheme.row_height`).
    Returns True if Windows accepted the request.
    """
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        return ctypes.WinDLL("shcore").SetProcessDpiAwareness(1) == 0  # system DPI aware
    except (OSError, AttributeError):
        try:
            return bool(ctypes.WinDLL("user32").SetProcessDPIAware())
        except (OSError, AttributeError):
            return False


def set_dark_title_bar(window: tk.Misc, dark: bool) -> bool:
    """Ask Windows 10/11 to draw ``window``'s title bar dark (or light). Returns True on success.

    The window must already be mapped; :class:`TkTheme` calls this on the first ``<Map>``.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        dwmapi = ctypes.WinDLL("dwmapi")
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos.restype = wintypes.BOOL
        dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

        hwnd = user32.GetParent(window.winfo_id())
        if not hwnd:
            return False
        value = ctypes.c_int(1 if dark else 0)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (Windows 10 2004+ / 11); 19 on older Windows 10 builds.
        for attribute in (20, 19):
            if dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                swp_framechanged = 0x0020
                flags = 0x0001 | 0x0002 | 0x0004 | swp_framechanged  # no size, no move, no z-order
                user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, flags)
                return True
        return False
    except (OSError, AttributeError, tk.TclError):
        return False


def color_map(old: Palette, new: Palette) -> dict[str, str]:
    """Old-mode hex -> new-mode hex for every token. Where two tokens share a hex,
    the one declared first in ``tokens.json`` wins (``ink`` over ``stripe_2``)."""
    mapping: dict[str, str] = {}
    for name in old:
        mapping.setdefault(old[name], new[name])
    return mapping


def _hex(widget: tk.Misc, value: str, cache: dict[str, str | None]) -> str | None:
    if value in cache:
        return cache[value]
    result: str | None = None
    if value:
        try:
            r, g, b = widget.winfo_rgb(value)
            result = f"#{r >> 8:02x}{g >> 8:02x}{b >> 8:02x}"
        except tk.TclError:
            result = None
    cache[value] = result
    return result


def _descendants(widget: tk.Misc) -> Iterable[tk.Misc]:
    yield widget
    for child in widget.winfo_children():
        yield from _descendants(child)


def recolor(widget: tk.Misc, mapping: dict[str, str]) -> int:
    """Swap token colors on classic Tk widgets and canvas items under ``widget``.

    ttk widgets are skipped (their styles already changed). Returns the number
    of options changed. Colors that are not tokens are left alone.
    """
    cache: dict[str, str | None] = {}
    changed = 0
    for w in _descendants(widget):
        if isinstance(w, ttk.Widget):
            continue
        try:
            keys = set(w.keys())
        except tk.TclError:
            continue
        for option in _COLOR_OPTIONS:
            if option not in keys:
                continue
            current = _hex(w, str(w.cget(option)), cache)
            if current in mapping and mapping[current] != current:
                w.configure({option: mapping[current]})
                changed += 1
        if isinstance(w, tk.Canvas):
            for item in w.find_all():
                for option in _ITEM_COLOR_OPTIONS:
                    try:
                        raw = w.itemcget(item, option)
                    except tk.TclError:
                        continue
                    current = _hex(w, raw, cache) if raw else None
                    if current in mapping and mapping[current] != current:
                        w.itemconfigure(item, {option: mapping[current]})
                        changed += 1
    return changed


def draw_hatch(canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float, *, color: str,
               spacing: float = 6, width: float = 1, tags: str | tuple[str, ...] = ()) -> list[int]:
    """Diagonal "/" hatch lines clipped to a rectangle: the sign for untrusted data."""
    ids: list[int] = []
    c = x0 + y0 + spacing
    while c < x1 + y1:
        xa, xb = max(x0, c - y1), min(x1, c - y0)
        if xa < xb:
            ids.append(canvas.create_line(xa, c - xa, xb, c - xb, fill=color, width=width, tags=tags))
        c += spacing
    return ids


def _clip(poly: list[tuple[float, float]], x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    """Sutherland-Hodgman clip of a polygon to an axis-aligned rectangle."""
    def clip_edge(points, inside, intersect):
        out = []
        for i, cur in enumerate(points):
            prev = points[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(intersect(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(intersect(prev, cur))
        return out

    def at_x(x):
        return lambda p, q: (x, p[1] + (q[1] - p[1]) * (x - p[0]) / (q[0] - p[0]))

    def at_y(y):
        return lambda p, q: (p[0] + (q[0] - p[0]) * (y - p[1]) / (q[1] - p[1]), y)

    for inside, intersect in ((lambda p: p[0] >= x0, at_x(x0)), (lambda p: p[0] <= x1, at_x(x1)),
                              (lambda p: p[1] >= y0, at_y(y0)), (lambda p: p[1] <= y1, at_y(y1))):
        poly = clip_edge(poly, inside, intersect)
        if not poly:
            break
    return poly


def draw_stripes(canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float, *,
                 colors: tuple[str, str], band: float = 7, tags: str | tuple[str, ...] = ()) -> list[int]:
    """Alternating "/" bands clipped to a rectangle: the sign for demo data."""
    ids = [canvas.create_rectangle(x0, y0, x1, y1, fill=colors[1], outline="", tags=tags)]
    c = x0 + y0 - band
    index = 0
    while c < x1 + y1:
        if index % 2 == 0:
            band_poly = [(c - y1, y1), (c + band - y1, y1), (c + band - y0, y0), (c - y0, y0)]
            clipped = _clip(band_poly, x0, y0, x1, y1)
            if len(clipped) >= 3:
                flat = [v for point in clipped for v in point]
                ids.append(canvas.create_polygon(*flat, fill=colors[0], outline="", tags=tags))
        c += band
        index += 1
    return ids


class TkTheme:
    """The One Desk look applied to one Tk root. Create it with :func:`apply`."""

    def __init__(self, root: tk.Tk, mode: str = "light", *, set_default_fonts: bool = True,
                 option_db: bool = True, title_bar: bool = True) -> None:
        self.root = root
        self.tokens: Tokens = _load()
        self.mode = resolve_mode(mode)
        self.palette: Palette = self.tokens.palette(self.mode)
        self.style = ttk.Style(root)
        self._option_db = option_db
        self._title_bar = title_bar
        self._title_bar_applied = False
        self._listeners: list[Callable[[], Callable[..., None] | None]] = []
        self.fonts: dict[str, tkfont.Font] = {}
        self.families: dict[str, tuple[str, str]] = {}
        self._create_fonts()
        if set_default_fonts:
            self._configure_default_fonts()
        self._apply_palette()
        if title_bar:
            root.bind("<Map>", self._on_map, add="+")
        root._onedesk_theme = self  # type: ignore[attr-defined]  # tie font lifetime to the root

    # -- fonts ---------------------------------------------------------------------
    def font_name(self, role: str) -> str:
        """Named font for a role (``"num"`` -> ``"OneDeskNum"``), usable in any ``font=`` option."""
        return FONTS[role][0]

    def _create_fonts(self) -> None:
        families = available_families(self.root)
        for role, (name, spec_role, size_role) in FONTS.items():
            family, weight = resolve_font(self.root, self.tokens.font(spec_role), families)
            size = self.tokens.type_size(size_role).pt
            try:
                font = tkfont.Font(root=self.root, name=name, family=family, size=size, weight=weight, exists=False)
            except tk.TclError:  # already created by an earlier theme in this interpreter
                font = tkfont.Font(root=self.root, name=name, exists=True)
                font.configure(family=family, size=size, weight=weight)
            self.fonts[role] = font
            self.families[role] = (family, weight)

    def _configure_default_fonts(self) -> None:
        families = available_families(self.root)
        for name, (spec_role, size_role) in TK_DEFAULT_FONTS.items():
            family, weight = resolve_font(self.root, self.tokens.font(spec_role), families)
            tkfont.nametofont(name, root=self.root).configure(
                family=family, size=self.tokens.type_size(size_role).pt, weight=weight)

    def row_height(self, kind: str = "table") -> int:
        """Row height in pixels from the number face's line height, so it scales with DPI.

        ``kind`` is ``"table"`` or ``"matrix"`` (26 and 24 px at 100% scaling with Cascadia Mono 9 pt).
        """
        return int(self.fonts["num"].metrics("linespace")) + self.tokens.row_padding(kind)

    # -- palette -------------------------------------------------------------------
    def _apply_palette(self) -> None:
        self.style.theme_use("clam")
        self._configure_styles()
        if self._option_db:
            self._configure_option_db()
        try:
            self.root.configure(background=self.palette.canvas)
        except tk.TclError:
            pass
        if self._title_bar and self.root.winfo_ismapped():
            self._title_bar_applied = set_dark_title_bar(self.root, self.mode == "dark")

    def _on_map(self, event: tk.Event) -> None:
        if event.widget is self.root and not self._title_bar_applied:
            self._title_bar_applied = True
            set_dark_title_bar(self.root, self.mode == "dark")

    def set_mode(self, mode: str, *, recolor_widgets: bool = True) -> bool:
        """Switch light/dark live. Returns True if the mode changed.

        ttk widgets restyle immediately; classic widgets and canvas items whose
        colors are tokens are recolored; listeners registered with
        :meth:`on_mode_change` run last so they can redraw.
        """
        new_mode = resolve_mode(mode)
        if new_mode == self.mode:
            return False
        old = self.palette
        self.mode = new_mode
        self.palette = self.tokens.palette(new_mode)
        self._apply_palette()
        if recolor_widgets:
            recolor(self.root, color_map(old, self.palette))
        self._notify()
        return True

    def toggle(self) -> str:
        """Flip between light and dark; returns the new mode."""
        self.set_mode("dark" if self.mode == "light" else "light")
        return self.mode

    def on_mode_change(self, callback: Callable[..., None]) -> None:
        """Call ``callback(theme)`` after every mode change. Bound methods are held weakly."""
        if hasattr(callback, "__self__") and hasattr(callback, "__func__"):
            self._listeners.append(weakref.WeakMethod(callback))  # type: ignore[arg-type]
        else:
            self._listeners.append(lambda cb=callback: cb)

    def _notify(self) -> None:
        alive = []
        for ref in self._listeners:
            callback = ref()
            if callback is None:
                continue
            try:
                callback(self)
            except tk.TclError:  # widget destroyed
                continue
            alive.append(ref)
        self._listeners = alive

    # -- widgets --------------------------------------------------------------------
    def tag(self, master: tk.Misc, item: _status.Status) -> StatusTag:
        """A :class:`StatusTag` bound to this theme."""
        return StatusTag(master, self, item)

    def _configure_option_db(self) -> None:
        p, r, pri = self.palette, self.root, _OPTION_PRIORITY
        entries = {
            "*Background": p.canvas,
            "*Foreground": p.ink,
            "*Font": self.font_name("ui"),
            "*activeBackground": p.raised,
            "*activeForeground": p.ink,
            "*selectBackground": p.navy_tint,
            "*selectForeground": p.ink,
            "*insertBackground": p.ink,
            "*highlightColor": p.focus,
            "*highlightBackground": p.canvas,
            "*disabledForeground": p.faint,
            "*troughColor": p.sunken,
            "*readonlyBackground": p.raised,
            "*selectColor": p.surface,
            "*Canvas.Background": p.surface,
            "*Canvas.highlightThickness": 0,
            "*Text.Background": p.surface,
            "*Entry.Background": p.surface,
            "*Listbox.Background": p.surface,
            "*Menu.Background": p.surface,
            "*Menu.activeBackground": p.navy_tint,
            "*Menu.activeForeground": p.ink,
            "*Menu.relief": "flat",
            "*TCombobox*Listbox.Background": p.surface,
            "*TCombobox*Listbox.Foreground": p.ink,
            "*TCombobox*Listbox.selectBackground": p.navy_tint,
            "*TCombobox*Listbox.selectForeground": p.ink,
            "*TCombobox*Listbox.Font": self.font_name("control"),
        }
        for pattern, value in entries.items():
            r.option_add(pattern, value, pri)

    def _configure_styles(self) -> None:
        s, p = self.style, self.palette
        f = {role: name for role, (name, _, _) in FONTS.items()}
        row = self.row_height("table")

        s.configure(".", background=p.canvas, foreground=p.ink, bordercolor=p.rule_strong,
                    lightcolor=p.canvas, darkcolor=p.canvas, troughcolor=p.sunken,
                    selectbackground=p.navy_tint, selectforeground=p.ink, fieldbackground=p.surface,
                    insertcolor=p.ink, arrowcolor=p.muted, focuscolor=p.focus, font=f["ui"], relief="flat")
        s.map(".", background=[("disabled", p.canvas)], foreground=[("disabled", p.faint)],
              selectbackground=[("!focus", p.navy_tint)], selectforeground=[("!focus", p.ink)])

        # Frames: canvas by default; named surfaces for the shared frame zones.
        s.configure("TFrame", background=p.canvas)
        for name, token in (("Surface", "surface"), ("Raised", "raised"), ("Sunken", "sunken"),
                            ("AppBar", "surface"), ("Strip", "raised"), ("StatusBar", "surface")):
            s.configure(f"{name}.TFrame", background=p[token])
        s.configure("Panel.TFrame", background=p.surface, relief="solid", borderwidth=1,
                    bordercolor=p.rule, lightcolor=p.surface, darkcolor=p.surface)

        # Labels: one base per surface, then role styles such as "Caps.Raised.TLabel".
        s.configure("TLabel", background=p.canvas, foreground=p.ink, font=f["ui"])
        for prefix, token in _SURFACES.items():
            base = f"{prefix}TLabel"
            if prefix:
                s.configure(base, background=p[token])
            for role, (font_role, color) in _LABEL_ROLES.items():
                s.configure(f"{role}.{base}", font=f[font_role], foreground=p[color])

        # Buttons: quiet default, navy primary, borderless ghost. Sentence-case labels.
        # Clam draws the 1 px outline only for "raised"; matching bevel colors keep it flat.
        s.configure("TButton", background=p.surface, foreground=p.ink, bordercolor=p.rule_strong,
                    lightcolor=p.surface, darkcolor=p.surface, focuscolor=p.focus, padding=(10, 3),
                    relief="raised", font=f["control"], anchor="center", width=0)
        s.map("TButton",
              background=[("disabled", p.raised), ("pressed", p.sunken), ("active", p.raised)],
              foreground=[("disabled", p.faint)],
              bordercolor=[("disabled", p.rule), ("focus", p.navy)],
              lightcolor=[("pressed", p.sunken), ("active", p.raised)],
              darkcolor=[("pressed", p.sunken), ("active", p.raised)])
        s.configure("Primary.TButton", background=p.navy, foreground=p.on_navy, bordercolor=p.navy,
                    lightcolor=p.navy, darkcolor=p.navy)
        s.map("Primary.TButton",
              background=[("disabled", p.rule), ("pressed", p.navy_hover), ("active", p.navy_hover)],
              foreground=[("disabled", p.surface)],
              bordercolor=[("disabled", p.rule), ("focus", p.focus)],
              lightcolor=[("pressed", p.navy_hover), ("active", p.navy_hover)],
              darkcolor=[("pressed", p.navy_hover), ("active", p.navy_hover)])
        s.configure("Ghost.TButton", background=p.surface, bordercolor=p.surface, lightcolor=p.surface,
                    darkcolor=p.surface)
        s.map("Ghost.TButton",
              background=[("pressed", p.sunken), ("active", p.raised)],
              bordercolor=[("focus", p.navy), ("active", p.rule_strong)])
        s.configure("TMenubutton", background=p.surface, foreground=p.ink, bordercolor=p.rule_strong,
                    lightcolor=p.surface, darkcolor=p.surface, arrowcolor=p.muted, padding=(10, 3),
                    relief="raised", font=f["control"], width=0)
        s.map("TMenubutton", background=[("pressed", p.sunken), ("active", p.raised)],
              bordercolor=[("focus", p.navy)])

        # Inputs.
        for cls in ("TEntry", "TCombobox", "TSpinbox"):
            s.configure(cls, fieldbackground=p.surface, background=p.surface, foreground=p.ink,
                        bordercolor=p.rule_strong, lightcolor=p.surface, darkcolor=p.surface,
                        insertcolor=p.ink, arrowcolor=p.muted, padding=(6, 3),
                        selectbackground=p.navy_tint, selectforeground=p.ink)
        s.map("TEntry", fieldbackground=[("readonly", p.raised), ("disabled", p.sunken)],
              foreground=[("disabled", p.faint)], bordercolor=[("focus", p.navy)],
              lightcolor=[("focus", p.surface)], darkcolor=[("focus", p.surface)])
        s.map("TCombobox",
              fieldbackground=[("readonly", "focus", p.surface), ("readonly", p.surface), ("disabled", p.sunken)],
              foreground=[("readonly", "focus", p.ink), ("disabled", p.faint)],
              selectbackground=[("readonly", p.surface)], selectforeground=[("readonly", p.ink)],
              background=[("active", p.raised), ("pressed", p.raised)],
              bordercolor=[("focus", p.navy)], arrowcolor=[("disabled", p.faint)],
              lightcolor=[("focus", p.surface)], darkcolor=[("focus", p.surface)])
        s.map("TSpinbox", fieldbackground=[("readonly", p.raised), ("disabled", p.sunken)],
              foreground=[("disabled", p.faint)], bordercolor=[("focus", p.navy)],
              arrowcolor=[("disabled", p.faint)], background=[("active", p.raised)])

        # Check and radio buttons, with variants for panels ("Surface.TCheckbutton", ...).
        for cls in ("TCheckbutton", "TRadiobutton"):
            s.configure(cls, background=p.canvas, foreground=p.ink, indicatorbackground=p.surface,
                        indicatorforeground=p.on_navy, upperbordercolor=p.rule_strong,
                        lowerbordercolor=p.rule_strong, indicatormargin=(0, 0, 6, 0), padding=2,
                        font=f["ui"], focuscolor=p.focus)
            s.map(cls, background=[("active", p.canvas)], foreground=[("disabled", p.faint)],
                  indicatorbackground=[("disabled", p.sunken), ("selected", p.navy), ("pressed", p.raised)],
                  indicatorforeground=[("selected", p.on_navy)],
                  upperbordercolor=[("selected", p.navy)], lowerbordercolor=[("selected", p.navy)])
            for prefix, token in _SURFACES.items():
                if prefix:
                    s.configure(f"{prefix}{cls}", background=p[token])
                    s.map(f"{prefix}{cls}", background=[("active", p[token])])

        # Tabs: the selected tab joins the surface below it.
        s.configure("TNotebook", background=p.canvas, bordercolor=p.rule, lightcolor=p.canvas,
                    darkcolor=p.canvas, tabmargins=(0, 4, 0, 0))
        s.configure("TNotebook.Tab", background=p.raised, foreground=p.ink_2, bordercolor=p.rule,
                    lightcolor=p.raised, darkcolor=p.raised, padding=(12, 5), font=f["control"])
        s.map("TNotebook.Tab",
              background=[("selected", p.surface), ("active", p.sunken)],
              foreground=[("selected", p.ink), ("disabled", p.faint)],
              lightcolor=[("selected", p.surface)], darkcolor=[("selected", p.surface)],
              padding=[("selected", (12, 5))], expand=[("selected", (0, 0, 0, 0))])

        # Tables: numbers in mono by default; "Text.Treeview" for prose-heavy tables.
        s.configure("Treeview", background=p.surface, fieldbackground=p.surface, foreground=p.ink,
                    bordercolor=p.rule, lightcolor=p.surface, darkcolor=p.surface, rowheight=row,
                    font=f["num"], relief="flat", borderwidth=1)
        s.map("Treeview", background=[("selected", p.navy_tint), ("disabled", p.sunken)],
              foreground=[("selected", p.ink), ("disabled", p.faint)])
        s.configure("Treeview.Heading", background=p.raised, foreground=p.muted, bordercolor=p.rule,
                    lightcolor=p.raised, darkcolor=p.raised, relief="flat", font=f["label"], padding=(8, 5))
        s.map("Treeview.Heading", background=[("pressed", p.sunken), ("active", p.sunken)],
              foreground=[("active", p.ink)], lightcolor=[("active", p.sunken)], darkcolor=[("active", p.sunken)])
        s.configure("Text.Treeview", font=f["ui"])

        # Everything else.
        s.configure("TScrollbar", background=p.raised, troughcolor=p.canvas, bordercolor=p.rule,
                    lightcolor=p.raised, darkcolor=p.raised, arrowcolor=p.muted, gripcount=0, arrowsize=12)
        s.map("TScrollbar", background=[("pressed", p.rule_strong), ("active", p.sunken)],
              arrowcolor=[("disabled", p.faint)])
        s.configure("TLabelframe", background=p.canvas, bordercolor=p.rule, lightcolor=p.canvas,
                    darkcolor=p.canvas, relief="solid", borderwidth=1)
        s.configure("TLabelframe.Label", background=p.canvas, foreground=p.muted, font=f["label"])
        s.configure("TSeparator", background=p.rule)
        s.configure("TProgressbar", background=p.navy, troughcolor=p.sunken, bordercolor=p.rule,
                    lightcolor=p.navy, darkcolor=p.navy, thickness=6)
        s.configure("TScale", background=p.surface, troughcolor=p.sunken, bordercolor=p.rule_strong,
                    lightcolor=p.surface, darkcolor=p.surface)
        s.map("TScale", background=[("active", p.raised)])
        s.configure("TPanedwindow", background=p.canvas)
        s.configure("Sash", sashthickness=4, gripcount=0, background=p.rule)


class StatusTag(tk.Frame):
    """A status tag (glyph, caps word, detail) that follows the theme's mode.

    Tk cannot paint gradients, so the stale tag uses a sunken fill instead of a
    hatch and the demo tag is a solid amber chip; use :func:`draw_hatch` and
    :func:`draw_stripes` on canvases for the full patterns.
    """

    def __init__(self, master: tk.Misc, theme: TkTheme, item: _status.Status) -> None:
        super().__init__(master, highlightthickness=1, bd=0)
        self.theme = theme
        self._glyph = tk.Label(self, bd=0, padx=0, pady=0, font=theme.font_name("ui_small"))
        self._word = tk.Label(self, bd=0, padx=0, pady=0, font=theme.font_name("label"))
        self._detail = tk.Label(self, bd=0, padx=0, pady=0, font=theme.font_name("num_small"))
        self.status = item
        theme.on_mode_change(self._on_mode_change)
        self.set(item)

    def _on_mode_change(self, _theme: TkTheme) -> None:
        if self.winfo_exists():
            self.set(self.status)

    def colors(self) -> _status.TagColors:
        """The colors this tag draws with, after Tk's pattern fallbacks."""
        c = _status.tag_colors(self.status.kind, self.theme.palette)
        p = self.theme.palette
        if self.status.kind is _status.Kind.STALE:
            return _status.TagColors(c.text, c.glyph, p.sunken, c.border)
        if self.status.kind is _status.Kind.DEMO:
            return _status.TagColors(p.stripe_2, p.stripe_2, p.stripe, p.stripe)
        return c

    def set(self, item: _status.Status) -> None:
        """Show a different status."""
        self.status = item
        c = self.colors()
        self.configure(background=c.background, highlightbackground=c.border, highlightcolor=c.border)
        for label in (self._glyph, self._word, self._detail):
            label.pack_forget()
            label.configure(background=c.background)
        shown = []
        if item.shown_glyph:
            self._glyph.configure(text=item.shown_glyph, foreground=c.glyph)
            shown.append(self._glyph)
        self._word.configure(text=item.label, foreground=c.text)
        shown.append(self._word)
        if item.detail:
            detail_fg = c.text if item.kind in (_status.Kind.HIT, _status.Kind.DEMO) else self.theme.palette.muted
            self._detail.configure(text=item.detail, foreground=detail_fg)
            shown.append(self._detail)
        for i, label in enumerate(shown):
            left = 7 if i == 0 else 5
            right = 7 if i == len(shown) - 1 else 0
            label.pack(side="left", padx=(left, right), pady=3)


def apply(root: tk.Tk, mode: str = "light", **kwargs: bool) -> TkTheme:
    """Apply One Desk to ``root`` and return the :class:`TkTheme` (keep a reference)."""
    return TkTheme(root, mode, **kwargs)
