# CMOS Control GUI — Multi-Vendor Extension Plan

Goal: extend the ZWO-only GUI into a **cmos-control-gui** that also operates a
QHY42PRO (per `HANDOFF.md`), is cleanly extensible to future cameras, and
guarantees that **every camera capability is operable identically from the GUI
and from the remote (WebSocket) connection**.

---

## 1. The core inversion: a headless `CameraController`

**Problem today.** Remote commands are executed by `gui.py:handle_ws_command`
*driving GUI widgets* (`setValue`, `setChecked`, then calling button slots).
The GUI is the controller; the WS server is a puppeteer. Parity between local
and remote operation exists only because every remote command is a simulated
click. Adding a second vendor inside that structure would double the widget
plumbing and make parity accidental.

**Design.** Introduce `core/controller.py` — a **headless, Qt-free**
`CameraController` that owns all state and is the *single* command surface:

```
                 ┌────────────────┐
   GUI (view) ──▶│                │──▶ CameraBackend (zwo | qhy | fake)
                 │ CameraController│──▶ Recorder (FITS)
   WS server  ──▶│                │──▶ HeaderAssembler
                 └────────────────┘
```

- Controller methods ≈ today's WS verbs: `list_cameras`, `connect(index)`,
  `disconnect`, `set_controls(dict)`, `set_roi`, `start_stream`, `stop_stream`,
  `record(n, mode, directory, basename, obstype, extra_headers)`, `abort`,
  `cooler(on, target)`, `status() -> dict`.
- It emits **events** (callbacks or a small signal bus, not Qt signals):
  `frame_preview`, `stats`, `record_progress`, `record_done`, `state_changed`,
  `error`. GUI subscribes and renders; WS server subscribes and forwards.
- **Parity rule (enforced, not aspirational):** GUI and `ws_server` may import
  only `core/`; **no vendor imports outside `vendors/`**; the GUI never
  touches a backend or SDK directly. A capability that exists only as a widget
  handler is a bug. A parity unit test walks the WS verb table and asserts
  each maps 1:1 onto a controller method.
- The controller is fully usable with no GUI at all (headless server mode
  later comes for free).

## 2. Package layout & rename

Rename package `zwo_camera_gui` → `cmos_camera_gui` (project
`cmos-camera-gui`, script `cmos-camera-gui`) via `git mv` to keep history.

```
src/cmos_camera_gui/
  __main__.py               # CLI: --vendor auto|zwo|qhy, --sdk-path, --ws-port
  core/
    controller.py           # headless CameraController (§1)
    backend.py              # CameraBackend ABC + Frame/StreamStats dataclasses (§3)
    controls.py             # camera_config.py, vendor maps removed (§5)
    headers.py              # header framework (§4)
    events.py               # tiny thread-safe event bus (callbacks)
  vendors/
    __init__.py             # registry: {"zwo": ..., "qhy": ...}; auto-detect order
    zwo/
      sdk.py                # current sdk.py, unchanged
      backend.py            # ZwoBackend (wraps ASICamera + capture loop)
      profile.py            # name renames, hidden ctrls, default overrides, display order
    qhy/
      sdk_wrapper.py        # copied ~verbatim from qcam_gps_live (per HANDOFF §2)
      backend.py            # QhyBackend: lifecycle, grab state machine, TEC keep-alive
      profile.py            # exposed ControlIDs, defaults (gain 10, offset 140, ...)
  gui/
    main_window.py          # current gui.py minus controller logic; view over controller
    widgets.py, stretch.py, style.py
  remote/
    ws_server.py            # dispatches to controller directly (no Qt bridge for logic)
    client.py               # CameraClient (ASIClient kept as deprecated alias)
  io/
    recorder.py             # FITS writers + header validation hook
sdk/                        # vendor DLLs (qhyccd.dll here; ASICamera2.dll optional)
```

## 3. `CameraBackend` interface (the vendor contract)

One abstract class every vendor implements. Chosen to fit *both* acquisition
models we already have:

