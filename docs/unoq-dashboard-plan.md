# UNO Q Backend and Dashboard Implementation Plan

## 1. Objective

Replace the provisional Python UNO Q pipeline with a production Rust service
that owns:

- USB CDC ingestion and reconnect handling.
- Byte-exact raw archival and replay.
- Protocol validation and canonical observations.
- Rolling SS-TWR and DS-TWR.
- CIR scaling, resampling, delay alignment, and phase alignment.
- Waterfall and FFT processing.
- Health monitoring and distance calibration.
- Versioned REST and FlatBuffers WebSocket APIs.
- A responsive Svelte/WebGL2 dashboard hosted by the UNO Q.

Retain the Python implementation as a non-production reference decoder and
differential-test oracle.

## 2. Hardware Baseline

Read-only inventory confirmed:

| Resource | UNO Q |
|---|---|
| Host | `chinny`, Arduino Imola |
| OS | Debian 13 ARM64 |
| CPU | 4-core Qualcomm Kryo-V2, up to 2.016 GHz |
| Memory | 3.6 GiB, approximately 2.9 GiB available |
| Home storage | 18 GiB, approximately 17 GiB available |
| Gateway | Stable by-ID path, currently `/dev/ttyACM1` |
| Rust/Node on UNO Q | Not installed |
| Docker | Installed |
| Development host | Rust 1.93.1, Node 24.18.0, npm 11.16.0 |

An obsolete dated dashboard currently occupies port `8080` through an
`@reboot` crontab entry. It will be fully disabled at final cutover without
modifying or deleting its frozen source files.

## 3. Repository Layout

```text
unoq/
  Cargo.toml
  Cargo.lock
  crates/
    heimdall-protocol/
    heimdall-dsp/
    heimdall-service/
  schemas/
    telemetry-v1.fbs
    openapi-v1.yaml
  dashboard/
    package.json
    package-lock.json
    src/
  migrations/
  systemd/
  heimdall/                 # retained Python reference
```

Use one production executable with subcommands:

```text
heimdall serve
heimdall replay
heimdall inspect
heimdall verify
heimdall benchmark
```

Source crates remain libraries inside one systemd-managed process, not
separate services.

## 4. Backend Architecture

```text
USB CDC
  -> mandatory archive writer
  -> USB framing and validation
  -> beacon fragment processing
  -> canonical observations
  -> per-link ranging and CIR DSP
  -> bounded live-state projection
  -> REST / FlatBuffers WebSocket
  -> Svelte/WebGL2 clients
```

Runtime task isolation:

- CDC discovery, reading, and reconnect handling.
- Raw archive writer with write acknowledgment.
- USB parser and semantic validator.
- Beacon reassembly and partial-fragment recovery.
- Canonical event ordering and deduplication.
- DSP workers partitioned by directed link.
- Metadata SQLite writer.
- API/static-file server.
- Supervisor, metrics, shutdown, and systemd watchdog.

Rules:

- Archive acceptance precedes parsing.
- Raw archive sync occurs at least once per second.
- Dashboard, API, DSP, and metadata backpressure never block CDC capture.
- Slow WebSocket clients are disconnected and resynchronize from a fresh
  snapshot.
- Archive failure pauses ingestion rather than silently processing unarchived
  bytes.
- All processing uses radio event order, never USB chunking or arrival wall
  time.

## 5. Protocol Compatibility

Implement USB CDC v1 and beacon v1 directly from the normative contracts.

Preserve:

- Every raw byte, including noise, corruption, unknown types, and partial
  tails.
- USB sequence numbers and gaps without renumbering.
- Connection and configuration epochs.
- Arbitrary read fragmentation.
- N=2 through N=8 and arbitrary valid `M`.
- Forty-bit timestamps and u32 `k` wrapping.
- Identical canonical fields for local and relayed observations.
- Byte-exact CIR and subreport evidence.

Correct known Python weaknesses rather than reproducing them silently:

- Validate the USB reserved byte.
- Separate unsupported-version accounting from CRC failures.
- Validate all `HELLO` dimensions and invariants.
- Apply identical semantic checks to local and relayed observations.
- Suppress duplicate/old records from processing while retaining raw evidence.
- Validate TX records, cycle summaries, flags, ownership, and dimensions.
- Clarify CFO units as `ratio = raw / 2^26` and
  `ppm = ratio * 10^6`.
