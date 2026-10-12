"""Schwab one-time / weekly authorization helper.

Double-click ``authorize_schwab.bat`` in the ``schwab_auth`` folder (or run
``python schwab_auth/authorize_schwab.py`` from the ``mini_pc`` folder). It
opens the Schwab consent page in your
browser; after you log in and approve, copy the ENTIRE address of the page you
land on, paste it here, and press Enter. The script exchanges the code
immediately (beating the ~30-second expiry) and writes the new refresh token
into ``schwab_token.json`` for you.

Reads app_key/app_secret from schwab_token.json; never prints token values.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

AUTH_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTH_DIR.parent
TOKEN_PATH = AUTH_DIR / "schwab_token.json"
REDIRECT_URI = "https://127.0.0.1:8182"

sys.path.insert(0, str(PROJECT_ROOT))

from options_matrix.schwab_client import (  # noqa: E402
    SchwabRequestError,
    exchange_authorization_code,
)


def extract_code(raw: str) -> str:
    """Accept a full redirect URL, a code with &session= junk, or a bare code."""
    raw = raw.strip().strip('"').strip("'")
    if "code=" in raw:
        query = urllib.parse.urlparse(raw).query
        if not query and "?" in raw:
            query = raw.split("?", 1)[1]
        values = urllib.parse.parse_qs(query).get("code")
        if values and values[0]:
            return values[0]
    return urllib.parse.unquote(raw.split("&", 1)[0])


def main() -> int:
    try:
        data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read {TOKEN_PATH}: {exc}")
        return 1

    app_key = str(data.get("app_key") or "").strip()
    app_secret = str(data.get("app_secret") or "").strip()
    if not app_key or app_key.startswith("YOUR_") or not app_secret or app_secret.startswith("YOUR_"):
        print(f"Fill in app_key and app_secret in {TOKEN_PATH} first (from developer.schwab.com).")
        return 1

    auth_url = (
        "https://api.schwabapi.com/v1/oauth/authorize"
        f"?response_type=code&client_id={urllib.parse.quote(app_key)}"
        f"&scope=readonly&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
    )

    print("Opening the Schwab authorization page in your browser ...")
    print("(If it does not open, copy this address into your browser yourself:)")
    print(f"  {auth_url}")
    print()
    print("Log in with your Schwab BROKERAGE credentials and approve access.")
    print("You will land on a page that looks broken (https://127.0.0.1/...) - that is normal.")
    print("Copy the ENTIRE address of that page from the address bar.")
    print()
    webbrowser.open(auth_url)

    raw = input("Paste the address (or just the code) here and press Enter:\n> ")
    if not raw.strip():
        print("Nothing pasted; aborting.")
        return 1
    code = extract_code(raw)

    print("Exchanging the code for tokens ...")
    try:
        payload = exchange_authorization_code(code, REDIRECT_URI, app_key=app_key, app_secret=app_secret)
    except SchwabRequestError as exc:
        print(f"EXCHANGE FAILED: {exc}")
        print()
        print("Common causes: the code expired (redo this whole flow - it is quick),")
        print("or the Callback URL registered on your Schwab app is not exactly")
        print(f"{REDIRECT_URI} (check the app settings on developer.schwab.com).")
        return 1

    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        print("The response contained no refresh_token. Keys received:", sorted(payload))
        return 1

    data["refresh_token"] = refresh_token
    # The app warns ahead of Schwab's 7-day refresh-token expiry using this.
    data["refresh_token_issued_at"] = datetime.now(timezone.utc).isoformat()
    TOKEN_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print()
    print(f"SUCCESS - refresh token saved to {TOKEN_PATH.name}.")
    print("You can now launch the app with launch_options_matrix.pyw and click CONNECT.")
    print("Schwab expires this token after 7 days; when CONNECT starts failing,")
    print("just run this helper again.")
    return 0


if __name__ == "__main__":
    exit_code = main()
    print()
    input("Press Enter to close this window.")
    sys.exit(exit_code)
