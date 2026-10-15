# multi_matrix

Two programs that split Options Matrix across two PCs:

| Folder | Program | Runs on | What it does |
|---|---|---|---|
| `mini_pc` | **Options Matrix Studio** | Mini PC | Connects to Schwab. You choose the tickers, expiries, strike ranges and widths here. Records one snapshot per second to Parquet files. Shares the live matrix with the work PC. Can run itself every trading day (autopilot). |
| `work_pc` | **Options Matrix Viewer** | Work PC | Connects to the mini PC, never to Schwab. Shows any part of the mini PC's tabs with exactly the same numbers. Your positions and exit alerts live here. |

Your original `Options_Matrix` and `matrix_recorder` folders are untouched and still work on their own.

```
Schwab --> Mini PC: Options Matrix Studio --> recordings on the mini PC
                 |
                 |  live prices, tabs, settings (home network)
                 v
           Work PC: Options Matrix Viewer (positions + exit alerts)
```

How the numbers stay identical: the viewer doesn't receive finished numbers. It receives the mini PC's option chains, tab settings, fair-value settings and every live quote, then runs the **same code**, which it loads from the `mini_pc` folder. If the two PCs ever have different versions of that code, the viewer refuses to show anything and says **UPDATE NEEDED**.

---

## One-time setup

