"""Launch Options Matrix Studio (mini PC) quietly with this folder's own Python."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Sequence


def _child_environment(venv_dir: Path) -> dict[str, str]:
    """Build an isolated environment without changing the calling process."""
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["VIRTUAL_ENV"] = str(venv_dir)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"

    scripts_dir = str(venv_dir / "Scripts")
    current_path = environment.get("PATH", "")
    environment["PATH"] = (
        scripts_dir
        if not current_path
        else scripts_dir + os.pathsep + current_path
    )
    return environment


def _parse_arguments(arguments: Sequence[str]) -> tuple[bool, list[str]]:
    parser = argparse.ArgumentParser(
        description=(
            "Launch Options Matrix in an isolated process. The default detached "
            "mode returns the calling terminal immediately."
        )
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="keep the app attached to this terminal and return its exit code",
    )
    parsed, app_arguments = parser.parse_known_args(list(arguments))
    return bool(parsed.foreground), app_arguments


def _report_error(project_root: Path, message: str, details: str | None = None) -> None:
    """Report launcher failures in a terminal or a GUI-safe message box."""
    full_message = message if not details else f"{message}\n\n{details.rstrip()}"
    error_path = project_root / "logs" / "launcher-error.log"
    try:
        error_path.parent.mkdir(parents=True, exist_ok=True)
        error_path.write_text(full_message + "\n", encoding="utf-8")
    except OSError:
        error_path = None

    if sys.stderr is not None:
        print(full_message, file=sys.stderr)
        return

    display_message = message
    if error_path is not None:
        display_message += f"\n\nDetails were written to:\n{error_path}"
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, display_message, "Options Matrix", 0x10)
    except (AttributeError, OSError):
        pass


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    venv_dir = project_root / ".venv"
    venv_python = venv_dir / "Scripts" / "python.exe"
    venv_pythonw = venv_dir / "Scripts" / "pythonw.exe"

    foreground, app_arguments = _parse_arguments(sys.argv[1:])

    if not venv_dir.is_dir():
        _report_error(
            project_root,
            "Options Matrix cannot start because its project virtual environment "
            f"does not exist:\n  {venv_dir}\n"
            "Double-click setup.bat in the mini_pc folder, "
            "then start the program again.",
        )
        return 1

    app_python = venv_python if foreground else venv_pythonw
    if not app_python.is_file():
        interpreter_kind = "console" if foreground else "GUI"
        _report_error(
            project_root,
            "Options Matrix cannot start because the virtual-environment "
            f"{interpreter_kind} Python executable is missing:\n  {app_python}",
        )
        return 1

    command = [
        str(app_python),
        "-u",
        "-m",
        "options_matrix",
        *app_arguments,
    ]
    environment = _child_environment(venv_dir)

    if foreground:
        try:
            completed = subprocess.run(
                command,
                cwd=project_root,
                env=environment,
                shell=False,
                check=False,
            )
        except OSError:
            _report_error(
                project_root,
                "Options Matrix failed to start.",
                traceback.format_exc(),
            )
            return 1
        return int(completed.returncode)

    logs_dir = project_root / "logs"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    log_path = logs_dir / f"mini-pc-{timestamp}-{os.getpid()}.log"
    creation_flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    breakaway_flag = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)

    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab", buffering=0) as log_handle:
            popen_arguments = {
                "cwd": project_root,
                "env": environment,
                "stdin": subprocess.DEVNULL,
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "shell": False,
                "close_fds": True,
            }
            try:
                subprocess.Popen(
                    command,
                    creationflags=creation_flags | breakaway_flag,
                    **popen_arguments,
                )
            except OSError as exc:
                if not breakaway_flag or getattr(exc, "winerror", None) != 5:
                    raise
                subprocess.Popen(
                    command,
                    creationflags=creation_flags,
                    **popen_arguments,
                )
    except OSError:
        _report_error(
            project_root,
            "Options Matrix failed to start.",
            traceback.format_exc(),
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
