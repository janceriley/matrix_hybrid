# Vendored: One Desk 0.1.0

This folder is a pinned copy of the `onedesk` package from `Dev\Design\onedesk`, the shared look for Options Matrix, Market Synthetic Playback and the USO scalp dashboard. It sits inside `options_matrix` so both PCs get it with the rest of the folder, and the editable installs on both PCs find it without running setup again.

Don't edit it here. Change the source in `Dev\Design`, run its tests, then copy the `.py` files and `tokens.json` back into this folder.

The Matrix uses `onedesk.tk_theme` (ttk styles, the One Desk fonts, the dark title bar and the hatch) through `options_matrix/one_desk.py`. Nothing in here is part of the math fingerprint (`share_protocol.MATH_SOURCE_FILES` covers only `app.py` and `models.py`), so updating this copy never makes the Viewer say UPDATE NEEDED.