- Recover complete CRC-valid subreports contained in surviving M=2 fragments.
- Flag recovered subreports with partial-report provenance.
- Define cycle-summary behavior at u32 wrap.

## 6. Raw Storage

The byte-exact `.husb` archive is the source of truth. Canonical and derived
products are regenerable and will not be duplicated permanently in SQLite.

Archive policy:

| Setting | Decision |
|---|---|
| Segment size | 8 MiB |
| Unprotected quota | 200 MB |
| Current retention | Approximately 18 minutes |
| Sync interval | At most one second of power-loss exposure |
| Quota behavior | Delete oldest closed, verified, unprotected segment |
| Protected data | May exceed 200 MB |
| Disk safety floor | Pause archival and alert at 20% free space |
| Active segment | Never deleted |
| Catalog requirement | Size and SHA-256 verified before deletion eligibility |

Metadata SQLite stores:

- Archive and connection catalogs.
- Segment hashes.
- Processing-setting epochs.
- Calibration epochs and rollback history.
- Node names and colors.
- Protected-clip metadata.
- Health thresholds and recent alerts.
- Software, firmware, configuration, and schema versions.

Protected clips:

- Save Capture Clip captures 30 seconds before and 30 seconds after
  activation.
- Produce a standalone replayable archive using complete USB-record
  boundaries.
- Accept an optional name and note.
- Export through REST/dashboard as an uncompressed ZIP containing raw data,
  manifest, hashes, metadata, firmware identities, and processing epochs.

Warm restart:

- Continue archiving immediately.
- Replay retained raw data in parallel.
- Restore five minutes of distance history.
- Restore 30 seconds of CIR, waterfall, FFT, and filter state.
- Merge replay state into the live event frontier without duplicates.

## 7. Ranging

### SS-TWR

Use adjacent opposite-direction observations only:

```text
A TX t1A -> B RX t2B
B TX t3B -> A RX t4A
```

```text
roundA = delta40(t4A, t1A)
replyB = delta40(t3B, t2B)
q = filtered_cfo_B_to_A

tof = (roundA - replyB * (1 - q)) / 2
```

CFO processing:

- Maintain independent state for every directed link.
- Use an event-time leaky integrator.
- Default half-life is 2 seconds.
- Global configurable range is 0.1-30 seconds.
- Initialize from the first valid sample.
- Reset on configuration changes or detected timestamp discontinuities.
- Record raw CFO, filtered CFO, half-life, and filter revision with each result.
- Do not bridge missing SS exchanges.

### DS-TWR

Use rolling alternating triples:

```text
A -> B, B -> A, A -> B
```

```text
tof = (roundA * roundB - replyA * replyB)
      / (roundA + roundB + replyA + replyB)
```

Requirements:

- Use checked `i128` interval products to avoid overflow.
- Allow one missed-message bridge.
- Limit first-to-third span to 100 ms.
- Never bridge configuration changes, resets, invalid timestamps, or uncertain
  identity.
- Attach bridge duration and reduced-confidence flags.
- Publish the result at the radio-relative midpoint of its evidence window.

### Shared Range Processing

- Use `299,702,547 m/s`.
- Retain all constituent observation identities and six/four timestamps.
- Preserve raw, calibrated, and smoothed distances.
- Show dashboard distances in centimeters.
- Retain integer millimeters in API records.
- Display raw invalid/negative points distinctly.
- Offer browser controls to show, hide, or clamp invalid points.

Filtering:

- Hampel rejection defaults to a 1-second window and 3 MAD.
- Hampel window is configurable from 0.25-5 seconds.
- Hampel threshold is configurable from 2-6 MAD.
- Smoothed distance is a causal moving average.
- Moving-average default is 1 second.
- Moving-average range is 1-30 seconds.
- Raw and smoothed DS and SS traces appear together.

## 8. Distance Calibration

Add a final **Distance Calibration** tab.

Workflow:

