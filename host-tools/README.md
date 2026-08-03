# Host Tools

Tools here operate on USB CDC captures and decoded records. They must not be
required by the radio firmware and should be usable from Windows for bench
diagnostics.

- `radar-map/`: replay-only 3D bistatic backprojection and slice API.
- `radar-map/analyze_pulse_response.py`: align hardware CIRs to the fractional
  first-path marker and quantify pre-first-path response against the noise floor.
