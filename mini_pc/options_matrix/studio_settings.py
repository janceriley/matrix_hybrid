"""Mini PC settings: where recordings go, autopilot, and sharing with viewers.

These live in ``studio.json`` beside the program's other preferences (by
default ``%APPDATA%\\OptionsMatrixStudio``). The sharing password is created
once and kept until you replace it from the Sharing window.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Mapping

STUDIO_SETTINGS_FILENAME = "studio.json"
STUDIO_SETTINGS_VERSION = 1
DEFAULT_SHARE_PORT = 47210
MIN_SHARE_PORT = 1024
MAX_SHARE_PORT = 65535
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 64

# Letters and digits that are hard to confuse when typed from the screen.
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# Preference files carried over from the original Options Matrix once, so the
# mini PC program starts with the same fair-value settings and saved tabs.
_SEED_FILENAMES = ("settings.json", "matrix_cache.json", "width_preferences.json")


def generate_share_password() -> str:
    """Return a new random password such as ``K7QF-29XM-PA4D-HN8R`` (80 bits)."""
    groups = (
        "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(4))
        for _ in range(4)
    )
    return "-".join(groups)


def valid_share_password(value: object) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return (
        stripped == value
        and MIN_PASSWORD_LENGTH <= len(value) <= MAX_PASSWORD_LENGTH
        and value.isprintable()
    )


def valid_share_port(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MIN_SHARE_PORT <= value <= MAX_SHARE_PORT
    )


@dataclass(frozen=True)
class StudioSettings:
    recording_folder: str = ""
    record_every_trading_day: bool = False
    connect_at_startup: bool = False
    reload_tabs_after_connect: bool = True
    share_enabled: bool = True
    share_port: int = DEFAULT_SHARE_PORT
    share_password: str = ""


def studio_app_data_dir(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    appdata = (env.get("APPDATA") or "").strip()
    if appdata:
        return Path(appdata) / "OptionsMatrixStudio"
    return (home or Path.home()) / ".options_matrix_studio"


def original_app_data_dir(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Where the original Options Matrix keeps its preferences."""
    env = os.environ if environ is None else environ
    appdata = (env.get("APPDATA") or "").strip()
    if appdata:
        return Path(appdata) / "OptionsMatrix"
    return home or Path.home()


def seed_from_original(settings_root: Path, original_root: Path) -> list[str]:
    """Copy the original program's preferences once; never overwrite."""
    copied: list[str] = []
    settings_root.mkdir(parents=True, exist_ok=True)
    for name in _SEED_FILENAMES:
        source = original_root / name
        destination = settings_root / name
        if destination.exists() or not source.is_file():
            continue
        try:
            shutil.copyfile(source, destination)
        except OSError:
            continue
        copied.append(name)
    return copied


def studio_settings_from_mapping(mapping: Mapping[str, Any]) -> StudioSettings:
    defaults = StudioSettings()
    values: dict[str, Any] = {}
    for field in fields(StudioSettings):
        raw = mapping.get(field.name, getattr(defaults, field.name))
        default = getattr(defaults, field.name)
        if isinstance(default, bool):
            values[field.name] = raw if isinstance(raw, bool) else default
        elif field.name == "share_port":
            values[field.name] = raw if valid_share_port(raw) else default
        elif field.name == "share_password":
            values[field.name] = raw if valid_share_password(raw) else ""
        else:
            values[field.name] = raw.strip() if isinstance(raw, str) else default
    return StudioSettings(**values)


def load_studio_settings(path: Path) -> StudioSettings:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return StudioSettings()
    if not isinstance(payload, Mapping) or payload.get("version") != STUDIO_SETTINGS_VERSION:
        return StudioSettings()
    return studio_settings_from_mapping(payload)


def save_studio_settings(settings: StudioSettings, path: Path) -> None:
    payload: dict[str, Any] = {"version": STUDIO_SETTINGS_VERSION, **asdict(settings)}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