1. Press Snapshot Distances.
2. Collect ten seconds of stationary DS-TWR measurements.
3. Compute per-pair means, variances, counts, and quality.
4. Select a board pair from an upper-triangle matrix.
5. Enter the tape-measured distance.
6. Display current matrix rank and condition.
7. Recommend the next pair that improves rank or conditioning most.
8. Enable solving only after full rank is reached.
9. Permit additional measurements for an overdetermined solution.
10. Preview offsets, uncertainty, residuals, and before/after distances.
11. Explicitly apply a versioned calibration epoch or cancel.
12. Support rollback to an earlier epoch.

Solve one effective offset per board:

```text
measured_ij - tape_ij = offset_i + offset_j
```

Solver behavior:

- Require full measurement-matrix rank.
- Detect connected bipartite/rank-deficient layouts.
- Weight measurements using UWB snapshot variance only.
- Use regularized weighted least squares.
- Choose regularization automatically from variance and conditioning.
- Expose regularization strength and sensitivity before Apply.
- Warn when residual RMSE exceeds a configurable 5 cm default.
- Preserve unconstrained evidence and do not silently hide poor fits.
- Apply corrections host-side only.
- Do not alter firmware antenna-delay registers.
- Retain the firmware delays and effective host overrides in provenance.

## 9. CIR Processing

Every directed link has independent state and references.

Amplitude processing:

- Default to accumulation-normalized dB.
- Apply optional DGC compensation.
- Offer raw accumulator-dB display.
- Preserve aligned raw samples as plot markers.
- Overlay the 16x resampled curve.

Resampling:

- Kaiser-windowed sinc FIR.
- Kaiser beta `8.6`.
- Half-width ten samples.
- Sixteen phases per original tap.
- Non-circular boundaries.
- Golden tests for integer exactness, fractional impulses, sinusoids, edges,
  and lag sign.

Alignment:

- Fractional-delay alignment.
- Common complex-phase alignment.
- One-second default delay half-life.
- One-second default phase half-life.
- Time-aware smoothing based on radio event intervals.
- Global settings, not per-link controls.

Reference modes:

- First valid CIR.
- Qualified reference epochs, default.
- Continuously adaptive template.

Qualified mode:

- Select a high-quality initial reference.
- Default reacquisition threshold is correlation below 0.90 for five seconds.
- Threshold and duration are configurable.
- Record every reference epoch.

Adaptive mode:

- Default template half-life is ten seconds.
- Configurable range is 1-60 seconds.

Analytical gaps remain gaps. Display-only interpolation must be flagged and
must never enter scientific FFT/ranging calculations.

## 10. Waterfall and FFT

### CIR Waterfall

- Selectable 1-30 second history.
- Processed per directed link.
- Support normalized/raw scale.
- Support clutter removal, nuisance fitting, noise clipping, DGC correction,
  outlier display, and fixed/automatic color ranges.
- Apply optional path-loss compensation.
- Use smoothed DS distance first.
- Fall back to the corresponding smoothed directional SS distance.
- Disable compensation if neither is valid.

### Slow-Time FFT

- Transform successive aligned CIR measurements at every tap.
- Selectable complex or magnitude input.
- Always display 0 Hz through Nyquist.
- Selectable 1-30 second history.
- Support optional DC/static-clutter removal.
- Interpolate no more than two consecutive missing samples.
- Require no more than 10% interpolated samples per window.
- Attach filled-sample percentage and quality state.
- Offer rectangular, Hann, Hamming, and Blackman windows.
- Use Hann by default.
- Apply path-loss compensation when enabled.

### Fast-Time FFT

- Transform each latest 64-tap complex CIR across delay.
- Display channel-frequency-response magnitude by default.
- Provide a phase toggle.
- Offer the same selectable FFT windows with Hann default.
- State explicitly that this is channel frequency response, not a range FFT.

All expensive results are cached per global processing epoch and parameter
set. They update only on new observations, not on browser animation frames.

## 11. API

Serve from:

```text
http://192.168.8.215:8080
```

Security decision:

- Trusted LAN.
- No authentication.
- Document that any LAN client can read data and change settings.

REST v1:

