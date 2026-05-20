# Nikon Ti2 ZDrive timing test

This folder contains a standalone Nikon Ti2 ZDrive timing tool. It uses the
Nikon Ti2 SDK DLL directly through Python `ctypes` and does not depend on the
SIM GUI.

## Safety

The script only moves hardware when `--execute` is present. Run the dry run
first and confirm the printed Z targets are safe for the current objective,
sample, and focus state.

Default motion settings:

- `iZPOSITIONSpeed = 1`, Nikon's 2.50 mm/s Z speed preset.
- `iZPOSITIONTolerance = 0`, Nikon's strictest Z position tolerance preset.
- Step size is `0.3 um` (300 nm).
- Ten measured moves are performed after moving to the start position.
- The exposure-ready point is the return of `MIC_DataSet(..., wait=True)`.
- No fixed post-move sleep is inserted and no post-move `MIC_DataGet` polling is
  performed.

By default the start position is `50 um` and the scan direction is
`increasing`. If your microscope's coordinate direction does not match "toward
the sample", use `--direction decreasing` or specify `--start-um`.

## Dry run

```powershell
.\.venv\Scripts\python.exe z-scan\z_scan_timing.py
```

The dry run opens the SDK, reads the Z range, prints the start position and ten
target positions, then exits without moving the ZDrive.

## Hardware run

```powershell
.\.venv\Scripts\python.exe z-scan\z_scan_timing.py --execute
```

Useful overrides:

```powershell
.\.venv\Scripts\python.exe z-scan\z_scan_timing.py --execute --direction increasing --step-um 0.3 --steps 10
.\.venv\Scripts\python.exe z-scan\z_scan_timing.py --execute --start-um 1000.0 --runs 3
.\.venv\Scripts\python.exe z-scan\z_scan_timing.py --execute --return-to-start
```

Results are written under `z-scan/results/` as both CSV and JSONL.
If `MIC_DataSet` returns a non-zero SDK code, the script writes the failed step
record and stops before sending the next measured move.

## Timing fields

Each measured movement writes one record.

- `sdk_call_ms`: time from just before calling `MIC_DataSet` until
  `MIC_DataSet` returns.
- `command_to_exposure_ready_ms`: same timing as `sdk_call_ms`; it marks the
  point where the layer-scan controller should trigger camera exposure.
- `step_cycle_ms`: measured interval from the current move command to the next
  move command when another measured step follows; for the last step in a run,
  it is the `MIC_DataSet` call-return duration.
- `exposure_triggered`: whether the measured move reached the exposure-ready
  point. It is `false` only when `MIC_DataSet` returns a non-zero SDK code.
- `sdk_retcode`: raw return code from the measured `MIC_DataSet` call.

For layer scanning, use `MIC_DataSet(..., wait=True)` return as the exposure
trigger point. The timing records keep this as `sdk_call_ms` and
`command_to_exposure_ready_ms` for clarity.
