# Camera GUI (ASI + QHY42)

Streaming and FITS recording GUI with two backends:
- ASI (ZWO) via direct ctypes wrapper around `ASICamera2.dll`.
- QHY42 via the `qcam` Python wrapper and `qhyccd` SDK.

## Install

```bash
# core only (PyQt5 + numpy)
pip install .

# with FITS recording
pip install ".[fits]"

# with WebSocket remote scripting
pip install ".[ws]"

# everything
pip install ".[all]"

# editable dev install
pip install -e ".[all]"
```

## SDK

Download `ASICamera2.dll` (Windows) or `libASICamera2.so` (Linux) from
[ZWO developer downloads](https://www.zwoastro.com/software/) and either
place it on PATH or pass `--sdk /path/to/ASICamera2.dll`.

For QHY42, install your QHY SDK (`qhyccd.dll`/`libqhyccd.so`) and the Python
wrapper used by your setup (`qcam`). Then start with `--backend qhy` and
optionally pass `--qhy-sdk /path/to/qhyccd.dll`.

## Run

```bash
# as installed console script
zwo-camera-gui --sdk C:\path\to\ASICamera2.dll

# as module
python -m zwo_camera_gui --sdk C:\path\to\ASICamera2.dll

# QHY42 backend
python -m zwo_camera_gui --backend qhy --qhy-sdk C:\path\to\qhyccd.dll

# with WebSocket command server
zwo-camera-gui --sdk ASICamera2.dll --ws-port 8765
```
