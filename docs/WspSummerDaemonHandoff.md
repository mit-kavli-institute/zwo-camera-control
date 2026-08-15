# SUMMER → WSP Interface Daemon — Handoff / Requirements Doc

**Audience:** the agent/developer building the WSP-side integration for the
SUMMER camera in the `observatory` repo: the camera interface daemon and the
wiring that makes `wintercmd do_exposure`, robotic startup/shutdown, and
scheduled observations work against SUMMER.

**Status of the other side:** the SUMMER camera GUI (this repo,
`cmos-camera-gui`, branch `qhy42`) **already implements the full GUI-side
contract** from `observatory/docs/SummerCameraGuiHandoff.md` and passed all
seven steps of its §4 acceptance test against the real QHY42PRO on
2026-08-14, driven end-to-end through the shipped `SummerClient` with the
server running in headless service mode. Your job is the daemon layer only.
The cleaner you keep it a near-copy of `spring_camera_daemon.py`, the better;
every semantic difference you need to handle is enumerated in §6 below —
there are exactly seven, all small.

---

## 1. What you are building (mirror of GUI-handoff §6)

1. `wsp/camera/daemons/summer_camera_daemon.py` — near-copy of
   `spring_camera_daemon.py` with the client import swapped and the §6
   deltas applied; `create_camera_daemon(SummerCameraInterface, "SUMMERCamera")`.
2. `wsp/camera/implementations/summer_camera.py` — mirror of
   `spring_camera.py` (~10 lines).
3. `config.yaml` wiring: daemon host/port, telemetry keys, TEC setpoint;
   the existing `summer` darks/flats/focus blocks are reused as-is.
4. `summer_camera_daemon_interface_test.ipynb` — mirror of spring's,
   capturing real replies (ground-truth examples in §4/§5 below to compare
   against).

## 2. The GUI server you are talking to

- **Repo:** `github.com:mit-kavli-institute/zwo-camera-control`, branch
  `qhy42` (multi-vendor "CMOS Control GUI"; the QHY42PRO is SUMMER at the
  telescope, but the same app drives ZWO lab cameras — instrument identity
  is deployment config, see below).
- **Transport:** JSON over **WebSocket** (one request → one reply). You
  never touch this directly; `SummerClient` (§3) wraps it.
