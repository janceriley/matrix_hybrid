"""NiceGUI adapter: Quasar brand colors, CSS variables, AG Grid and Plotly for Playback.

    from nicegui import ui
    from onedesk import nicegui_theme

    nicegui_theme.apply()                                   # before ui.run, once
    ui.run(dark=nicegui_theme.dark_setting("light"))        # "light" | "dark" | "auto"

Inside pages:

    nicegui_theme.status_tag(status.ready("12,345 rows"))
    ui.plotly(nicegui_theme.figure(fig_dict, mode))         # re-render on mode change

How dark mode works: Quasar adds ``body--dark`` to ``<body>``. The stylesheet
puts light ``--od-*`` values on ``:root`` and dark values on ``body.body--dark``,
so every rule written against the variables switches on its own. Quasar brand
colors are written inline on ``<body>`` by NiceGUI, so their dark overrides use
``!important``. Plotly cannot read CSS variables; re-render figures with the
dark template when the mode changes.

Targets NiceGUI 3.x (Quasar 2, AG Grid theming API).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from . import css as _css
from . import plotly_theme as _plotly
from . import status as _status
from .tokens import load as _load
from .tokens import resolve_mode

DARK_SELECTOR = "body.body--dark"


def brand_colors(mode: str = "light") -> dict[str, str]:
    """Keyword arguments for ``app.colors()`` / ``ui.colors()`` in ``mode``.

    ``dark`` and ``dark_page`` are Quasar's dark-mode surfaces and always come from the dark palette.
    """
    p = _load().palette(resolve_mode(mode))
    d = _load().palette("dark")
    return {
        "primary": p.navy,
        "secondary": p.quote,
        "accent": p.model,
        "dark": d.surface,
        "dark_page": d.canvas,
        "positive": p.ok,
        "negative": p.bad,
        "info": p.count,
        "warning": p.warn,
    }


def _quasar_vars(mode: str) -> str:
    colors = brand_colors(mode)
    names = {"dark_page": "dark-page"}
    return "\n".join(f"  --q-{names.get(k, k)}: {v} !important;" for k, v in colors.items())


def quasar_rules() -> str:
    """Rules that bring Quasar components in line with One Desk."""
    return f"""\
{DARK_SELECTOR} {{
{_quasar_vars("dark")}
}}
{DARK_SELECTOR} {{ background: var(--od-canvas); color: var(--od-ink); }}
/* Buttons speak in sentence case; caps are for labels and status words only. */
.q-btn, .q-tab {{ text-transform: none; letter-spacing: 0; font-family: var(--od-font-ui); }}
/* White text on the light navy used in dark mode would be unreadable. Quasar's `.text-white`
   is a layered !important rule that an unlayered !important cannot beat, so color the label itself. */
{DARK_SELECTOR} .q-btn.bg-primary .q-btn__content, {DARK_SELECTOR} .q-chip.bg-primary .q-chip__content {{
  color: var(--od-on-navy); }}
/* Flat panels: hairline border, small radius, no shadow. */
.q-card {{ background: var(--od-surface); border: 1px solid var(--od-rule); border-radius: var(--od-radius);
  box-shadow: none; }}
.q-card--dark {{ border-color: var(--od-rule); }}
.q-menu {{ background: var(--od-surface); border: 1px solid var(--od-rule-strong); box-shadow: none;
  border-radius: var(--od-radius); }}
.q-field--outlined .q-field__control {{ border-radius: var(--od-radius); }}
.q-field--outlined .q-field__control:before {{ border-color: var(--od-rule-strong); }}
.q-field__native, .q-field__input, .q-select__dropdown-icon {{ color: var(--od-ink); }}
.q-separator {{ background: var(--od-rule); }}
.q-tooltip {{ background: var(--od-ink); color: var(--od-surface); font-family: var(--od-font-ui);
  font-size: var(--od-text-caption); border-radius: var(--od-radius); }}
