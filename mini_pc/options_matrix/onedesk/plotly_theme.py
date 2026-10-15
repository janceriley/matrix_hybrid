"""One Plotly template for Order Parser (Python Plotly) and Playback (Plotly.js dicts).

The template is a plain ``dict``, so it can be registered with ``plotly.io``
or dropped straight into a figure dict as ``layout.template``.

Chart rules carried here:

* Numbers on axes and in tooltips use the mono face; titles and legends use the UI face.
* Gridlines are hairlines in ``rule``; the plot is transparent so it sits on its panel.
* Categorical colors come in a fixed order and are never cycled (see ``tokens.json``).
* Green and red mean money direction only; use :func:`roles` for semantic traces.
* Bars keep square corners, matching what the Tk programs can draw.
"""

from __future__ import annotations

import copy
from typing import Any

from . import color as _color
from .tokens import load as _load
from .tokens import resolve_mode

TEMPLATE_NAMES: dict[str, str] = {"light": "onedesk_light", "dark": "onedesk_dark"}


def sequential_scale(mode: str = "light") -> list[list[Any]]:
    """Plotly colorscale for magnitude (one hue)."""
    ramp = _load().sequential(mode)
    return [[i / (len(ramp) - 1), c] for i, c in enumerate(ramp)]


def diverging_scale(mode: str = "light") -> list[list[Any]]:
    """Plotly colorscale for loss (low) through neutral to gain (high)."""
    ramp = _load().diverging(mode, steps=11)
    return [[i / (len(ramp) - 1), c] for i, c in enumerate(ramp)]


def roles(mode: str = "light") -> dict[str, str]:
    """Semantic trace colors, so no program picks its own hex for these jobs."""
    p = _load().palette(resolve_mode(mode))
    return {
        "mid": p.ink,                    # synthetic / market mid
        "fair_value": p.model,
        "model_band": _color.rgba(p.model, 0.12),
        "lower_endpoint": p.up,          # buy threshold
        "upper_endpoint": p.down,        # sell threshold
        "buy": p.up,
        "sell": p.down,
        "gain": p.up,
        "loss": p.down,
        "fees": p.down,
        "bid": p.quote,
        "ask": p.quote_2,
        "itm": p.itm,
        "count": p.count,                # neutral quantities and rates
        "reference": p.ink_2,            # dashed target / average lines
        "playhead": p.navy,              # a control, so it wears the accent
        "missing": p.hatch,
    }


