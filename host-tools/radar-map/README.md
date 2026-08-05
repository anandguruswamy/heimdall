# Heimdall Radar Map

> **RETIRED (2026-08-05).** This Windows-host replay tool is no longer
> maintained. Live bistatic mapping now runs in the UNO Q dashboard as the
> **Radar Map** tab, which backprojects the aligned CIRs from the
> `instantaneous-cir` stream directly (no raw CIR blob/DGC scaling needed).
> This directory is retained as reference-only; do not build on it for new
> work. The content below describes the frozen behaviour.

This experimental Windows-host tool replays `.husb` captures into a 3D
bistatic backprojection volume. The UNO Q remains the system of record and owns
live USB ingestion, validation, archival, fusion, and its dashboard.

## Processing model

For each directed transmitter/receiver link and voxel, the mapper evaluates:

```text
excess_path = |voxel - transmitter| + |voxel - receiver| - |transmitter - receiver|
```

The excess path is converted to CIR taps without the monostatic divide by two.
CIRs are DGC/accumulation scaled, aligned to their reported first path, and
combined after rejecting invalid records and batch outliers in absolute first
path, CIR start offset, normalized correlation, and relative energy. By
default, the first 16 accepted frames on each link form a complex median static
clutter baseline for the motion product. A separate static product uses robust
aligned magnitude and masks the direct-path guard interval instead of removing
persistent reflections.

The quality gate calls the known approximately 72-sample CIA jump
`false_first_path`; `fp_valid` alone is not sufficient for admission.

## Geometry

Copy `geometry.example.json` to a local file and replace every coordinate with
surveyed antenna phase-centre coordinates. Coordinates are metres in an
explicit right-handed local frame. Browser solver fit consistency is not a
substitute for surveyed geometry or antenna-delay calibration.

## Build A Volume

Run from `host-tools/radar-map` (or add that directory to `PYTHONPATH`):

```powershell
python -m pip install -r requirements.txt
python -m radar_map build capture.husb geometry.json output `
  --bounds 0 4 0 4 0 2.5 --spacing 0.1
```

Outputs are motion `volume.npy`, `static-volume.npy`, shared `confidence.npy`,
and `metadata.json`. Add `--zarr` to also write `volume.zarr` when the optional
`zarr` package is installed. `--clutter-frames` controls the motion baseline;
`--direct-path-guard-taps` masks LOS energy from the static environment product.
Configuration or USB-sequence restarts are never combined. Parser loss,
post-HELLO rejects, and incomplete boundary reports are recorded in metadata;
add `--require-complete-stream` to reject a capture containing any of them.

## Open The Viewer

```powershell
python -m radar_map serve output --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/`. The dependency-free browser viewer provides an
orbitable WebGL point cloud, linked XY/XZ/YZ heatmaps, static/motion selection,
canonical top and 3D cameras, percentile and point-size controls, board markers,
run diagnostics, and an explicit warning when geometry is not surveyed.

Endpoints:

- `GET /`
- `GET /api/v1/health`
- `GET /api/v1/metadata`
- `GET /api/v1/points?product=static&percentile=85&limit=50000`
- `GET /api/v1/slices/xy?index=0`
- `GET /api/v1/slices/xz?index=0`
- `GET /api/v1/slices/yz?index=0`

Point-cloud responses contain `[x_m, y_m, z_m, magnitude, confidence]`. Point
and slice requests accept `product=motion|static`. Slice arrays use the axis
order stated in each response. Stored volumes are always `(z, y, x)`.

## Dependencies

The required runtime is Python 3.10 or newer with NumPy 2.2.6. NumPy 2.2.6 has
no native Windows ARM64 wheel; the validated laptop environment is CPython
x86-64 under Windows ARM64 emulation. The server uses only the Python standard
library. Zarr is deliberately optional until its storage contract is selected
and pinned.
