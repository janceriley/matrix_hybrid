# Sharing link between the mini PC and work PC viewers

## Roles
- **Mini PC (Options Matrix Studio)** owns the Schwab connection, the matrix tabs, the fair-value settings and the recorder. `StudioServices` (`options_matrix/studio_services.py`) gathers state on the Tk thread and hands it to `ShareHub`/`ShareServer` (`options_matrix/share_server.py`). Those do all network work on their own threads and never call Tk.
- **Work PC (Options Matrix Viewer)** (`work_pc/matrix_viewer`) connects with `ShareClient`, then rebuilds every tab with the same `options_matrix` code:
  - `ViewerApp._mirror_tabs` hold full copies computed exactly like the mini PC's.
  - The display tabs in `ViewerApp.matrix_tabs` show the user's chosen part and share the mirrors' live quotes.

## Transport
TCP, each message a 4-byte big-endian length followed by UTF-8 JSON, at most 32 MiB. Port 47210 by default. The mini PC listens on all IPv4 interfaces. To listen on one address only (for example a Tailscale address, or `127.0.0.1` for testing), set the environment variable `OPTIONS_MATRIX_SHARE_HOST` before starting it. The link isn't encrypted, so use it only on a trusted home network or a private VPN, and never forward the port on a router.

## Sign-in (protocol version 1)
1. Mini PC → viewer: `{"type": "challenge", "protocol": 1, "nonce": <32 hex>}`.
2. Viewer → mini PC: `{"type": "hello", "protocol": 1, "client_nonce": <32 hex>, "proof": HMAC-SHA256(password, "options-matrix-share:viewer:<nonce>:<client_nonce>"), "viewer_name": <host>}`.
3. Mini PC → viewer, either:
   - `{"type": "denied", "reason": "password" | "version" | "full", "message": ...}`, or
   - `{"type": "welcome", "protocol": 1, "proof": HMAC-SHA256(password, "options-matrix-share:studio:<client_nonce>:<nonce>"), "fingerprint": ..., "studio_name": ..., "demo": bool}`.

The viewer checks the mini PC's proof, so the mini PC must know the password too. It also checks `fingerprint`, a hash of `models.py` and `app.py`. If the fingerprint differs, the viewer stops and shows **UPDATE NEEDED**.

## State messages (mini PC → viewer)
After sign-in, the viewer receives the complete current state in this order, then each change as it happens:

| Type | Content |
|---|---|
| `settings` | `formula` (fair-value settings) and `signal_method` |
| `tabs` | Every tab: `id`, `label`, `input` (full `MatrixInputState`), `stale`, `hint` (chain price), and `records` (key of the option chain). Also the option chains themselves (`records`), the ticker details (`search`) and the mini PC's active tab id. |
| `diagnostics` | Stream subscription state for each contract; drives the cell border colors and exit-alert eligibility |
| `health` | Sent once a second: Schwab connection, authorization badge, recorder state and rows, market status, autopilot text. Also acts as the heartbeat. |
| `quotes` | Batches of leg and underlying quotes, flushed every 100 ms |

Quote receive times are sent as ages (milliseconds before the frame was sent), encoded when the frame actually leaves. The viewer rebuilds them on its own clock, so a clock difference between the PCs can't make old prices look fresh. Exchange quote times are sent as absolute times.

## Failure handling
- A viewer that falls more than 2000 messages behind is disconnected; it reconnects and receives a fresh full state.
- The viewer treats more than 3.5 s without any message as a lost link:
  - it marks every tab stale, which clears the quotes
  - exit alerts pause
  - it reconnects
- Wrong password and version mismatch stop retrying until the settings change. "Too many viewers" (4 by default) retries every 10 s.
