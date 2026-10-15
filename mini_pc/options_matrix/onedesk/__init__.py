"""One Desk: the shared look for Options Matrix Studio and Viewer, Playback and Order Parser.

Core modules have no third-party dependencies:

* :mod:`onedesk.tokens`  - the validated token file (colors for light and dark, faces, sizes)
* :mod:`onedesk.color`   - color math (mixing, contrast, OKLab)
* :mod:`onedesk.fmt`     - one way to write money, prices, strikes, widths, dates and times
* :mod:`onedesk.status`  - the status grammar (glyph, caps word, detail)
* :mod:`onedesk.css`     - CSS variables and component classes for the web programs
* :mod:`onedesk.plotly_theme` - the shared Plotly template (plain dicts)

Toolkit adapters import their toolkit lazily:

* :mod:`onedesk.tk_theme`        - Tkinter (Studio and Viewer)
* :mod:`onedesk.streamlit_theme` - Streamlit (Order Parser)
* :mod:`onedesk.nicegui_theme`   - NiceGUI (Playback)
"""

from __future__ import annotations

from .tokens import MODES, Palette, TokenError, Tokens, load, palette, resolve_mode, system_prefers_dark

__version__ = load().version

__all__ = [
    "MODES",
    "Palette",
    "TokenError",
    "Tokens",
    "__version__",
    "load",
    "palette",
    "resolve_mode",
    "system_prefers_dark",
]