- **ZWO model:** continuous video stream; "record" = keep the next N frames.
- **QHY model (HANDOFF §4):** continuous live stream at idle exposure;
  "record" = exposure-switch → delta-gate transition frames → keep N → re-arm.

Contract (threading per HANDOFF: **the backend owns the one SDK thread**;
frames cross to the controller via a bounded queue):

```python
class CameraBackend(ABC):
    # discovery / lifecycle
    @classmethod list_cameras() -> list[CameraDesc]      # {index, name, id}
    open(index) / close()
    describe() -> CameraDesc                             # name, sensor, sizes, capability flags
    capabilities() -> Capabilities                       # has_cooler, has_gps, needs_idle_stream, ...
    control_caps() -> dict[str, caps_dict]               # feeds core.controls unchanged

    # configuration (legal mid-stream for both vendors)
    set_control(name, value) / get_control(name)
    set_roi(x, y, w, h, binning) / get_roi()
    set_bit_depth(bits)

    # acquisition — backend pushes into queues owned by the controller
    start_stream(frame_queue, event_queue)               # continuous; previews flow
    stop_stream()
    start_record(n, exposure_us)                         # vendor fast path; kept frames
                                                         # arrive tagged kind="kept"
    abort_record()

    # thermal
    set_cooling(on, target_c) / thermal() -> Thermal     # temp, power_pct, regulating

    # headers (§4)
    run_header_cards(ctx) -> list[Card]
    frame_header_cards(frame) -> list[Card]
```

`Frame` dataclass: `data (ndarray), t_host (perf_counter), kind
("preview"|"kept"), seq (int|None), vendor_meta (dict)` — QHY fills
`seq`/`vendor_meta` from the GPS row; ZWO leaves them None.

Backend-internal, invisible to the controller:

- **ZwoBackend:** current `CaptureWorker` loop, minus Qt (plain thread +
  queues); record = flag next N stream frames as kept — **after an
  inter-arrival delta gate discards in-flight frames**. Measured on the
  ASI294MM Pro (2026-08-14): the SDK buffers 1–2 frames exposed *before*
  the record command, so a `set Exposure` + `record` sweep records its
  first frame(s) at the OLD exposure — the ZWO analog of the QHY
  transition-frame problem. Gate: keep only frames with
  `0.8×T ≤ delta ≤ T + readout_margin`, unifying both vendors on the
  handoff's "every kept exposure starts after the grab command" rule.
  (Side effect: EXPOSING then genuinely spans N fresh exposures, matching
  operator intuition.) Also measured: sub-frame ROIs (e.g. 1024×1024
  RAW16) stall the ASI294 stream for seconds at a time — a camera/SDK
  quirk to document, not code around.
- **QhyBackend:** HANDOFF recipe verbatim — init order (read mode → stream
  mode → InitQHYCCD → bits → effective area → params → GPS on → DDR on),
  stale-DDR drain after `BeginQHYCCDLive`, reusable frame buffer, the
  IDLE→CAPTURING→IDLE state machine with the **0.8×T inter-arrival gate**,
  ~0.2 s re-arm to idle, TEC keep-alive re-issued every few seconds from the
  SDK thread, temp distrusted while PWM == 0, GPS row-0 parse then row 0
  excluded from preview/statistics (data kept intact; flagged in header).

**Capabilities drive the GUI**, not vendor checks: cooler panel appears iff
`has_cooler`; GPS/seq status iff `has_gps`; idle-exposure control iff
`needs_idle_stream`. New vendor ⇒ new folder + registry entry, zero edits in
`gui/` or `remote/`.

## 4. FITS header framework (required + camera-specific)

New `core/headers.py`, replacing the inline dict in `gui.py`:

- **`Card`** = `(key ≤ 8 chars, value, comment)`, validated at construction
  (no more silent `k[:8].upper()`).
