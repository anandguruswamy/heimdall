# Heimdall Radar Map

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
clutter baseline.

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

Outputs are `volume.npy`, `confidence.npy`, and `metadata.json`. Add `--zarr`
to also write `volume.zarr` when the optional `zarr` package is installed.
Use `--clutter-frames 0` to map mean magnitude without static subtraction.
Configuration or USB-sequence restarts are never combined. Parser loss,
post-HELLO rejects, and incomplete boundary reports are recorded in metadata;
add `--require-complete-stream` to reject a capture containing any of them.

## Serve Slices

```powershell
python -m radar_map serve output --host 127.0.0.1 --port 8765
```

Endpoints:

- `GET /api/v1/health`
- `GET /api/v1/metadata`
- `GET /api/v1/slices/xy?index=0`
- `GET /api/v1/slices/xz?index=0`
- `GET /api/v1/slices/yz?index=0`

Slice arrays use the axis order stated in each response. The stored volume is
always `(z, y, x)`.

## Dependencies

The required runtime is Python 3.10 or newer with NumPy 2.2.6. NumPy 2.2.6 has
no native Windows ARM64 wheel; the validated laptop environment is CPython
x86-64 under Windows ARM64 emulation. The server uses only the Python standard
library. Zarr is deliberately optional until its storage contract is selected
and pinned.
