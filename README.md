# CMOS Control GUI

Multi-vendor CMOS camera control: PyQt5 GUI + JSON-WebSocket remote command
server over a shared headless `CameraController`, so every camera capability
works identically from the GUI and from a remote client.

**Supported cameras:** ZWO ASI (direct ctypes wrapper around
`ASICamera2.dll`) and QHYCCD (validated on the QHY42PRO, incl. GPS frame
timestamps, TEC control, and the fast N-frame grab method). At the telescope
this app is the **SUMMER camera GUI** for WSP — see
[docs/WspSummerDaemonHandoff.md](docs/WspSummerDaemonHandoff.md).

Highlights:

- Vendor registry (`vendors/zwo`, `vendors/qhy`) — new cameras plug in
  without touching core, GUI, or remote code.
- Finite state machine (`READY / EXPOSING / SAVING / TEC_SETTLING / ...`)
  reported over the wire; TEC settle + stall detection.
- Record gate: every kept exposure starts *after* the record command
  (in-flight / transition frames are discarded, both vendors).
- FITS: required-header set with save-time validation, `DATASEC`, GPS
  per-frame metadata (FRAMEMETA bintable), combined outputs
  (mean / median / sum with net-integration EXPTIME), atomic writes.
- High-speed idle mode, capability-driven GUI, advanced-controls dialog.

## Install

```bash
pip install -e .          # app
pip install -e ".[test]"  # + pytest
```

## SDKs

- **ZWO:** `ASICamera2.dll` via `--sdk`, the Browse button, ASIStudio
  install paths, or PATH.
- **QHY:** drop `qhyccd.dll` under [sdk/](sdk/README.md) (or set
  `QHYCCD_SDK_DLL`). No hotplug — connect the camera before launching.

## Run

```bash
# interactive GUI + remote server
cmos-camera-gui --ws-port 8765

# telescope service mode (no window, auto-connect, WSP identity)
python -m cmos_camera_gui --headless --ws-port 5566 \
       --auto-connect qhy --instrument summer
# (equivalently: --config summer.json)
```

## Remote control

```python
from cmos_camera_gui.client import CameraClient      # native protocol

with CameraClient("ws://localhost:8765") as cam:
    cam.connect_camera(0)
    cam.set(Exposure=50_000, Gain=200)               # exposure in µs
    cam.wait_for_state("READY")
    cam.capture_frames(20, directory="./captures", basename="demo",
                       combine="mean")
```

For WSP / telescope integration use the pirtcam-compatible client instead
(float seconds, ack-then-poll captures):

```python
from cmos_camera_gui.summer_client import SummerClient

cam = SummerClient(host="localhost", port=5566)
cam.connect()
cam.set_exposure(7.5)
cam.set_save_path("~/data/images/20260814/summer")
cam.capture_frames(filename="img_0001", nframes=1, object="M42",
                   observer="wsp", headers=[["FIELDID", 42, "card"]])
# poll cam.get_status()["data"]["is_capturing"] until False
```

## Tests

```bash
python -m pytest tests/    # no hardware required
```

## Docs

- [PLAN.md](PLAN.md) — architecture, phase history, WSP compliance map
- [docs/WspSummerDaemonHandoff.md](docs/WspSummerDaemonHandoff.md) — for
  the WSP-side daemon implementer
- [docs/HardwareFieldNotes.md](docs/HardwareFieldNotes.md) — measured
  camera/SDK quirks (QHY TEC readback, readout floors, ASI294 ROI stalls)
- [examples/](examples/) — notebooks for the native and WSP clients
