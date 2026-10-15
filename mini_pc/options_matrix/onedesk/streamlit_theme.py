"""Streamlit adapter: theme config, CSS variables and the Plotly template for Order Parser.

Three pieces, used together:

1. ``config_toml()`` writes the ``[theme]`` tables for ``.streamlit/config.toml``
   (light and dark sections, faces, radius, chart colors). Regenerate it with
   ``python -m onedesk.streamlit_theme --write .streamlit/config.toml``.
2. ``inject()`` adds the ``--od-*`` variables and component classes for the mode
   Streamlit is currently showing, and registers the Plotly template.
3. ``plotly_chart()`` draws a figure with the One Desk template. Plain
   ``st.plotly_chart`` calls need ``theme=None`` so Streamlit's own chart theme
   does not override the template.

Targets Streamlit 1.60 option names. Only ``[theme]`` accepts the chart color
lists and ``showSidebarBorder``; colors, faces and radius may differ per mode.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from . import css as _css
from . import plotly_theme as _plotly
from . import status as _status
from .tokens import load as _load
from .tokens import resolve_mode


def _stack(role: str) -> str:
    # Streamlit takes a comma-separated list of family names as one string.
    return ", ".join(_load().font(role).css)


def theme_colors(mode: str = "light") -> dict[str, str]:
    """Color options for one mode (valid in ``[theme]``, ``[theme.light]`` and ``[theme.dark]``)."""
    p = _load().palette(resolve_mode(mode))
    return {
        "primaryColor": p.navy,
        "backgroundColor": p.canvas,
        "secondaryBackgroundColor": p.surface,
        "textColor": p.ink,
        "linkColor": p.navy,
        "codeTextColor": p.ink,
        "codeBackgroundColor": p.sunken,
        "borderColor": p.rule_strong,
        "dataframeBorderColor": p.rule,
        "dataframeHeaderBackgroundColor": p.raised,
        # Streamlit's "basic palette" drives st.success/st.warning/st.error/st.info and colored text.
        "redColor": p.bad,
        "redBackgroundColor": p.bad_tint,
        "redTextColor": p.bad,
        "greenColor": p.ok,
        "greenBackgroundColor": p.up_tint,
        "greenTextColor": p.ok,
        "orangeColor": p.warn,
        "orangeBackgroundColor": p.warn_tint,
        "orangeTextColor": p.warn,
        "yellowColor": p.stripe,
        "yellowBackgroundColor": p.warn_tint,
        "yellowTextColor": p.warn,
        "blueColor": p.count,
        "blueBackgroundColor": p.navy_tint,
        "blueTextColor": p.navy,
        "violetColor": p.model,
        "violetBackgroundColor": p.model_tint,
        "violetTextColor": p.model,
        "grayColor": p.muted,
        "grayBackgroundColor": p.sunken,
        "grayTextColor": p.muted,
    }


def sidebar_colors(mode: str = "light") -> dict[str, str]:
    p = _load().palette(resolve_mode(mode))
    return {"backgroundColor": p.raised, "secondaryBackgroundColor": p.surface}


def theme_shared() -> dict[str, Any]:
    """Options that are the same in both modes (faces, shape, chart colors)."""
    t = _load()
    return {
        "font": _stack("ui"),
        "headingFont": _stack("display"),
        "codeFont": _stack("num"),
        "baseRadius": f"{t.radius('web')}px",
        "buttonRadius": f"{t.radius('web')}px",
        "showWidgetBorder": True,
        "showSidebarBorder": True,
        "chartCategoricalColors": list(t.categorical("light")),
        "chartSequentialColors": list(t.sequential("light")),
        "chartDivergingColors": list(t.diverging("light", steps=10)),
    }


def config(modes: Iterable[str] = ("light", "dark")) -> dict[str, dict[str, Any]]:
    """Theme tables keyed by TOML table name (``"theme"``, ``"theme.light"``, ...).

    One mode gives a single-mode ``[theme]`` with ``base`` set; both modes give
    ``[theme.light]`` and ``[theme.dark]`` so Streamlit can follow the viewer's setting.
    """
    chosen = [resolve_mode(m) for m in modes]
    if not chosen:
        raise ValueError("pick at least one mode")
    if len(set(chosen)) == 1:
        mode = chosen[0]
        return {
            "theme": {"base": mode, **theme_colors(mode), **theme_shared()},
            "theme.sidebar": sidebar_colors(mode),
        }
    tables: dict[str, dict[str, Any]] = {"theme": theme_shared()}
    for mode in ("light", "dark"):
        tables[f"theme.{mode}"] = theme_colors(mode)
        tables[f"theme.{mode}.sidebar"] = sidebar_colors(mode)
    return tables


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)  # JSON string escapes are valid TOML basic-string escapes
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def config_toml(modes: Iterable[str] = ("light", "dark"), *, header: bool = True) -> str:
    """The theme tables as TOML text for ``.streamlit/config.toml``."""
    t = _load()
    lines: list[str] = []
    if header:
        lines += [f"# One Desk {t.version} theme, generated by onedesk.streamlit_theme. Do not edit by hand;",
                  "# regenerate with: python -m onedesk.streamlit_theme --write .streamlit/config.toml", ""]
    for table, options in config(modes).items():
        lines.append(f"[{table}]")
        lines += [f"{key} = {_toml_value(value)}" for key, value in options.items()]
        lines.append("")
    return "\n".join(lines)


def write_config(path: str | Path, modes: Iterable[str] = ("light", "dark"), *,
                 extra: Mapping[str, Mapping[str, Any]] | None = None) -> Path:
    """Write ``config_toml()`` (plus any ``extra`` tables, e.g. ``{"server": {...}}``) to ``path``."""
    target = Path(path)
    text = config_toml(modes)
    for table, options in (extra or {}).items():
        text += f"[{table}]\n" + "".join(f"{k} = {_toml_value(v)}\n" for k, v in options.items()) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def streamlit_rules() -> str:
    """Fixes for Streamlit widgets that the theme options cannot reach."""
    return """\