.q-linear-progress {{ color: var(--od-navy); }}
.q-table__card, .q-table {{ color: var(--od-ink); }}
.q-markup-table, .q-table__card {{ border: 1px solid var(--od-rule); box-shadow: none; border-radius: var(--od-radius); }}
.q-badge {{ font-family: var(--od-font-label); font-weight: 600; font-stretch: var(--od-label-stretch);
  letter-spacing: 0.06em; text-transform: uppercase; border-radius: var(--od-radius); }}
"""


def aggrid_rules() -> str:
    """AG Grid theme parameters as CSS variables, pointed at the One Desk tokens."""
    return """\
.nicegui-aggrid, .nicegui-aggrid .ag-root-wrapper {
  --ag-font-family: var(--od-font-ui);
  --ag-font-size: var(--od-text-body);
  --ag-data-font-size: var(--od-text-body);
  --ag-background-color: var(--od-surface);
  --ag-foreground-color: var(--od-ink);
  --ag-text-color: var(--od-ink);
  --ag-cell-text-color: var(--od-ink);
  --ag-header-background-color: var(--od-raised);
  --ag-header-text-color: var(--od-muted);
  --ag-header-font-family: var(--od-font-label);
  --ag-header-font-weight: 600;
  --ag-header-font-size: var(--od-text-caption);
  --ag-border-color: var(--od-rule);
  --ag-row-border-color: var(--od-rule);
  --ag-header-column-border: none;
  --ag-odd-row-background-color: var(--od-surface);
  --ag-row-hover-color: var(--od-raised);
  --ag-selected-row-background-color: var(--od-navy-tint);
  --ag-accent-color: var(--od-navy);
  --ag-checkbox-checked-background-color: var(--od-navy);
  --ag-checkbox-checked-border-color: var(--od-navy);
  --ag-input-focus-border: 1px solid var(--od-navy);
  --ag-wrapper-border-radius: var(--od-radius);
  --ag-border-radius: var(--od-radius);
}
.nicegui-aggrid .ag-header-cell-text { text-transform: uppercase; letter-spacing: var(--od-label-tracking);
  font-stretch: var(--od-label-stretch); }
.nicegui-aggrid .ag-right-aligned-cell, .nicegui-aggrid .od-num { font-family: var(--od-font-num);
  font-variant-numeric: tabular-nums; }
"""


def stylesheet() -> str:
    """Everything Playback needs: variables for both modes, base rules, components, Quasar and AG Grid."""
    return (_css.stylesheet({"light": ":root", "dark": DARK_SELECTOR}, base=True, components=True)
            + quasar_rules() + aggrid_rules())


def dark_setting(mode: str = "light") -> bool | None:
    """Value for ``ui.run(dark=...)`` or ``ui.dark_mode(...)``: False, True, or None for auto."""
    if mode == "auto":
        return None
    return resolve_mode(mode) == "dark"


def apply(*, shared: bool = True) -> None:
    """Register the brand colors and the stylesheet for every page. Call once before ``ui.run``."""
    from nicegui import app, ui

    app.colors(**brand_colors("light"))
    ui.add_css(stylesheet(), shared=shared)


def figure(fig: dict[str, Any], mode: str = "light", **config: Any) -> dict[str, Any]:
    """A copy of a Plotly figure dict with the One Desk template and config, ready for ``ui.plotly``."""
    out = _plotly.apply(fig, mode)
    out["config"] = {**_plotly.config(), **out.get("config", {}), **config}
    return out


def status_tag(item: _status.Status) -> Any:
    """Add one status tag to the current NiceGUI container and return the element."""
    from nicegui import ui

    return ui.html(_css.status_tag_html(item), tag="span")


def status_tags(items: Iterable[_status.Status]) -> Any:
    """Add a row of status tags."""
    from nicegui import ui

    with ui.row().classes("items-center gap-1") as row:
        for item in items:
            status_tag(item)
    return row
