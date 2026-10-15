"""CSS for the two web-based programs (Streamlit and NiceGUI).

Everything is expressed as ``--od-*`` custom properties, so one stylesheet can
carry both modes: light values on one selector, dark values on another, and
component classes that only ever reference variables.

    >>> from onedesk import css
    >>> sheet = css.stylesheet({"light": ":root", "dark": "body.body--dark"})
"""

from __future__ import annotations

from collections.abc import Mapping
from html import escape

from . import status as _status
from .tokens import load as _load
from .tokens import resolve_mode

PREFIX = "od"


def _var(name: str) -> str:
    return f"--{PREFIX}-{name.replace('_', '-')}"


def variables(mode: str = "light") -> dict[str, str]:
    """Every ``--od-*`` custom property for ``mode``, as a name -> value dict."""
    t = _load()
    p = t.palette(resolve_mode(mode))
    out: dict[str, str] = {_var(name): value for name, value in p.items()}

    for role in t.font_roles:
        out[_var(f"font_{role}")] = t.font(role).css_stack()
    label = t.font("label")
    out[_var("label_stretch")] = label.stretch or "normal"
    out[_var("label_tracking")] = f"{label.letter_spacing_em or 0}em"

    for role in t.type_roles:
        out[_var(f"text_{role}")] = f"{t.type_size(role).px}px"
    for i, step in enumerate(t.space, start=1):
        out[_var(f"space_{i}")] = f"{step}px"
    for name in ("app_bar", "control_strip", "status_bar", "matrix_row", "table_row"):
        out[_var(f"h_{name}")] = f"{t.height(name)}px"
    out[_var("rail")] = f"{t.rail}px"
    out[_var("radius")] = f"{t.radius('web')}px"
    out[_var("rule_width")] = f"{t.rule('hairline')}px"

    for i, value in enumerate(t.categorical(mode), start=1):
        out[_var(f"series_{i}")] = value

    for kind in _status.Kind:
        c = _status.tag_colors(kind, p)
        out[_var(f"tag_{kind.value}_text")] = c.text
        out[_var(f"tag_{kind.value}_glyph")] = c.glyph
        out[_var(f"tag_{kind.value}_bg")] = c.background
        out[_var(f"tag_{kind.value}_border")] = c.border
    return out


def declarations(mode: str = "light", *, indent: str = "  ", important: bool = False) -> str:
    """The variables for ``mode`` as CSS declarations, one per line."""
    bang = " !important" if important else ""
    body = "\n".join(f"{indent}{k}: {v}{bang};" for k, v in variables(mode).items())
    scheme = "dark" if resolve_mode(mode) == "dark" else "light"
    return f"{body}\n{indent}color-scheme: {scheme};"


_HATCH = "repeating-linear-gradient(135deg, var(--od-hatch) 0 1px, transparent 1px 6px)"
_STRIPES = "repeating-linear-gradient(135deg, var(--od-stripe) 0 7px, var(--od-stripe-2) 7px 14px)"


def base_rules() -> str:
    """Page-level rules: background, text color and the three faces."""
    return """\
body { background: var(--od-canvas); color: var(--od-ink); font-family: var(--od-font-ui); }
input, textarea, select, button { font-family: inherit; }
"""