/* Streamlit writes white text on primary buttons; dark mode's light navy needs dark text. */
button[kind^="primary"], button[kind^="primary"] * { color: var(--od-on-navy) !important; }
"""


def stylesheet(mode: str = "light") -> str:
    """Variables for one mode, the component classes and the widget fixes.

    Streamlit's own theme (``config_toml``) handles the page itself.
    """
    return _css.stylesheet({resolve_mode(mode): ":root"}, base=False) + streamlit_rules()


def current_mode(default: str = "light") -> str:
    """The mode Streamlit is showing (``st.context.theme.type``), or ``default`` outside a run."""
    try:
        import streamlit as st

        kind = st.context.theme.type
        return kind if kind in ("light", "dark") else default
    except Exception:  # not running under `streamlit run`, or an older Streamlit
        return default


def inject(mode: str | None = None) -> str:
    """Add the One Desk CSS for ``mode`` (default: Streamlit's current mode) and register Plotly.

    Call once near the top of the script, on every run. Returns the mode used.
    """
    import streamlit as st

    chosen = resolve_mode(mode) if mode else current_mode()
    st.markdown(f"<style>\n{stylesheet(chosen)}</style>", unsafe_allow_html=True)
    try:
        _plotly.register(chosen)
    except ImportError:
        pass
    return chosen


def status_tags_html(items: Iterable[_status.Status]) -> str:
    return " ".join(_css.status_tag_html(item) for item in items)


def status_tags(*items: _status.Status) -> None:
    """Show one or more status tags on one line."""
    import streamlit as st

    st.markdown(status_tags_html(items), unsafe_allow_html=True)


def plotly_chart(figure: Any, mode: str | None = None, **kwargs: Any) -> Any:
    """``st.plotly_chart`` with the One Desk template, ``theme=None`` and the shared config."""
    import streamlit as st

    chosen = resolve_mode(mode) if mode else current_mode()
    kwargs.setdefault("config", _plotly.config())
    return st.plotly_chart(_plotly.apply(figure, chosen), theme=None, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print or write the One Desk Streamlit theme.")
    parser.add_argument("--mode", choices=["both", "light", "dark"], default="both",
                        help="both (default) writes [theme.light] and [theme.dark]")
    parser.add_argument("--write", metavar="PATH", help="write to PATH instead of printing")
    args = parser.parse_args(argv)
    modes = ("light", "dark") if args.mode == "both" else (args.mode,)
    if args.write:
        print(f"wrote {write_config(args.write, modes)}")
    else:
        sys.stdout.write(config_toml(modes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
