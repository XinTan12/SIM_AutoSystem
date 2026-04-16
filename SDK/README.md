# SDK Layout

This repository does not track vendor SDK packages or drivers under `SDK/`.
Keep the required vendor files in this directory locally on each machine.

## Required components

1. Hamamatsu `DCAM-API` / `DCAM-SDK4` for the ORCA-Fusion BT camera
2. Forth Dimension Displays / Kopin `R11CommLib` bundle for the `QXGA-R11-STR` SLM
3. NI `NI-DAQmx` driver for the `USB-6423`

## Official sources

- Hamamatsu DCAM driver/software hub:
  https://www.hamamatsu.com/us/en/product/cameras/software/driver-software.html
- Hamamatsu DCAM-SDK4 page:
  https://www.hamamatsu.com/us/en/product/cameras/software/driver-software/dcam-sdk4.html
- Kopin / Forth Dimension Displays support entry:
  https://www.kopin.com/forth-dimension-displays-redirect/
- Kopin contact page for SLM / FLCoS support:
  https://www.kopin.com/about/contact/
- NI device drivers download page:
  https://www.ni.com/en/support/downloads/drivers/download.ni-device-drivers.html
- NI Python resources for DAQ hardware:
  https://www.ni.com/en/support/documentation/supplemental/16/python-resources-for-ni-hardware-and-software.html

## Expected local layout

The current code in `sim_control/adapters.py` looks for these default paths when the
backend SDK path fields are left empty.

### Hamamatsu camera SDK

Place the DCAM Python sample directory here:

`SDK/Hamamatsu_DCAMSDK4_v25056964/dcamsdk4/samples/python/`

That directory must contain `dcam.py` and `dcamapi4.py`.

### Kopin / FDD SLM SDK

Place the R11 DLLs here:

`SDK/R11 CD Bundle Mar 2020/2020-03/Software/R11CommLib/R11CommLib-1.8.189.118/examples/msvc/lib/`

The code expects one of:

- `R11CommLib-1.8-x64.dll`
- `R11CommLib-1.8-x86.dll`

The `R11CommLib` package is usually vendor-supplied rather than a public
self-service download. If you do not already have it, use the Kopin contact
page above and request the current Windows package for the `QXGA-R11-STR`.

### NI DAQ driver

Install `NI-DAQmx` on Windows so the Python `nidaqmx` package can talk to the
`USB-6423`.

## Optional path override

If your local SDK packages live somewhere else, set these fields in
`config/sim_control_config.json` or through the GUI settings dialog:

- `backend.fusion_bt_sdk_path`
- `backend.slm_sdk_path`
