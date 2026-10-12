"""Start Options Matrix Studio, the mini PC program (display, recorder, sharing)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence


def resolve_settings_root(settings_dir: Path | None) -> Path:
    """Use the given folder, or ``%APPDATA%\\OptionsMatrixStudio`` seeded once
    from the original Options Matrix preferences."""
    from .studio_settings import original_app_data_dir, seed_from_original, studio_app_data_dir

    if settings_dir is not None:
        return settings_dir.expanduser().resolve()
    settings_root = studio_app_data_dir()
    seed_from_original(settings_root, original_app_data_dir())
    return settings_root


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Options Matrix Studio (mini PC)")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="use made-up market data instead of Schwab, for trying the setup when the market is closed",
    )
    parser.add_argument(
        "--settings-dir",
        type=Path,
        help="folder for preferences and saved tabs (default: %%APPDATA%%\\OptionsMatrixStudio)",
    )
    args = parser.parse_args(argv)

    try:
        from .app import OptionsMatrixApp
    except ImportError as exc:
        if getattr(exc, "name", "") == "websocket":
            print("Missing dependency `websocket-client`. Run setup.bat in the mini_pc folder.")
            return 1
        raise

    settings_root = resolve_settings_root(args.settings_dir)
    service = None
    if args.demo:
        from .demo_service import DemoMarketDataService

        service = DemoMarketDataService()
    app = OptionsMatrixApp(settings_root=settings_root, market_data_service=service, demo=args.demo)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
