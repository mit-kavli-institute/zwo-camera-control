# ZWO Camera GUI

ZWO ASI camera streaming stress-test. Direct ctypes wrapper around
`ASICamera2.dll` — no third-party `zwoasi` dependency.

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

## Run

```bash
# as installed console script
zwo-camera-gui --sdk C:\path\to\ASICamera2.dll

# as module
python -m zwo_camera_gui --sdk C:\path\to\ASICamera2.dll

# with WebSocket command server
zwo-camera-gui --sdk ASICamera2.dll --ws-port 8765
```