def template(mode: str = "light", *, transparent: bool = True) -> dict[str, Any]:
    """The One Desk template for ``mode`` as a plain dict."""
    t = _load()
    m = resolve_mode(mode)
    p = t.palette(m)
    ui = t.font("ui").css_stack()
    num = t.font("num").css_stack()
    display = t.font("display").css_stack()
    body = t.type_size("body").px
    caption = t.type_size("caption").px
    title = t.type_size("title").px
    background = "rgba(0,0,0,0)" if transparent else p.surface

    axis = {
        "gridcolor": p.rule,
        "gridwidth": 1,
        "linecolor": p.rule_strong,
        "linewidth": 1,
        "zerolinecolor": p.rule_strong,
        "zerolinewidth": 1,
        "tickcolor": p.rule_strong,
        "ticklen": 4,
        "tickfont": {"family": num, "size": caption, "color": p.muted},
        "title": {"font": {"family": ui, "size": caption, "color": p.muted}, "standoff": 8},
        "automargin": True,
    }
    colorbar = {"outlinewidth": 0, "tickfont": {"family": num, "size": caption, "color": p.muted}}
    layout: dict[str, Any] = {
        "font": {"family": ui, "size": body, "color": p.ink},
        "title": {"font": {"family": display, "size": title, "color": p.ink}, "x": 0, "xanchor": "left",
                  "xref": "paper"},
        "paper_bgcolor": background,
        "plot_bgcolor": background,
        "colorway": list(t.categorical(m)),
        "colorscale": {"sequential": sequential_scale(m), "diverging": diverging_scale(m)},
        "margin": {"l": 48, "r": 16, "t": 28, "b": 40, "pad": 0},
        "hoverlabel": {"bgcolor": p.surface, "bordercolor": p.rule_strong, "align": "left",
                       "font": {"family": num, "size": body, "color": p.ink}},
        "legend": {"font": {"family": ui, "size": caption, "color": p.ink_2}, "bgcolor": "rgba(0,0,0,0)",
                   "orientation": "h", "x": 0, "xanchor": "left", "y": 1.02, "yanchor": "bottom"},
        "xaxis": {**axis, "showline": True, "showgrid": False, "zeroline": False},
        "yaxis": {**axis, "showline": False, "showgrid": True, "zeroline": True},
        "modebar": {"bgcolor": "rgba(0,0,0,0)", "color": p.muted, "activecolor": p.navy},
        "hovermode": "closest",
        "bargap": 0.28,
        "separators": ".,",
        "annotationdefaults": {"font": {"family": ui, "size": caption, "color": p.ink_2}, "arrowcolor": p.muted},
        "shapedefaults": {"line": {"color": p.rule_strong, "width": 1}},
        "coloraxis": {"colorbar": colorbar},
    }
    data: dict[str, Any] = {
        "scatter": [{"line": {"width": 2}, "marker": {"size": 8, "line": {"width": 0}}}],
        "bar": [{"marker": {"line": {"width": 0}}}],
        "heatmap": [{"colorbar": colorbar}],
    }
    return {"layout": layout, "data": data}


def config(**overrides: Any) -> dict[str, Any]:
    """Plotly ``config``: no logo, toolbar on hover, 2x PNG export. Overrides win."""
    base: dict[str, Any] = {
        "displaylogo": False,
        "displayModeBar": "hover",
        "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d", "toggleSpikelines"],
        "toImageButtonOptions": {"format": "png", "scale": 2},
        "responsive": True,
    }
    base.update(overrides)
    return base


def apply(figure: Any, mode: str = "light", *, transparent: bool = True) -> Any:
    """Put the template on a figure.

    A ``dict`` figure (Playback) is copied and returned with ``layout.template``
    set; an object with ``update_layout`` (a ``plotly.graph_objects.Figure``) is
    updated in place and returned.
    """
    tpl = template(mode, transparent=transparent)
    if isinstance(figure, dict):
        fig = copy.deepcopy(figure)
        fig.setdefault("layout", {})["template"] = tpl
        return fig
    figure.update_layout(template=tpl)
    return figure


def empty_figure(message: str, hint: str | None = None, mode: str = "light") -> dict[str, Any]:
    """A figure dict for "nothing to show yet" states: hidden axes and a centered message."""
    p = _load().palette(resolve_mode(mode))
    annotations = [{
        "text": message, "xref": "paper", "yref": "paper", "x": 0.5, "y": 0.56 if hint else 0.5,
        "showarrow": False, "font": {"size": 13, "color": p.muted},
    }]
    if hint:
        annotations.append({
            "text": hint, "xref": "paper", "yref": "paper", "x": 0.5, "y": 0.42,
            "showarrow": False, "font": {"size": 11, "color": p.faint},
        })
    return {
        "data": [],
        "layout": {
            "template": template(mode),
            "xaxis": {"visible": False},
            "yaxis": {"visible": False},
            "annotations": annotations,
        },
    }


def register(mode: str = "light", *, set_default: bool = True) -> str:
    """Register both templates with ``plotly.io`` and return the name for ``mode``.

    Streamlit apps should then call ``st.plotly_chart(fig, theme=None)`` so
    Streamlit's own chart theme does not override the template.
    """
    import plotly.io as pio  # optional dependency

    for name_mode, name in TEMPLATE_NAMES.items():
        pio.templates[name] = template(name_mode)
    chosen = TEMPLATE_NAMES[resolve_mode(mode)]
    if set_default:
        pio.templates.default = chosen
    return chosen
