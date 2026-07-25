# Radio Firmware Baseline

This is the copied, proven Phase 1 open-radio application from the workspace's
UWB bring-up. It is intentionally preserved as a baseline while Heimdall's
custom beacon and gateway profiles are implemented.

The application proves DWM3001 access, DEV_ID, IRQ-driven traffic, 64-tap CIR
reads, scheduled TX, native USB CDC plumbing, and reversible 8 MHz/32 MHz SPI
profiles. It is not yet Heimdall-specific: beacon v0 and USB CDC v0 are not
wired into the C structs, and node/gateway profiles are not yet split.
