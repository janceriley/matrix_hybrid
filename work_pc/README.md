# Options Matrix Viewer (work PC)

The work PC program. It connects to Options Matrix Studio on the mini PC and shows its live matrix. It never talks to Schwab and needs no Schwab credentials.

- **Identical numbers:** the viewer rebuilds every mini PC tab from the mini PC's option chain, settings and live quotes, using the same code, which it loads from the sibling `mini_pc` folder.
- **Your view:** for each tab, narrow the side, strike range and widths (**VIEW** row, **APPLY VIEW**, **SHOW ALL**). You can't show anything the mini PC hasn't loaded.
- **Positions and exit alerts** are saved on this PC and watch the mini PC's whole tabs, even rows your view hides.
- **Safe when the link drops:** after about 3.5 s without data, the matrix greys out and alerts pause until a fresh full copy arrives.

See the [main guide](../README.md) for setup and troubleshooting.

## Files
| File | Purpose |
|---|---|
| `setup.bat` | Creates `.venv`, installs the shared code from `..\mini_pc` and then this viewer. Runs `setup.ps1` |
| `Start Work PC.pyw` / `.bat` | Start the viewer |
| `add_to_startup.bat` / `remove_from_startup.bat` | Open the viewer when Windows signs in. Each runs the `.ps1` of the same name |
| `matrix_viewer\` | The viewer program |

The connection settings (address, port, password) and positions are kept in `%APPDATA%\OptionsMatrixViewer`.

## Tests
```powershell
.\setup.bat -Dev
.\.venv\Scripts\python.exe -B -m pytest -q
```
The end-to-end tests start both programs on this PC, connect them over `127.0.0.1` using demo data, and check that every number matches.