- **Launch (telescope service mode):**

  ```bash
  python -m cmos_camera_gui --headless --ws-port 5566 \
         --auto-connect qhy --instrument summer
  # or equivalently: python -m cmos_camera_gui --config summer.json
  # summer.json: {"headless": true, "ws_port": 5566,
  #               "auto_connect": "qhy", "instrument": "summer"}
  ```

  `--headless` runs controller + command server with no window (an operator
  GUI is available by omitting it — same protocol, same state, both can't
  hold the camera at once since it's one process either way).
  `--instrument summer` sets FITS `INSTRUME` and status `camname` to
  `summer`; without it the camera model is used (lab mode).
- **Port:** default 8765; **use 5566 at the telescope** (per GUI-handoff §3.4,
  ≠ spring's 5555) via `--ws-port`/config. Put it in WSP `config.yaml`.
- **Hard operational constraint — no hotplug:** the QHYCCD SDK enumerates
  cameras once at process start. The camera must be powered/connected
  *before* the GUI launches; if it is replugged, the GUI process must be
  restarted. Plan the service supervision accordingly (e.g. auto-restart on
  exit + the daemon's reconnect loop rides through it).
- **Server robustness (verified in acceptance):** abrupt client disconnect
  mid-capture is harmless (capture completes, file lands); reconnect works
  immediately; garbage JSON and unknown commands get error replies without
  killing the server; all state lives in the GUI process, none in the
  connection.

## 3. `SummerClient` — the client class your daemon imports

```python
from cmos_camera_gui.summer_client import SummerClient   # pip install -e the GUI repo, or vendor the file

cam = SummerClient(host="localhost", port=5566)
cam.connect()
```

`summer_client.py` depends only on `websockets` — it is safe to vendor the
single file into the observatory environment if you prefer not to install
the GUI package.

Method surface (pirtcam-compatible names and semantics):

| Method | Notes |
|---|---|
| `connect()` / `close()` | persistent connection |
| `get_status()` | `{"status": "success", "data": {...}}`, cached snapshot, never blocks on hardware |
| `set_exposure(seconds)` | float **seconds**; echoed **exactly** in status `exposure` (float `==` safe) |
| `set_save_path(dir)` | creates if missing, `~` ok |
| `capture_frames(filename, nframes=1, object=..., observer=..., headers=[...], wait_for_completion=False, debug=False)` | immediate ACK; completion by polling `is_capturing` |
| `set_tec_enabled(bool)` | |
| `set_tec_temperature(°C)` | setpoint clamped to camera range −45…+20 °C |

Replies are `{"status": "success"|"error", "message": ...}`; camera-side
failures are reported, never raised. Transport failures raise — your
`pollCameraStatus` `except` path treats that as a dead connection and
re-runs `setup_connection()`, exactly like spring.

`headers` accepts the same shapes the spring daemon already produces after
its OBJECT/OBSERVER stripping: a list of `(key, value)` / `(key, value,
comment)` tuples (or a dict). Caller-supplied headers win on collision with
GUI-generated cards.

## 4. `get_status` ground truth

Captured from the acceptance run (QHY42PRO live, instrument `summer`).
Required keys per the GUI-handoff §2.2 are all present; extras below the
line are free telemetry — forward what you like in
`update_camera_state_info()`.

```jsonc
{
  "status": "success",
  "data": {
    "camera_state": "READY",          // §5 vocabulary
    "ready": true,                    // connected AND state == READY
    "is_capturing": false,            // true from capture ACK until FITS closed
    "current_frame": 0,
    "total_frames": 0,
    "capture_time_remaining": 0.0,    // (frames left) x (exposure + overhead)
    "exposure": 7.5,                  // float SECONDS, exact echo of set_exposure
    "tec_temp": 16.7,                 // °C; -888 until first valid reading
    "tec_setpoint": 10.0,
    "tec_enabled": 1,
    "tec_locked": 0,                  // threshold+dwell: within 1.5°C for 45 s
    "tec_voltage": -888,              // QHY exposes PWM only — see §6.3
    "tec_power_pct": 100.0,           // cooler drive, 0-100 %
    "save_path": "C:/data/images/20260814/summer",
    "case_temp": -888,                // no such sensors on the QHY42
    "digpcb_temp": -888,
    "senspcb_temp": -888,
    // ---- extras (JSON-native, safe to pipe into telemetry) ----
    "camname": "summer",
    "vendor": "qhy",
    "connected": true,
    "streaming": true,
    "nframes": 1,                     // last-requested capture depth
    "idle_mode": false,               // high-speed idle (§7.3)
    "gain": 10, "offset": 140, "usbtraffic": 30,
    "gps_locked": 0,                  // present once streaming has begun
    "gps_seq": 82                     // hardware frame counter (drop detection)
  }
}
```

All values are JSON-native (float/int/bool/str; no numpy scalars) — verified
by round-tripping every key through `json.dumps` in the acceptance test.

## 5. `camera_state` vocabulary

Superset of pirt's; only `"READY"` is load-bearing for your completion
checks (set-exposure completion = `gui_state == "READY"` AND exact exposure
echo, both verified working):

| Value | Meaning |
|---|---|
| `READY` | idle, can accept commands |
| `SETTING_EXPOSURE` | queued exposure push not yet on hardware (usually <1 s) |
| `EXPOSING` | capture in flight |
| `SAVING` | frames captured, FITS still writing (`is_capturing` still true) |
| `TEC_SETTLING` | cooler regulating toward setpoint, not yet stable |
| `INITIALIZING` | camera connect in progress (~2.5 s) |
| `DISCONNECTED` | no camera open (e.g. before auto-connect completes) |
| `ERROR` | latched fault (capture/SDK/save failure) |

**Watch-item (agreed with Nate, revisit if it bites):** `TEC_SETTLING`
reappears whenever the TEC drifts out of its 1.5 °C band and blocks the
`setExposure` completion check until stability returns (up to ~45 s dwell) —
identical in kind to pirt/spring behavior, but the QHY TEC is coarser. If
mid-night `set_exposure` timeouts show up in ops, the fix candidates are a
looser GUI-side READY definition or a daemon-side completion tweak; flag it
rather than working around it silently.

`ERROR` recovery: the daemon's normal reconnect loop does not clear a
latched ERROR (it is camera state, not connection state). The GUI's native
protocol has a `{"cmd": "clear_error"}` verb (and disconnect/reconnect of
the *camera* clears it); for robotic recovery, sending that raw verb over
the same socket is acceptable — or restart the GUI service.

## 6. The seven deltas from `spring_camera_daemon.py`

Everything else is a mechanical copy. Handle exactly these:

1. **Client import/port:**
   `from cmos_camera_gui.summer_client import SummerClient` and
   `SummerClient(host=..., port=5566)` (host/port from `config.yaml`).
2. **`initialize_camera()` becomes a no-op.** The spring
   `set_correction("GAIN"/"OFFSET"/"SUB", ...)` calls have no SUMMER
   equivalent (and the status keys `gain_corr/offset_corr/sub_corr` do not
   exist — drop them from `update_camera_state_info()` or default to -888).
   Idempotence on reconnect is then trivial.
3. **TEC drive getters:** `tec_voltage` is always `-888`. Override
   `tecGetVoltage()`/`tecGetCurrent()` to return -888 and
   `tecGetPercentage()` to read `data["tec_power_pct"]` directly.
4. **Shutdown completion:** spring waits for warm-up (`tec_temp > -45`).
   SUMMER's TEC shuts off instantly with no ramp (decision: Nate,
   2026-08-14). Keep a warm-up threshold if WSP wants one for consistency
   (e.g. `> 0 °C`; from a 0 °C setpoint the sensor warms through that in a
   few minutes) or reduce the check to `not tec_enabled` — your call, flag
   which.
5. **Startup TEC target:** config `tec_setpoint` must lie in the camera's
   clamp range **−45…+20 °C** (the GUI clamps silently). Cooldown reference:
   ~126 s from warm to a locked 0 °C in the GUI author's measurements;
   spring's 30 min startup timeout has huge margin. The startup completion
   check (`|tec_temp − setpoint| < 0.5`) works unchanged — note it is
   *stricter* than the GUI's own `tec_locked` (1.5 °C band), which is fine:
   you're comparing temps, not reading the flag.
6. **`update_camera_state_info()` keys:** replace the spring extras with
   SUMMER's (suggested: `tec_power_pct`, `gain`, `offset`, `usbtraffic`,
   `gps_locked`, `gps_seq`, `vendor`, `idle_mode`, plus `gui_state` from
   `camera_state` as spring does). `case/digpcb/senspcb_temp` read -888 and
   can stay wired for shape compatibility.
7. **`getDefaultImageDirectory()`:** `~/data/images/<tonight>/summer`.

## 7. Capture lifecycle — verified numbers and options

### 7.1 The nframes=1 path (all WSP operations)

Measured in the acceptance run (2 s exposure, full frame):

- ACK returned in **5 ms**; the *first* `get_status` after the ACK already
  showed `is_capturing: true` (R1 — the flag is set synchronously inside
  the command, so the framework's 1 s grace period has ~200x margin).
- `is_capturing` stays true through readout **and** FITS write, dropping
  false only after the file is closed (R2). Writes are atomic
  (temp + rename): the instant your `_exposure_complete` fires and
  `makeImageFilepath` is symlinked, the file is complete — a failed save
  can never leave a partial file at the target path (it latches `ERROR`
  and drops `is_capturing` with no file).
- Output is exactly `<save_path>/<filename>.fits`, **single 2D image HDU**,
  containing `OBJECT`, `OBSERVER`, all passthrough header cards, plus GUI
  camera-truth cards: `EXPTIME` (ms) / `EXPOSURE`-related controls, `GAIN`,
  `OFFSET`, `INSTRUME='summer'`, `DETECTOR='QHY42PRO'`, `DATE-OBS` +
  `TIMESRC` (GPS time of first frame when the GPS is locked, host UTC
  otherwise), `DATASEC`, `NFRAMES`, `DETTEMP`, GPS cards when enabled.
- Timing budget: capture ≈ stream-spin-up (≤1.5 s, first capture only) +
  gate (≤ ~2 frame periods) + exposure + 42 ms readout + save (8 MB, sub-
  second). Comfortably inside `2·exptime + 30 s` at any exposure.

### 7.2 The reserved stack option (nframes > 1)

Per Nate's requirement, WSP may command a stack saved as a **single file**:
`capture_frames(filename=..., nframes=N)` writes the (N, H, W) cube (plus a
`FRAMEMETA` bintable of per-frame GPS/timing) at the same exact path, with
identical `is_capturing` semantics (verified with N=3 in acceptance).
**Caveat for the WSP side:** anything that opens `last_image.fits` must
tolerate a 3D primary HDU when this option is used. `nframes=1` remains the
default and the guaranteed-2D path.

### 7.3 High-speed idle (optional, off by default)

`{"cmd": "set", "idle_mode": true}` (raw verb) makes the stream idle at
1 ms between grabs instead of the target exposure. Measured: the
first-frame latency floor (T + ~65–100 ms) is achieved **with or without**
idle mode on this camera, so WSP does not need it for latency; it only
changes between-grab readout duty cycle. Ignore unless thermal tuning
becomes interesting.

## 8. Acceptance for the daemon

Mirror spring's interface-test notebook and check, at minimum: status shape
matches §4; `set_exposure(7.5)` completes via the daemon's own
`_check_set_exposure_complete`; a `doExposure` round-trip produces the file
at `makeImageFilepath`'s path with `OBJECT`/`OBSERVER` and the symlink
updated; robotic `autoStartup` reaches READY from warm within its timeout;
kill the GUI process mid-session and confirm the daemon's reconnect loop
recovers once the service restarts.

GUI-side evidence to compare against: the acceptance script
(`§4 of SummerCameraGuiHandoff`) passing in full lives with the GUI repo's
session records; PLAN.md §10 in the GUI repo maps every contract clause to
its implementation.
