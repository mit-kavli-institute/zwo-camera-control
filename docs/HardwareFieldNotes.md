# Hardware / SDK Field Notes (measured)

Quirks measured during the 2026-08-14 multi-vendor port, on real hardware.
These belong in any future port's handoff (the QHY items should be folded
into the master QHYCCD N-Frame Grabber handoff doc — they extend its §7/§10).

## QHY42PRO (SDK V20260625_16)

1. **CURPWM readback is only valid immediately after `ControlQHYCCDTemp`.**
   The register reads 0 the rest of the time, even while the TEC is
   audibly regulating and CURTEMP tracks correctly. Consequence: the
   handoff's "distrust temperature while PWM == 0" rule discards *valid*
   temperatures. Working recipe (implemented in `vendors/qhy/vendor.py`):
   read CURPWM right after every keep-alive / `set_cooler` and cache it;
   detect the genuinely-frozen idle temperature readout by its signature
   instead — several consecutive *exactly identical* CURTEMP reads with
   the TEC off.
2. **CURTEMP is live and trustworthy whenever regulation is active** —
   it tracked a full warm-up and cool-down smoothly while CURPWM read 0.
3. **The "drain until 0.4 s of silence" stale-DDR rule never terminates at
   short exposures** (fresh frames arrive continuously every ~45-150 ms).
   Stale frames burst out back-to-back: drain until the first ~0.35 s gap
   OR a ~1 s hard cap, whichever comes first.
4. **Readout floor: 42 ms/frame full-frame 16-bit** (23.7 fps),
   slightly better than the handoff's ~45 ms estimate.
5. **`CONTROL_USBTRAFFIC` has no effect on a healthy USB path** — swept
   60→0 in 9 steps at 1 ms exposure: identical 42.2 ms/frame cadence with
   zero GPS-seq drops at every value. The camera is sensor-readout-limited;
   the knob only matters on marginal USB chains (raise it if drops appear).
6. **Re-issuing the exposure (even unchanged) at record start aborts the
   in-flight frame and re-times the sensor.** Measured: first kept frame
   lands at T + ~65-100 ms after the record command *without* any idle-
   exposure switching — the handoff's idle-at-1ms recipe is therefore not
   needed for grab latency, only for between-grab duty-cycle control.
7. **Exposure-switch transitions confirmed:** 2-3 short/hybrid frames plus
   exactly one GPS seq skip per switch, all rejected by the record gate
   (kept frames carried consecutive seq numbers in every hardware run).
8. TEC cooldown reference: setpoint 0 °C from warm engages at PWM 255 and
   tapers (255 → 235 → 171 within ~20 s); GUI-side keep-alive every ~3 s.
   Setpoint clamp: −45…+20 °C. Instant off (`MANULPWM = 0`) — no ramp
   (decision: not needed for this sensor).

## ZWO ASI294MM Pro

9. **Sub-frame ROIs stall the video stream.** 1024×1024 RAW16 streaming
   showed multi-second inter-frame gaps and SDK-level dropped frames;
   full frame is rock-solid at its 476 ms readout cadence. Suspected
   quad-bayer readout/SDK interaction. Full frame is the validated path.
10. **`ASISetROIFormat` mid-stream freezes the pipeline for seconds** —
    never re-push an unchanged ROI (the controller caches the applied ROI
    for this reason).
11. **The SDK serializes calls per camera**: any call from a second thread
    blocks behind an in-flight `ASIGetVideoData` for up to a full
    exposure. One thread must own the SDK while streaming (same contract
    as the QHY method); all cross-thread work goes through the capture
    thread's command queue.
12. **The SDK buffers 1-2 in-flight frames**: a record command right after
    an exposure change otherwise captures frames exposed *before* the
    command at the old exposure (measured: first recorded frame arrived on
    the old cadence). Same class of problem as the QHY transition frames;
    fixed by the shared record gate.
13. **`ASIGetCameraProperty` requires a prior `ASIGetNumOfConnectedCameras`**
    enumeration in the same process, or it returns `INVALID_INDEX`.
