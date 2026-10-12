from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class AuthorizeSchwabLayoutTests(unittest.TestCase):
    def test_helper_imports_package_and_resolves_sibling_token(self) -> None:
        project_root = Path(__file__).resolve().parent.parent
        helper_path = project_root / "schwab_auth" / "authorize_schwab.py"
        probe = textwrap.dedent(
            """
            import builtins
            import runpy
            import sys
            import types
            import urllib.request
            import webbrowser
            from pathlib import Path

            def forbidden(*_args, **_kwargs):
                raise AssertionError("authorization side effect ran during import")

            sys.modules["websocket"] = types.ModuleType("websocket")
            builtins.input = forbidden
            urllib.request.urlopen = forbidden
            webbrowser.open = forbidden

            helper = Path(sys.argv[1]).resolve()
            namespace = runpy.run_path(
                str(helper),
                run_name="authorize_schwab_import_smoke",
            )
            assert namespace["TOKEN_PATH"] == helper.parent / "schwab_token.json"
            assert namespace["PROJECT_ROOT"] == helper.parent.parent
            assert (
                namespace["exchange_authorization_code"].__module__
                == "options_matrix.schwab_client"
            )
            """
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    probe,
                    str(helper_path),
                ],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
