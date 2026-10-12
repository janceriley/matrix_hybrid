from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from options_matrix import run_app


class RunAppTests(unittest.TestCase):
    @staticmethod
    def _project_root(temp_dir: str, *, include_python: bool = True) -> Path:
        project_root = Path(temp_dir)
        scripts_dir = project_root / ".venv" / "Scripts"
        scripts_dir.mkdir(parents=True)
        if include_python:
            (scripts_dir / "python.exe").touch()
            (scripts_dir / "pythonw.exe").touch()
        return project_root

    def test_missing_virtual_environment_reports_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stderr = io.StringIO()
            with (
                patch.object(
                    run_app,
                    "__file__",
                    str(Path(temp_dir) / "options_matrix" / "run_app.py"),
                ),
                patch.object(run_app.sys, "argv", ["run_app.py"]),
                patch.object(run_app.subprocess, "Popen") as popen,
                redirect_stderr(stderr),
            ):
                result = run_app.main()

        self.assertEqual(result, 1)
        self.assertIn("virtual environment", stderr.getvalue())
        popen.assert_not_called()

    def test_missing_virtual_environment_python_reports_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = self._project_root(temp_dir, include_python=False)
            stderr = io.StringIO()
            with (
                patch.object(
                    run_app,
                    "__file__",
                    str(project_root / "options_matrix" / "run_app.py"),
                ),
                patch.object(run_app.sys, "argv", ["run_app.py"]),
                patch.object(run_app.subprocess, "Popen") as popen,
                redirect_stderr(stderr),
            ):
                result = run_app.main()

        self.assertEqual(result, 1)
        self.assertIn("Python executable is missing", stderr.getvalue())
        popen.assert_not_called()

    def test_foreground_uses_project_python_and_returns_child_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = self._project_root(temp_dir)
            completed = SimpleNamespace(returncode=17)
            with (
                patch.object(
                    run_app,
                    "__file__",
                    str(project_root / "options_matrix" / "run_app.py"),
                ),
                patch.object(
                    run_app.sys,
                    "argv",
                    ["run_app.py", "--foreground", "--example", "value"],
                ),
                patch.object(run_app.subprocess, "run", return_value=completed) as run,
            ):
                result = run_app.main()

        self.assertEqual(result, 17)
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [
                str(project_root / ".venv" / "Scripts" / "python.exe"),
                "-u",
                "-m",
                "options_matrix",
                "--example",
                "value",
            ],
        )
        self.assertEqual(run.call_args.kwargs["cwd"], project_root)
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_detached_launch_is_isolated_and_returns_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = self._project_root(temp_dir)
            original_environment = os.environ.copy()
            isolated_environment = {
                "PATH": "C:\\OtherProject\\.venv\\Scripts",
                "PYTHONHOME": "C:\\OtherPython",
                "PYTHONPATH": "C:\\OtherProject",
                "SCHWAB_APP_KEY": "preserved",
                "ORDINARY_VALUE": "preserved",
            }
            stdout = io.StringIO()
            with (
                patch.object(
                    run_app,
                    "__file__",
                    str(project_root / "options_matrix" / "run_app.py"),
                ),
                patch.object(run_app.sys, "argv", ["run_app.py", "--child-arg"]),
                patch.dict(run_app.os.environ, isolated_environment, clear=True),
                patch.object(run_app.subprocess, "Popen") as popen,
                redirect_stdout(stdout),
            ):
                result = run_app.main()

            self.assertEqual(os.environ.copy(), original_environment)

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "")
        command = popen.call_args.args[0]
        self.assertEqual(
            command[0],
            str(project_root / ".venv" / "Scripts" / "pythonw.exe"),
        )
        self.assertEqual(command[-1], "--child-arg")
        arguments = popen.call_args.kwargs
        self.assertEqual(arguments["cwd"], project_root)
        self.assertIs(arguments["stdin"], subprocess.DEVNULL)
        self.assertIs(arguments["stderr"], subprocess.STDOUT)
        self.assertFalse(arguments["shell"])
        self.assertTrue(arguments["close_fds"])
        self.assertTrue(
            str(arguments["stdout"].name).startswith(str(project_root / "logs"))
        )

        child_environment = arguments["env"]
        self.assertNotIn("PYTHONHOME", child_environment)
        self.assertNotIn("PYTHONPATH", child_environment)
        self.assertEqual(child_environment["SCHWAB_APP_KEY"], "preserved")
        self.assertEqual(child_environment["ORDINARY_VALUE"], "preserved")
        self.assertEqual(child_environment["VIRTUAL_ENV"], str(project_root / ".venv"))
        self.assertTrue(
            child_environment["PATH"].startswith(
                str(project_root / ".venv" / "Scripts") + os.pathsep
            )
        )
        expected_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        )
        self.assertEqual(arguments["creationflags"], expected_flags)

    def test_detached_launch_retries_without_breakaway_when_host_denies_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = self._project_root(temp_dir)
            access_denied = OSError("host job denied process breakaway")
            access_denied.winerror = 5
            with (
                patch.object(
                    run_app,
                    "__file__",
                    str(project_root / "options_matrix" / "run_app.py"),
                ),
                patch.object(run_app.sys, "argv", ["run_app.py"]),
                patch.object(
                    run_app.subprocess,
                    "Popen",
                    side_effect=[access_denied, SimpleNamespace(pid=4321)],
                ) as popen,
            ):
                result = run_app.main()

        self.assertEqual(result, 0)
        self.assertEqual(popen.call_count, 2)
        first_flags = popen.call_args_list[0].kwargs["creationflags"]
        second_flags = popen.call_args_list[1].kwargs["creationflags"]
        self.assertEqual(
            first_flags,
            (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
            ),
        )
        self.assertEqual(
            second_flags,
            (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
        )

    def test_start_failure_prints_traceback_and_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = self._project_root(temp_dir)
            stderr = io.StringIO()
            with (
                patch.object(
                    run_app,
                    "__file__",
                    str(project_root / "options_matrix" / "run_app.py"),
                ),
                patch.object(run_app.sys, "argv", ["run_app.py"]),
                patch.object(
                    run_app.subprocess,
                    "Popen",
                    side_effect=OSError("injected launch failure"),
                ),
                redirect_stderr(stderr),
            ):
                result = run_app.main()

        self.assertEqual(result, 1)
        self.assertIn("Options Matrix failed to start", stderr.getvalue())
        self.assertIn("injected launch failure", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