### On both PCs
1. Install Python 3.12 or newer from [python.org](https://www.python.org/downloads/). In the installer, keep **tcl/tk** and **py launcher** ticked.
2. Copy this **whole** `multi_matrix` folder to each PC. The viewer uses code from the `mini_pc` folder, so both folders must be present on the work PC too. Keep both PCs on the same copy.

### Mini PC
1. In `mini_pc`, double-click **setup.bat**. This creates the program's own Python environment (`mini_pc\.venv`) and installs what it needs. The window stays open when it's done so you can read the result.
2. Schwab credentials: copy `schwab_token.json` from `Options_Matrix\oma_app\schwab_auth\` into `mini_pc\schwab_auth\`, or run `mini_pc\schwab_auth\authorize_schwab.bat`. The credentials file stays on the mini PC only.
3. Double-click **Start Mini PC.pyw**. The first time it opens, it copies your existing fair-value settings and saved tabs from the original Options Matrix.
4. Windows will probably ask whether **Python** may use the network. Tick **Private networks** only and click **Allow**.
5. Click **Mini PC…** (top right) and note **this PC's address**, the **port** (47210 by default) and the **password**.
6. Network housekeeping:
   - Settings > Network & internet > your connection: set **Network profile type** to **Private**.
   - In your router's settings, **reserve** the mini PC's IP address so it never changes.
   - Never set up **port forwarding** for this port. The link is meant for your home network only.

### Work PC
1. In `work_pc`, double-click **setup.bat**.
2. Double-click **Start Work PC.pyw**.
3. Click **Connection…** and enter the mini PC's address, port and password. The viewer remembers them and reconnects by itself from then on.

Nothing needs to be opened in the work PC's firewall. Only the mini PC accepts connections.

---

## Using it

### Mini PC
- Works like the Options Matrix you know: **Connect**, then load a ticker, expiry, side, strike range and widths, then **Load matrix** (up to 3 tabs).
- **Recording** tab at the bottom: choose the folder, then **Start recording** / **Stop recording**. Starting before the open waits for the bell, and recording stops at the close.
- **⇄ 1 VIEWER** (top right) shows how many work PCs are watching.

### Work PC
- The viewer's tabs mirror the mini PC's tabs. For each tab, the **VIEW** row lets you narrow the **side**, **strike range** and **widths**. You can't go beyond what the mini PC has loaded. Click **Apply view**; **Show all** returns to the whole tab. A `*` on a tab means you're looking at part of it.
- Narrowing a view never changes the numbers. Fair values are always computed on the mini PC's whole tab.
- **Positions / exit alerts** work as before and are saved on the work PC. A position keeps being watched even when your current view hides its row. Double-clicking it brings the row back into view.
- **Settings…** shows the fair-value settings read-only; change them on the mini PC.
- The top line shows the mini PC's state, for example: `Mini PC market data ● LIVE · ● REC ON · 12,345 rows saved · NYSE open until 4:00 PM ET`.

### Safety: the viewer never shows frozen prices as live
If the viewer hears nothing from the mini PC for about 3.5 seconds, or the mini PC loses Schwab, it:
- greys out the matrix
- pauses signals and exit alerts
- shows **↻ RECONNECTING** or **MINI PC: NO MARKET DATA**
- reconnects by itself, and only shows prices again after a complete fresh copy has arrived

---

## Autopilot (mini PC that runs itself)
On the mini PC, click **Mini PC…** and tick:
- **Connect to Schwab when this program starts, and keep reconnecting.** Unlike before, it doesn't give up after Schwab's 5 automatic retries.
- **Reload the saved matrix tabs after connecting.**
- **Record every trading day automatically.** Recording is armed before the open, stops at the close, and starts again the next trading day. If you press **Stop recording** yourself, it stays stopped for the rest of that day.

Then:
- Double-click **mini_pc\add_to_startup.bat** so the program opens when Windows signs in. Undo it with `remove_from_startup.bat`.
- Turn off sleep: Settings > System > Power > Screen and sleep > **Never**.
- Set Windows Update **active hours** to cover market hours, so it doesn't restart mid-session.
- Optionally, let Windows sign in by itself after a restart.
- Install a remote-desktop tool (for example Chrome Remote Desktop) to change tabs and do the weekly Schwab re-authorization from the work PC.

### Weekly Schwab re-authorization
Schwab expires the login every 7 days. The viewer shows **⚠ AUTH 2d** when it's coming due. On the mini PC, run `mini_pc\schwab_auth\authorize_schwab.bat`.

---

## Try it with the market closed
On the mini PC, double-click **Start Mini PC (demo data).pyw**. It uses made-up prices, connects instantly, treats the market as open and can record. Its window title says **DEMO DATA**, and so does the viewer's. Demo tabs are saved separately from your real tabs.

---

## Look

Both programs use One Desk, the look shared with Market Synthetic Playback and the USO scalp dashboard:

- **Light by default.** Both programs open light. To open them dark, set the Windows environment variable `OPTIONS_MATRIX_THEME` to `dark`: run `setx OPTIONS_MATRIX_THEME dark` once, then start the program again. `auto` follows the Windows app setting, and `light` goes back.
- **Healthy cells keep plain hairlines.** A failed stream hatches its cells instead of outlining them in red. The hatch is denser when the failure keeps coming back. **Settings… > Matrix cell guide** shows every cell state.
- **Numbers are in Cascadia Mono**, labels in Bahnschrift, everything else in Segoe UI Variable. PCs without them fall back to Consolas and Segoe UI.
- The colors and fonts live in `mini_pc/options_matrix/one_desk.py` and the vendored `onedesk` folder next to it. Neither is part of the version check, so restyling never causes UPDATE NEEDED. Changes to `app.py` or `models.py` still need the same copy on both PCs.

## Troubleshooting

**A setup window flashes and closes, or says *running scripts is disabled on this system*:** a `.ps1` file was opened directly, and Windows blocks PowerShell scripts by default. Double-click the `.bat` file with the same name instead (for example `setup.bat`).

| Viewer shows | What to do |
|---|---|
| ✕ WRONG PASSWORD | Re-enter the password shown under **Mini PC…** on the mini PC (**Connection…**). |
| ⚠ UPDATE NEEDED | The PCs have different versions. Copy the same `multi_matrix` folder to both. |
| ↻ RECONNECTING | Is the mini PC on, awake, and running the Studio with sharing on? Check the address and firewall (below). |
| MINI PC: NO MARKET DATA | The mini PC isn't connected to Schwab. Check its window, or re-authorize Schwab. |

To check that the work PC can reach the mini PC, open PowerShell on the work PC and run this, with the mini PC's address:

```powershell
Test-NetConnection 192.168.1.50 -Port 47210
```

`TcpTestSucceeded : True` means the mini PC is reachable. If it says False:
- Check the address.
- Check that the mini PC's network is **Private**.
- Check the firewall. If you clicked **Cancel** on the Windows prompt, Windows saved a block rule for Python. Delete it under *Windows Defender Firewall > Advanced settings > Inbound Rules*, or add a port rule in an admin PowerShell on the mini PC:

```powershell
New-NetFirewallRule -DisplayName "Options Matrix Studio" -Direction Inbound -Protocol TCP -LocalPort 47210 -Profile Private -Action Allow
```

---

## Where things are kept
| | Mini PC | Work PC |
|---|---|---|
| Settings, saved tabs | `%APPDATA%\OptionsMatrixStudio` (`studio.json` holds the sharing password) | `%APPDATA%\OptionsMatrixViewer` (`viewer.json`) |
| Positions | (not used) | `%APPDATA%\OptionsMatrixViewer\positions.json` |
| Recordings | `mini_pc\recordings` unless you choose another folder | (none) |
| Logs | `mini_pc\logs` | `work_pc\logs` |

---

## Notes
- Both programs are display-only. Nothing here stages or places orders.
- The link between the PCs isn't encrypted (market prices and file names travel as plain data), so keep it on your home network or a private VPN such as Tailscale.
- The password itself is never sent: each side proves it knows the password by answering a one-time challenge.

## For developers and AI assistants
- Run the tests with the program folder's own Python, after `setup.bat -Dev`:
  - mini PC: `cd mini_pc`, then `.venv\Scripts\python.exe -B -m pytest -q`
  - work PC: `cd work_pc`, then `.venv\Scripts\python.exe -B -m pytest -q`
  - The work PC tests include end-to-end runs of both programs on this PC's loopback address, using demo data and real Tk windows.
- `work_pc` imports the `options_matrix` package from `mini_pc`. The viewer compares a fingerprint of `models.py` and `app.py` with the mini PC's.
- The link protocol is described in `mini_pc\docs\sharing.md`.
