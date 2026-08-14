# CMOS Control GUI

Multi-vendor CMOS camera control: GUI + WebSocket remote command server.
Currently supports **ZWO ASI** cameras (direct ctypes wrapper around
`ASICamera2.dll`, no third-party `zwoasi` dependency); **QHYCCD** support
(QHY42PRO) is in progress — see `PLAN.md` and `HANDOFF.md`.

Every camera capability is driven through a headless `CameraController`, so
the GUI and the remote WebSocket connection operate any camera identically.

## Install

```bash
pip install .

# editable dev install
pip install -e .
```

## SDK

Download `ASICamera2.dll` (Windows) or `libASICamera2.so` (Linux) from
[ZWO developer downloads](https://www.zwoastro.com/software/) and either
place it on PATH or pass `--sdk /path/to/ASICamera2.dll`.

## Run

```bash
# as installed console script
cmos-camera-gui --sdk C:\path\to\ASICamera2.dll

# as module
python -m cmos_camera_gui --sdk C:\path\to\ASICamera2.dll

# with WebSocket command server --- use this one!
cmos-camera-gui --sdk ASICamera2.dll --ws-port 8765
```

## Remote control

```python
from cmos_camera_gui.client import CameraClient

with CameraClient("ws://localhost:8765") as cam:
    cam.connect_camera(0)
    cam.set(Exposure=50_000, Gain=200)
    cam.wait_for_state("READY")
    cam.capture_frames(20, directory="./captures", basename="demo")
```