- Health and topology.
- Node metadata.
- Current settings and processing epochs.
- Recent distances and CFO.
- Calibration snapshots, solves, apply, and rollback.
- Protected clips, download, and deletion.
- Archive status and storage health.
- OpenAPI documentation.

FlatBuffers WebSocket v1:

- Versioned envelope.
- Stream sequence and dropped-event count.
- Configuration and processing epoch IDs.
- Initial snapshot followed by deltas.
- Topic subscriptions for health, range, CIR, waterfall, slow FFT, fast FFT,
  CFO, and calibration.
- Compact typed numeric arrays.
- Bounded per-client queues.
- Snapshot resynchronization after reconnect or overflow.

Support five concurrent clients with different active tabs.

## 12. Dashboard

Technology:

- Svelte and TypeScript.
- Custom WebGL2 renderer.
- Lightweight non-WebGL health fallback.
- Refined dark technical-instrument design.
- Current Chrome, Edge, Firefox, Safari, iOS, and Android browsers.
- Modern x86 integrated-GPU and Snapdragon X Elite laptops.
- Modern phones in portrait and landscape.

Performance:

- Under 100 ms observation-to-visible-update target.
- 30 FPS rendering.
- Full active-tab update rate.
- Inactive tabs unsubscribe or throttle.
- N=8 acceptance with 56 directed links.
- Five simultaneous clients.

Desktop layout:

- Packed `N x (N-1)` grid.
- N=5 uses 5x4.
- N=8 uses 8x7.
- Every tile uses explicit `transmitter -> observer` labels.
- Clicking a tile opens a larger focus overlay.

Phone layout:

- Compact network/link overview.
- One selected readable link plot.
- Tap or swipe to change links.
- No primary-page scrolling.

No-scroll applies to primary tabs. Focus overlays and advanced-control drawers
may scroll internally.

Tab order:

1. Network Health
2. Live Distance
3. Instantaneous CIR
4. CIR Waterfall
5. Slow-Time FFT
6. Fast-Time FFT
7. CFO
8. Distance Calibration

Network Health includes:

- Radio delivery, rates, misses, age, and partial reports.
- Signal quality, first-path/channel power, DGC, and alignment score.
- Gateway state, USB gaps/drops, CRC and callback margin.
- Node state, firmware/configuration identity, synchronization, and reset
  detection.
- Service uptime, CPU, memory, storage, archival, and API-client count.
- In-page severity indicators and bounded event log.
- Documented global warning/error controls.

Display settings remain browser-local. Signal-processing changes create
persisted backend epochs shared by all clients.

## 13. Performance Strategy

Use the UNO Q's four cores deliberately:

- One latency-sensitive archive/ingest path.
- One protocol/canonical path.
- Two-worker bounded DSP pool, benchmark-adjusted.
- Tokio API and metadata tasks remain asynchronous.
- FFT plans and interpolation kernels are reused.
- Store aligned 64-tap histories, not permanently expanded 16x matrices.
- Update waterfall textures by row delta.
- Share computed products between clients.
- Never recompute unchanged data at animation-frame rate.

Acceptance load uses the contractual maximum, not only current N=5:

- Up to N=8.
- Up to approximately 433 kB/s modeled USB traffic.
- Fifty-six directed links.
- Five clients.
- Active heavy tabs at full update rate.
- No raw loss caused by DSP/API pressure.

## 14. Tooling and Build

Before installing or downloading:

- Inspect `tools/installers/`.
- Pin Rust, Node, npm, Svelte, FlatBuffers, and frontend dependencies.
- Record version, architecture, official URL, and SHA-256 in
  `tools/README.md`.
- Cache redistributable ARM64 archives or OCI images under ignored
  `tools/installers/linux-arm64/`.

Build approach:

- Build Svelte assets reproducibly.
- Generate FlatBuffers Rust/TypeScript bindings.
- Build a native Debian ARM64 Rust binary using a pinned ARM64
  container/toolchain.
- Embed hashed frontend assets in the binary.
- Deploy only the binary, configuration, and systemd unit.
- Do not require Rust or Node on the production UNO Q.

## 15. Verification

### Compatibility

