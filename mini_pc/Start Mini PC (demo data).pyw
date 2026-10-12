"""Double-click to try the mini PC program with made-up market data (no Schwab)."""

import sys

from options_matrix.run_app import main

if __name__ == "__main__":
    sys.argv.append("--demo")
    raise SystemExit(main())
