# Vendor SDK libraries

Place vendor camera SDK libraries here; the app searches this directory
recursively at startup.

## QHYCCD (QHY42PRO etc.)

Drop `qhyccd.dll` (Windows x64) anywhere under this directory, e.g.:

```
sdk/qhyccd.dll
```

Recent SDK builds (26.x) need only the single DLL. Older All-In-One
builds ship companion DLLs (`tbb.dll`, `ftd2xx.dll`) — copy the whole
folder in that case.

Alternatively set the environment variable `QHYCCD_SDK_DLL` to the full
path of the library.

Note: no hotplug — connect the camera before launching the app. If you
plug it in later, restart the app (a re-scan requires re-initialising
SDK resources).

## ZWO ASI

`ASICamera2.dll` is found via the `--sdk` flag, the Browse SDK button,
or the ASIStudio install locations; it can also be placed on PATH.