def component_rules() -> str:
    """Utility classes and the status tag, all driven by variables."""
    tag_kinds = "\n".join(
        f".od-tag--{k.value} {{ color: var(--od-tag-{k.value}-text); background-color: var(--od-tag-{k.value}-bg);"
        f" border-color: var(--od-tag-{k.value}-border); }}\n"
        f".od-tag--{k.value} .od-tag__glyph {{ color: var(--od-tag-{k.value}-glyph); }}"
        for k in _status.Kind
    )
    return f"""\
.od-num {{ font-family: var(--od-font-num); font-variant-numeric: tabular-nums; }}
.od-num-strong {{ font-family: var(--od-font-num-strong); font-weight: 600; font-variant-numeric: tabular-nums; }}
.od-label {{ font-family: var(--od-font-label); font-weight: 600; font-stretch: var(--od-label-stretch);
  letter-spacing: var(--od-label-tracking); text-transform: uppercase; font-size: var(--od-text-caption);
  color: var(--od-muted); }}
.od-readout {{ font-family: var(--od-font-label); font-weight: 600; font-stretch: var(--od-label-stretch);
  font-size: var(--od-text-readout); line-height: 1.1; font-variant-numeric: tabular-nums; }}
.od-title {{ font-family: var(--od-font-display); font-weight: 600; font-size: var(--od-text-title); }}
.od-ink2 {{ color: var(--od-ink-2); }}
.od-muted {{ color: var(--od-muted); }}
.od-faint {{ color: var(--od-faint); }}
.od-up {{ color: var(--od-up); }}
.od-down {{ color: var(--od-down); }}
.od-count {{ color: var(--od-count); }}
.od-model {{ color: var(--od-model); }}
.od-panel {{ background: var(--od-surface); border: var(--od-rule-width) solid var(--od-rule);
  border-radius: var(--od-radius); }}
.od-hatch {{ background-image: {_HATCH}; }}
.od-stripes {{ background-image: {_STRIPES}; }}
.od-tag {{ display: inline-flex; align-items: center; gap: 5px; height: 22px; padding: 0 7px;
  border: 1px solid var(--od-rule-strong); border-radius: var(--od-radius); background-color: var(--od-surface);
  color: var(--od-ink); font-family: var(--od-font-label); font-weight: 600; font-stretch: var(--od-label-stretch);
  font-size: 11px; letter-spacing: 0.07em; text-transform: uppercase; white-space: nowrap; line-height: 1;
  vertical-align: middle; box-sizing: border-box; }}
.od-tag__glyph {{ font-family: var(--od-font-ui); letter-spacing: 0; font-weight: 600; }}
.od-tag__detail {{ font-family: var(--od-font-num); text-transform: none; letter-spacing: 0; font-weight: 400;
  color: var(--od-muted); }}
{tag_kinds}
.od-tag--stale {{ background-image: repeating-linear-gradient(135deg, var(--od-hatch) 0 1px, transparent 1px 5px); }}
.od-tag--demo {{ background-image: repeating-linear-gradient(135deg, var(--od-stripe) 0 6px, var(--od-stripe-2) 6px 12px); }}
.od-tag--demo .od-tag__word {{ background: var(--od-surface); padding: 2px 5px; border-radius: 2px; }}
{frame_rules()}"""


def frame_rules() -> str:
    """The shared frame: app bar on top, optional control strip, status bar at the bottom."""
    return """\
.od-app-bar { background: var(--od-surface); color: var(--od-ink); border-bottom: 1px solid var(--od-rule);
  min-height: var(--od-h-app-bar); }
.od-strip { background: var(--od-raised); color: var(--od-ink); border-bottom: 1px solid var(--od-rule);
  min-height: var(--od-h-control-strip); }
.od-status-bar { background: var(--od-surface); color: var(--od-ink-2); border-top: 1px solid var(--od-rule);
  min-height: var(--od-h-status-bar); font-size: var(--od-text-caption); }
.od-mark { width: 24px; height: 24px; border-radius: var(--od-radius); background: var(--od-navy);
  color: var(--od-on-navy); display: inline-grid; place-items: center; flex: none; }
.od-mark svg { width: 16px; height: 16px; fill: currentColor; }
.od-app-name { font-family: var(--od-font-label); font-weight: 600; font-stretch: var(--od-label-stretch);
  letter-spacing: var(--od-label-tracking); text-transform: uppercase; font-size: var(--od-text-control);
  color: var(--od-ink); white-space: nowrap; }
.od-context { color: var(--od-ink-2); border-left: 1px solid var(--od-rule); padding-left: 10px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.od-ellipsis { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.od-note-warn { background: var(--od-warn-tint); border: 1px solid var(--od-tag-warn-border); color: var(--od-ink);
  border-radius: var(--od-radius); padding: 6px 10px; }
"""


def stylesheet(modes: Mapping[str, str] | None = None, *, base: bool = True, components: bool = True,
               important_selectors: tuple[str, ...] = ()) -> str:
    """A complete stylesheet.

    ``modes`` maps a mode to the selector that carries its variables, e.g.
    ``{"light": ":root", "dark": "body.body--dark"}``. Selectors listed in
    ``important_selectors`` get ``!important`` declarations (needed where a
    framework writes inline variables).
    """
    modes = modes or {"light": ":root"}
    blocks = []
    for mode, selector in modes.items():
        important = selector in important_selectors
        blocks.append(f"{selector} {{\n{declarations(mode, important=important)}\n}}")
    if base:
        blocks.append(base_rules())
    if components:
        blocks.append(component_rules())
    return "\n".join(blocks) + "\n"


def status_tag_html(item: _status.Status) -> str:
    """HTML for one status tag; style it with :func:`stylesheet`."""
    glyph = item.shown_glyph
    parts = [f'<span class="od-tag od-tag--{item.kind.value}">']
    if glyph:
        parts.append(f'<span class="od-tag__glyph" aria-hidden="true">{escape(glyph)}</span>')
    parts.append(f'<span class="od-tag__word">{escape(item.word)}</span>')
    if item.detail:
        parts.append(f'<span class="od-tag__detail">{escape(item.detail)}</span>')
    parts.append("</span>")
    return "".join(parts)