- Keep all existing 87 Python tests passing.
- Add a small checked-in hardware-derived `.husb` fixture.
- Compare Rust and Python canonical fingerprints.
- Test every input split point and randomized chunking.
- Document intentional differences such as partial-report recovery.
- Verify live and replay outputs independently of chunking and wall time.

### Ranging

- u40 wrap and bounded differences.
- u32 `k` wrap.
- CFO sign, ratio, ppm, half-life, gaps, and directional independence.
- SS pair order for every schedule separation.
- DS unequal delays, skew, one-message bridging, and 100 ms limit.
- `i128` overflow and cancellation vectors.
- Calibration epochs and raw evidence preservation.

### DSP

- Resampling impulse and sinusoid vectors.
- Fractional lag and phase sign.
- Boundary and truncation handling.
- Per-link reference isolation.
- Waterfall gap masks.
- FFT tone/bin/window/coherent-gain vectors.
- Python/JavaScript prototype comparison where behavior is intentional.

### Runtime

- CDC disconnect/reconnect.
- SIGTERM and power-loss-tail recovery.
- Disk full and 20% safety floor.
- Circular pruning.
- Protected clips and ZIP export.
- Slow WebSocket clients.
- Task panic/failure supervision.
- Warm-start replay.

### Browser

- 1080p no-scroll N=5 and N=8 layouts.
- Snapdragon X Elite and x86 integrated graphics.
- Phone portrait and landscape.
- Five concurrent clients.
- WebGL context loss.
- Under-100 ms latency and 30 FPS acceptance.

## 16. Implementation Order

### Phase 1: Plan and Contracts

- Correct CFO units and sign conventions.
- Specify derived range and calibration records.
- Specify partial-fragment recovery.
- Add OpenAPI and FlatBuffers v1 schemas.
- Update `.gitignore` and tooling manifest.

### Phase 2: Rust Compatibility Core

- Implement timestamp types, CRC, USB parser, beacon validation, and canonical
  observations.
- Add fixtures, property tests, and differential Python verification.
- Preserve Python as the independent reference.

### Phase 3: Archive and Runtime

- Implement raw-first 8 MiB segments, one-second sync, catalog, quota, clips,
  replay, and warm start.
- Implement reconnecting CDC service and bounded task supervision.
- Add metadata SQLite.

### Phase 4: Ranging and Calibration

- Implement directional CFO filters and SS-TWR.
- Implement rolling/bridged DS-TWR.
- Implement Hampel and moving-average traces.
- Implement effective board-offset calibration and epochs.
- Validate against synthetic vectors and existing primitive evidence.

### Phase 5: CIR and Spectral DSP

- Port and test resampling/alignment.
- Add reference modes and processing epochs.
- Add waterfall, slow FFT, fast FFT, and path-loss processing.
- Benchmark N=8 on `chinny` before API/UI integration.

### Phase 6: API

- Implement REST v1.
- Implement FlatBuffers WebSocket subscriptions.
- Add bounded clients, snapshots, deltas, and metrics.
- Load-test five clients.

### Phase 7: Dashboard

- Build the eight Svelte/WebGL2 tabs.
- Implement desktop, phone, focus, and control-drawer layouts.
- Add calibration workflow and clip management.
- Verify all target browsers and hardware.

### Phase 8: Deployment and Cutover

- Build the reproducible ARM64 artifact.
- Install the Heimdall systemd unit.
- Disable the obsolete dashboard crontab launcher and stop its process.
- Start Heimdall on port `8080`.
- Run reconnect, archive, replay, performance, and browser acceptance.
- Record UNO Q software version and results in `STATUS.md`.

### Phase 9: Final Qualification

- Run a sustained N=5 hardware soak.
- Run synthetic N=8 maximum-load qualification.
- Confirm no steady-state USB or internal queue drops.
- Verify raw archive/replay equivalence.
- Review displayed CIR, SS, DS, and calibration behavior against physical
  measurements.
- Only then designate Rust as production and Python as reference-only.

## 17. Primary Risk Gate

The N=8, 56-link, five-client, full-rate FFT requirement is a benchmark gate,
not an assumption. The UNO Q has adequate memory, but actual DSP throughput must
be measured before frontend completion. If it does not pass, optimize the
backend and data representation rather than weakening raw capture guarantees.