- **Three layers, merged in order (later wins):**
  1. **Required minimum set** — assembled by the controller, every camera,
     every file. Missing ⇒ save fails loudly (or `UNKNOWN` + warning, one
     policy, decided in code review):
     `DATE-OBS, INSTRUME, VENDOR, CAMID, EXPTIME, GAIN, OFFSET, NFRAMES,
     DEPTH, ROI_X/Y/W/H, XBINNING/YBINNING, DETTEMP, OBSTYPE, STRMFPS,
     ELAPSED, SWCREATE (name+version), TIMESYS/TIMESRC (host vs GPS)`.
  2. **Vendor/run cards** — `backend.run_header_cards()`: remaining control
     snapshot, read mode, USB traffic, TEC setpoint & PWM, GPS enabled flag,
     SDK/firmware versions, `GPSROW0=T` ("row 0 contains GPS binary header").
     The required set includes **`DATASEC`** (IRAF-style 1-based section,
     honored by DS9/IRAF/astropy): normally the full frame, e.g.
     `'[1:2048,1:2048]'`; when the QHY GPS header is active the backend
     overrides it to `'[1:2048,2:2048]'` so standard tools automatically
     exclude the GPS row from display scaling and statistics. `GPSROW0` stays
     as the human-readable explanation of *why* row 1 is excluded.
  3. **User extras** — the existing `extra_headers` pass-through, still last.
- **Per-frame cards** — `backend.frame_header_cards(frame)`:
  - common: `FRAME_ID, TIMESTMP, DELTA_T` (as today);
  - QHY: `GPS_SEQ, GPS_LOCK, DATE-BEG (GPS UTC exposure start), GPS_LAT/LON`.
  - *Individual mode:* cards go into each file's header (as now).
  - *Cube mode:* per-frame values become a **bintable extension**
    (`EXTNAME=FRAMEMETA`, one column per card key) so GPS timestamps survive
    stacking. Host timestamps move there too (shared code path).
- `io/recorder.py` gains a validation step against the required schema and the
  bintable writer; otherwise unchanged.

## 5. Vendor-neutral controls layer

`camera_config.py` → `core/controls.py` with the ZWO-specific module globals
(`_KIND_BY_NAME`, `_NAME_RENAMES`, `_DEFAULT_OVERRIDES`, `_HIDDEN`,
`_DISPLAY_ORDER`) extracted into a `VendorProfile` dataclass supplied by
`vendors/*/profile.py`. The QHY profile exposes a curated subset as canonical
names (`Gain`, `Offset`, `Exposure`, `UsbTraffic`, `IdleExposure`,
`TargetTemp`) with ranges from `GetQHYCCDParamMinMaxStep`, so
`ControlSpec`/`CameraSettings`, the GUI control panel, the WS `set` verb, and
header snapshots all work unchanged. **Canonical control names are the shared
vocabulary** across GUI, WS API, and headers — same name everywhere, both
vendors.

## 6. Remote protocol

Verbs stay as-is (existing clients keep working): `status, list_cameras,
connect_camera, disconnect_camera, set, start_stream, stop_stream, record,
cooler` — plus:

- `record` gains optional `exposure_us` (maps to QHY grab; for ZWO it's just
  `set Exposure` + record).
- `abort` — cancel an in-flight record/grab (both vendors).
- `status` reply grows: `camera_state` (FSM name, §7), `tec_locked`,
  `tec_stalled`, `vendor`, `temp`, `setpoint`, `cooler_power`,
  `gps: {locked, last_seq, last_utc}`, `dropped_seq` — fields present per
  capabilities. `camera_state` and `tec_locked` deliberately reuse the
  pirt-camera-control field names/values so the telescope system's
  `wait_for_state("READY")` pattern works against either GUI.
- `list_cameras` entries gain `vendor`.
- `client.py`: class renamed `CameraClient`, `ASIClient = CameraClient` alias;
  new `abort()`, richer `status()`.

The WS server keeps its asyncio thread but calls the controller directly;
only *GUI refresh* hops through Qt, not command execution.

## 7. Finite state machine & TEC settling

