"""Start Options Matrix Viewer, the work PC program."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


def _ensure_shared_package() -> None:
    """Use the mini_pc folder's matrix code when it is not installed yet."""
    try:
        import options_matrix  # noqa: F401
    except ImportError:
        sibling = Path(__file__).resolve().parents[2] / "mini_pc"
        if (sibling / "options_matrix" / "app.py").is_file():
            sys.path.insert(0, str(sibling))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Options Matrix Viewer (work PC)")
    parser.add_argument(
        "--settings-dir",
        type=Path,
        help="folder for connection settings and positions (default: %%APPDATA%%\\OptionsMatrixViewer)",
    )
    args = parser.parse_args(argv)
    _ensure_shared_package()

    from .viewer_app import ViewerApp
    from .viewer_settings import viewer_app_data_dir

    settings_root = (args.settings_dir or viewer_app_data_dir()).expanduser().resolve()
    settings_root.mkdir(parents=True, exist_ok=True)
    app = ViewerApp(settings_root=settings_root)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
