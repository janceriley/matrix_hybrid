# Options Matrix Studio (mini PC)

The mini PC program. It's the original Options Matrix display with three additions:

1. **Built-in recorder.** A **Recording** tab writes the same 42-column Parquet files as the separate Matrix Recorder did: one snapshot per second during NYSE hours, buffered and flushed off the window's thread. Files go in `recordings\<date>_ET__<TICKER>__<id>\part-0001.parquet`, …
2. **Sharing with work PC viewers.** The live matrix is offered on your home network: port 47210 by default, protected by a password. Click **MINI PC…** to see the address, port and password.
3. **Autopilot.** It can connect at startup and keep reconnecting, reload the saved tabs, and record every trading day. Turn these on under **MINI PC…**.

See the [main guide](../README.md) for setup on both PCs, troubleshooting and the daily routine.

## Files
| File | Purpose |
|---|---|
| `setup.bat` | Creates `.venv` and installs everything (`-Dev` adds test tools). Runs `setup.ps1` |
| `Start Mini PC.pyw` / `.bat` | Start the program |
| `Start Mini PC (demo data).pyw` | Start with made-up prices, for testing any time |
| `add_to_startup.bat` / `remove_from_startup.bat` | Open the program when Windows signs in. Each runs the `.ps1` of the same name |
| `schwab_auth\authorize_schwab.bat` | Weekly Schwab re-authorization |
| `options_matrix\` | The program. The work PC viewer imports this code too |
| `docs\sharing.md` | How the link to the work PC works |

## What changed from the original Options Matrix
- Recording no longer needs the separate Matrix Recorder program; the old `--recorder-link` mode is gone.
- Preferences live in `%APPDATA%\OptionsMatrixStudio`, so the original program's settings are never changed. They're copied from the original once, on first start. Positions aren't copied, because they belong on the work PC.
- The window title is **Options Matrix Studio (mini PC)**.

## Tests
```powershell
.\setup.bat -Dev
.\.venv\Scripts\python.exe -B -m pytest -q
```