For telescope-system compatibility the controller exposes an explicit FSM,
mirroring pirt-camera-control's `CameraState` (`READY / SETTING_EXPOSURE /
TEC_SETTLING / EXPOSING / ERROR`, reported as `camera_state` in status):

```
DISCONNECTED    no camera open
INITIALIZING    connect/init in progress (QHY InitQHYCCD ~2 s + DDR drain)
TEC_SETTLING    idle, cooler regulating toward setpoint — data-taking discouraged
READY           idle (streaming or not), TEC settled or cooler off
EXPOSING        record/grab in flight (includes QHY transition-frame discard)
SAVING          kept frames flushing to FITS (writer draining)
ERROR           latched fault; cleared by disconnect/reconnect or explicit clear
```

- Priority when conditions overlap: `ERROR > EXPOSING > SAVING >
  TEC_SETTLING > READY`. The three names the telescope relies on
  (`READY`, `EXPOSING`, `ERROR`) have identical semantics to pirt;
  `SETTING_EXPOSURE` is not needed (exposure changes are fast on both
  vendors) but the extra states are additive — `wait_for_state("READY")`
  behaves the same.
- The FSM lives in `CameraController` (single source of truth); the GUI shows
  it as a status chip, the WS server reports it, and state-change events are
  pushed so remote clients can subscribe instead of polling.
- **Policy:** `TEC_SETTLING` *reports*, it does not hard-block — `record`
  while settling is accepted but the reply carries a warning and the header
  gets `TECSTTL=F`. The telescope achieves blocking by waiting for `READY`.

**TEC settled definition** (`core/thermal.py`, vendor-agnostic — consumes
only `backend.thermal()` samples):

- Settled = `|temp − setpoint| ≤ tolerance_c` **continuously for `dwell_s`**
  (threshold + dwell, not slope). Unsettle only beyond
  `tolerance_c + hysteresis_c` to prevent flapping at the boundary.
- Restart-robust by construction: both vendors' TECs drop regulation when the
  camera closes, so re-entering TEC_SETTLING for one dwell period after a GUI
  restart reflects reality — no persisted state, no slope history needed.
- **Stall detection** (the bad-PID case — TEC parks short of setpoint):
  PWM saturated (≥ 95 %) + flat slope + outside tolerance for
  `stall_timeout_s` ⇒ `tec_stalled: true` in status/telemetry while remaining
  in TEC_SETTLING. Surfaces "will never settle" instead of hanging forever;
  slope is used *only* here, on long timescales where restarts don't matter.
- Knobs in **one obvious place**: `tec_tolerance_c`, `tec_dwell_s`,
  `tec_hysteresis_c`, `tec_stall_timeout_s`, and setpoint min/max per vendor
  in `vendors/*/profile.py` (e.g. QHY42 tolerance looser than ZWO),
  overridable at runtime via the normal `set` verb and echoed in `status`.
- QHY guards stay in the backend: thermal samples are withheld while
  PWM == 0 (readout frozen at a bogus value) and during the initial PWM-255
  slam, so the detector never sees garbage.
- Header cards at record start: `TECSETP` (setpoint), `TECSTTL` (settled
  bool), `TECPWM` — alongside the existing `DETTEMP`.

## 8. Phased execution (each phase leaves a working app)

**Status (2026-08-14):** Phases 0-1 done and hardware-validated (ASI294).
Phase 2 landed in a pragmatic form: instead of a formal Qt-free
`CameraBackend` ABC, vendors are duck-typed adapters + per-vendor capture
workers under `vendors/{zwo,qhy}/` with a registry, the SDK-thread-owns-
everything contract, the shared `RecordGate`, per-vendor profiles
(defaults, TEC tuning), capability flags driving the GUI, vendor run
header cards (`DATASEC`, `GPSROW0`, ...), and per-frame metadata
(FRAMEMETA bintable / GPS cards) — i.e. Phase 3's header essentials came
along. The formal ABC + full required-header validation remain future
polish. Phase 4 (QHY) is code-complete per HANDOFF; hardware validation
pending (drop `qhyccd.dll` into `sdk/`).

| Phase | Work | Risk / validation |
|---|---|---|
| **0. Rename** | `git mv` package, update pyproject/entry point/README; window title "CMOS Control GUI" | Mechanical; app runs as before |
| **1. Controller extraction** | Pull all state + command logic out of `gui.py` into `CameraController`, including the FSM (§7) and `camera_state` in status; GUI becomes a view; `ws_server` targets the controller. ZWO only, no new features | **Highest-risk refactor — done first, alone.** Validate: GUI parity by hand + WS regression against `client.py` |
| **2. Backend interface** | Define `CameraBackend`/`Frame`/`Capabilities`; wrap existing ASI code as `ZwoBackend` under `vendors/zwo/`; de-Qt the capture loop; extract ZWO `VendorProfile`; add `FakeBackend` for tests; `core/thermal.py` settle/stall detector wired to `thermal()` telemetry | Existing behavior unchanged; parity test + fake-backend CI tests land here (incl. scripted thermal ramps → settle/stall transitions) |
| **3. Header framework** | `core/headers.py`, required-set validation, bintable per-frame metadata, recorder wiring | Compare old vs new headers on a real ZWO capture |
| **4. QHY backend** | Copy `sdk_wrapper.py`; DLL in `sdk/`; implement `QhyBackend` per HANDOFF (§3 checklist as the test list); QHY profile | Hardware validation: overhead ≈ 65 ms, cadence ±3 ms, 2–3 gated transition frames, one seq skip per switch, TEC regulation, GPS decode |
| **5. GUI + remote polish** | Vendor selector / auto-detect, capability-driven panels (cooler, GPS status, idle exposure, grab-vs-record labeling), `abort` verb, docs, README | End-to-end: same scripted WS session against both cameras |

## 9. Testing strategy

- **`FakeBackend`** (deterministic synthetic frames, fake GPS row, fake
  thermal ramp) → controller, recorder, header, and WS tests run in CI with
  no hardware.
- **Parity test:** every WS verb ↔ controller method, and controller public
  API contains nothing GUI-only.
- **FSM test:** scripted sequences on `FakeBackend` assert the state graph
  (connect → TEC_SETTLING → READY → EXPOSING → SAVING → READY; error
  latching; stall flag on an unreachable-setpoint thermal ramp).
- **Header test:** required-set completeness for both vendors; GPS cards
  appear iff `has_gps`.
- **Hardware checklists** (manual, scripted via `client.py`): HANDOFF §10 for
  QHY; current behavior snapshot for ZWO.

## 10. Decisions taken (flag if you disagree)

- TEC "settled" = **threshold + dwell** (`|temp − setpoint| ≤ tolerance` held
  for `dwell_s`), with hysteresis; slope is used only for long-timescale
  stall detection, never for the settled decision — so GUI restarts cost one
  honest dwell period and nothing more.
- `TEC_SETTLING` warns but never hard-blocks `record`; blocking is the
  caller's policy (`wait_for_state("READY")`).
- FSM state names and the `camera_state` / `tec_locked` status keys mirror
  pirt-camera-control verbatim for telescope compatibility — but the
  **transport stays WebSocket**: the telescope talks to this app through our
  own `CameraClient`, so no pirt TCP protocol adapter is needed. `CameraClient`
  gains a `wait_for_state(state, timeout)` helper matching the pirt client's
  usage pattern.

- Package name **`cmos_camera_gui`** / project **`cmos-camera-gui`** (repo can
  stay or be renamed on GitHub later; nothing in-code depends on it).
- Single camera connected at a time (as today); the registry design doesn't
  preclude multi-camera later.
- Controller events use plain callbacks + queues (Qt-free core); the GUI
  wraps them in signals at its edge.
- QHY GPS row 0 is **kept in saved data** rather than stripped — raw sensor
  output stays intact. It is declared via the standard **`DATASEC`** keyword
  (`'[1:2048,2:2048]'` when GPS is active) so DS9 and standard pipelines
  exclude it automatically, plus `GPSROW0=T` as the explanatory flag.
  Stripping would silently change frame geometry between vendors.
- Old `ASIClient` name kept as an alias for one release.
