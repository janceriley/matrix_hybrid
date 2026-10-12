"""Work PC settings: which mini PC to connect to, and its password.

Saved as ``viewer.json`` in ``%APPDATA%\\OptionsMatrixViewer`` together with
this PC's positions. The password is stored as plain text in your Windows
profile folder; it only unlocks viewing the shared matrix.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

VIEWER_SETTINGS_FILENAME = "viewer.json"
VIEWER_SETTINGS_VERSION = 1
DEFAULT_PORT = 47210


@dataclass(frozen=True)
class ViewerSettings:
    host: str = ""
    port: int = DEFAULT_PORT
    password: str = ""
    connect_at_startup: bool = True

    @property
    def complete(self) -> bool:
        return bool(self.host.strip()) and bool(self.password) and 1 <= self.port <= 65535


def viewer_app_data_dir(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    appdata = (env.get("APPDATA") or "").strip()
    if appdata:
        return Path(appdata) / "OptionsMatrixViewer"
    return (home or Path.home()) / ".options_matrix_viewer"


def viewer_settings_from_mapping(mapping: Mapping[str, Any]) -> ViewerSettings:
    defaults = ViewerSettings()
    host = mapping.get("host")
    port = mapping.get("port")
    password = mapping.get("password")
    connect = mapping.get("connect_at_startup")
    return ViewerSettings(
        host=host.strip() if isinstance(host, str) else defaults.host,
        port=port if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535 else defaults.port,
        password=password if isinstance(password, str) else defaults.password,
        connect_at_startup=connect if isinstance(connect, bool) else defaults.connect_at_startup,
    )


def load_viewer_settings(path: Path) -> ViewerSettings:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return ViewerSettings()
    if not isinstance(payload, Mapping) or payload.get("version") != VIEWER_SETTINGS_VERSION:
        return ViewerSettings()
    return viewer_settings_from_mapping(payload)


def save_viewer_settings(settings: ViewerSettings, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"version": VIEWER_SETTINGS_VERSION, **asdict(settings)}, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
