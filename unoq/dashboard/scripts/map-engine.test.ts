import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DEFAULT_MAP_CONFIG,
  DEFAULT_RECONSTRUCTION_CONFIG,
  METRES_PER_TAP,
  backproject,
  boundsFromPositions,
  buildGrid,
  buildLinkProfiles,
  estimateNoiseFloor,
  estimateSoftBackground,
  geometryFromBoardFreeze,
  geometryFromBoardPositions,
  reconstruct,
  volumeToPoints,
  type LinkProfile,
  type MapGeometry,
  type Vec3,
} from '../src/lib/map/map-engine.ts';
import { nearestSamples, parseDatasetZip, type DatasetSample } from '../src/lib/map/dataset.ts';

const gaussian = (length: number, centre: number, sigma: number, height: number): Float32Array => {
  const output = new Float32Array(length);
  for (let i = 0; i < length; i++) {
    const d = (i - centre) / sigma;
    output[i] = height * Math.exp(-0.5 * d * d);
  }
  return output;
};

const distance = (a: Vec3, b: Vec3) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);

function framesForLink(
  from: number,
  to: number,
  nodes: Vec3[],
  reflector: Vec3,
  marker = 4
): unknown[] {
  const tx = nodes[from];
  const rx = nodes[to];
  const direct = distance(tx, rx);
  const excess =
    distance(reflector, tx) + distance(reflector, rx) - direct;
  const tap = excess / METRES_PER_TAP;
  const resampled = gaussian(497, marker * 16, 6, 100);
  const echo = gaussian(497, (marker + tap) * 16, 6, 8);
  for (let i = 0; i < 497; i++) resampled[i] += echo[i] + (Math.random() - 0.5) * 0.2;
  const magnitude = new Float32Array(32);
  const frames: unknown[] = [];
  for (let n = 0; n < 8; n++) {
    frames.push({ from, to, marker_aligned: marker + (Math.random() - 0.5) * 0.4, correlation: 0.92, match_score: 0.85, quality: 0, magnitude, resampled });
  }
  for (let n = 0; n < 2; n++) frames.push({ from, to, marker_aligned: marker, correlation: 0.1, match_score: 0.1, quality: 0, magnitude, resampled });
  frames.push({ from, to, marker_aligned: marker + 6, correlation: 0.9, match_score: 0.85, quality: 0, magnitude, resampled });
  return frames;
}

const nodes: Vec3[] = [{ x: 0, y: 0, z: 0 }, { x: 4, y: 0, z: 0 }, { x: 2, y: 3, z: 0 }];
const reflector: Vec3 = { x: 2, y: 1.2, z: 0.5 };
const geometry: MapGeometry = {
  positions: nodes,
  revision: 'test',
  provenance: { calibration_status: 'surveyed-calibrated' },
};

function buildProfiles(): LinkProfile[] {
  const config = { ...DEFAULT_MAP_CONFIG, frames: 64 };
  const profiles: LinkProfile[] = [];
  for (let from = 0; from < 3; from++) {
    for (let to = 0; to < 3; to++) {
      if (from === to) continue;
      const profile = buildLinkProfiles(framesForLink(from, to, nodes, reflector), config);
      assert.ok(profile, `link ${from}>${to} produced no profile`);
      assert.equal(profile.acceptedFrames, 8);
      profiles.push(profile);
    }
  }
  return profiles;
}

function decodeIndex(index: number, shape: [number, number, number]) {
  const [nz, ny, nx] = shape;
  const iz = Math.floor(index / (ny * nx));
  const remainder = index - iz * ny * nx;
  const iy = Math.floor(remainder / nx);
  const ix = remainder - iy * nx;
  return { ix, iy, iz };
}

test('backprojects a synthetic reflector to its surveyed location', () => {
  const profiles = buildProfiles();
  const grid = buildGrid(geometry, 0.1);
  const volume = backproject(profiles, geometry, grid);
  let best = -Infinity;
  let bestIndex = -1;
  for (let i = 0; i < volume.volume.length; i++) {
    if (volume.volume[i] > best) { best = volume.volume[i]; bestIndex = i; }
  }
  assert.ok(bestIndex >= 0, 'volume contains energy');
  const { ix, iy, iz } = decodeIndex(bestIndex, grid.shape);
  const at: Vec3 = {
    x: grid.min[0] + ix * grid.spacing[0],
    y: grid.min[1] + iy * grid.spacing[1],
    z: grid.min[2] + iz * grid.spacing[2],
  };
  const mirror: Vec3 = { x: reflector.x, y: reflector.y, z: -reflector.z };
  const peakError = Math.min(distance(at, reflector), distance(at, mirror));
  assert.ok(peakError < 0.4, `peak at (${at.x},${at.y},${at.z}) error ${peakError}`);
});

test('percentile point cloud retains the reflector peak', () => {
  const profiles = buildProfiles();
  const grid = buildGrid(geometry, 0.1);
  const volume = backproject(profiles, geometry, grid);
  const cloud = volumeToPoints(volume, 85);
  assert.ok(cloud.points.length > 0);
  assert.ok(cloud.valueRange[1] > 0);
  const brightest = cloud.points.reduce((best, point) => (point.magnitude > best.magnitude ? point : best));
  const mirror: Vec3 = { x: reflector.x, y: reflector.y, z: -reflector.z };
  const error = Math.min(
    distance({ x: brightest.x, y: brightest.y, z: brightest.z }, reflector),
    distance({ x: brightest.x, y: brightest.y, z: brightest.z }, mirror)
  );
  assert.ok(error < 0.5, `brightest point error ${error}`);
});

test('rejects weak-correlation frame sets', () => {
  const frames = Array.from({ length: 8 }, () => ({
    from: 0, to: 1, marker_aligned: 4, correlation: 0.2,
    magnitude: new Float32Array(32), resampled: new Float32Array(497),
  }));
  assert.equal(buildLinkProfiles(frames, DEFAULT_MAP_CONFIG), null);
});

test('rejects marker-inconsistent frame sets', () => {
  const markers = [0, 2, 4, 6, 8, 10, 12, 14];
  const frames = markers.map((marker, index) => ({
    from: 0, to: 1, marker_aligned: marker, correlation: 0.9,
    magnitude: new Float32Array(32), resampled: new Float32Array(497),
    round: index,
  }));
  assert.equal(buildLinkProfiles(frames, DEFAULT_MAP_CONFIG), null);
});

test('uses native magnitude when resampled is absent', () => {
  const config = { ...DEFAULT_MAP_CONFIG, frames: 64 };
  const marker = 4;
  const tap = 2;
  const magnitude = gaussian(32, marker + tap, 0.5, 8);
  const frames = Array.from({ length: 8 }, () => ({
    from: 0, to: 1, marker_aligned: marker, correlation: 0.9,
    magnitude,
  }));
  const profile = buildLinkProfiles(frames, config);
  assert.ok(profile);
  assert.equal(profile.acceptedFrames, 8);
  let peakIndex = 0;
  for (let i = 1; i < profile.magnitude.length; i++) {
    if (profile.magnitude[i] > profile.magnitude[peakIndex]) peakIndex = i;
  }
  assert.ok(
    Math.abs(profile.excessTaps[peakIndex] - tap) < 0.2,
    `peak tap ${profile.excessTaps[peakIndex]}`
  );
});

test('board freeze geometry requires a solved position set', () => {
  assert.equal(geometryFromBoardFreeze(null), null);
  assert.equal(geometryFromBoardFreeze({ solution: { positions: [] } }), null);
  const geometryValue = geometryFromBoardFreeze({
    solution: { positions: [{ x: 0, y: 0, z: 0 }, { x: 3, y: 0, z: 0 }] },
    configurationRevision: 7,
  });
  assert.ok(geometryValue);
  assert.equal(geometryValue.positions.length, 2);
  assert.equal(geometryValue.revision, '7');
  assert.equal(geometryValue.provenance.calibration_status, 'range-derived');
});

test('match_score gate accepts strong frames and rejects weak ones', () => {
  const strong = Array.from({ length: 8 }, () => ({
    from: 0, to: 1, marker_aligned: 4, match_score: 0.85,
    magnitude: new Float32Array(32), resampled: new Float32Array(497),
  }));
  assert.equal(buildLinkProfiles(strong, DEFAULT_MAP_CONFIG)?.acceptedFrames, 8);
  const weak = strong.map((frame) => ({ ...frame, match_score: 0.2 }));
  assert.equal(buildLinkProfiles(weak, DEFAULT_MAP_CONFIG), null);
});

test('match_score takes precedence over correlation', () => {
  const frames = Array.from({ length: 8 }, () => ({
    from: 0, to: 1, marker_aligned: 4, correlation: 0.92, match_score: 0.2,
    magnitude: new Float32Array(32), resampled: new Float32Array(497),
  }));
  assert.equal(buildLinkProfiles(frames, DEFAULT_MAP_CONFIG), null);
});

test('correlation fallback gate applies when match_score is absent', () => {
  const high = Array.from({ length: 8 }, () => ({
    from: 0, to: 1, marker_aligned: 4, correlation: 0.9,
    magnitude: new Float32Array(32), resampled: new Float32Array(497),
  }));
  assert.equal(buildLinkProfiles(high, DEFAULT_MAP_CONFIG)?.acceptedFrames, 8);
  const low = high.map((frame) => ({ ...frame, correlation: 0.2 }));
  assert.equal(buildLinkProfiles(low, DEFAULT_MAP_CONFIG), null);
});

test('dataset geometry parses board_positions blocks', () => {
  assert.equal(geometryFromBoardPositions(null), null);
  const geometryValue = geometryFromBoardPositions({
    schema: 'heimdall-geometry/1',
    units: 'm',
    node_count: 2,
    source: 'smoothed',
    revision: 1109,
    nodes: [
      { node_id: 0, position_m: [0, 0, 0] },
      { node_id: 1, position_m: [1.9, 0, 0] },
    ],
  });
  assert.ok(geometryValue);
  assert.equal(geometryValue.positions.length, 2);
  assert.equal(geometryValue.revision, '1109');
  assert.equal(geometryValue.provenance.calibration_status, 'range-derived');
  assert.equal(geometryFromBoardPositions({ nodes: [{ node_id: 0, position_m: [0, 0, 0] }] }), null);
});

function crc32(bytes: Uint8Array): number {
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) {
    crc ^= bytes[i];
    for (let j = 0; j < 8; j++) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function buildStoredZip(entries: [string, Uint8Array][]): Uint8Array {
  const encoder = new TextEncoder();
  const localParts: Uint8Array[] = [];
  const centralParts: Uint8Array[] = [];
  let offset = 0;
  for (const [name, data] of entries) {
    const nameBytes = encoder.encode(name);
    const crc = crc32(data);
    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true);
    local.setUint16(4, 20, true);
    local.setUint16(8, 0, true);
    local.setUint32(14, crc, true);
    local.setUint32(18, data.length, true);
    local.setUint32(22, data.length, true);
    local.setUint16(26, nameBytes.length, true);
    local.setUint16(28, 0, true);
    localParts.push(new Uint8Array(local.buffer), nameBytes, data);
    const central = new DataView(new ArrayBuffer(46));
    central.setUint32(0, 0x02014b50, true);
    central.setUint16(4, 20, true);
    central.setUint16(6, 20, true);
    central.setUint16(10, 0, true);
    central.setUint32(16, crc, true);
    central.setUint32(20, data.length, true);
    central.setUint32(24, data.length, true);
    central.setUint16(28, nameBytes.length, true);
    central.setUint32(42, offset, true);
    centralParts.push(new Uint8Array(central.buffer), nameBytes);
    offset += 30 + nameBytes.length + data.length;
  }
  const centralSize = centralParts.reduce((sum, part) => sum + part.length, 0);
  const eocd = new DataView(new ArrayBuffer(22));
  eocd.setUint32(0, 0x06054b50, true);
  eocd.setUint16(8, entries.length, true);
  eocd.setUint16(10, entries.length, true);
  eocd.setUint32(12, centralSize, true);
  eocd.setUint32(16, offset, true);
  const output = new Uint8Array(offset + centralSize + 22);
  let at = 0;
  for (const part of localParts) { output.set(part, at); at += part.length; }
  for (const part of centralParts) { output.set(part, at); at += part.length; }
  output.set(new Uint8Array(eocd.buffer), at);
  return output;
}

test('dataset zip parses manifest, geometry, and aligned CIRs', async () => {
  const manifest = JSON.stringify({ format: 'heimdall-capture-clip-v1', name: 'test-clip', duration_s: 15 });
  const metadata = JSON.stringify({
    board_positions: {
      schema: 'heimdall-geometry/1', units: 'm', node_count: 2, source: 'smoothed',
      nodes: [{ node_id: 0, position_m: [0, 0, 0] }, { node_id: 1, position_m: [3, 0, 0] }],
    },
  });
  const lines = [
    { from: 0, to: 1, event_s: 497.2, marker_aligned: 4, correlation: 0.9, match_score: 0.85, magnitude: [1, 0.5, 0.2] },
    { from: 0, to: 1, event_s: 497.25, marker_aligned: 4.1, correlation: 0.88, match_score: 0.8, magnitude: [1.1, 0.4, 0.3] },
    { from: 1, to: 0, event_s: 497.2, marker_aligned: 4, correlation: 0.7, magnitude: [0.9, 0.6, 0.1] },
    { from: 1, to: 0, event_s: 497.3, marker_aligned: 4.2, correlation: 0.6, magnitude: [0.8, 0.5, 0.2] },
  ].map((line) => JSON.stringify(line)).join('\n') + '\n';
  const zip = buildStoredZip([
    ['manifest.json', new TextEncoder().encode(manifest)],
    ['metadata.json', new TextEncoder().encode(metadata)],
    ['aligned-cirs.ndjson', new TextEncoder().encode(lines)],
  ]);
  const dataset = await parseDatasetZip(new Blob([zip]));
  assert.equal(dataset.name, 'test-clip');
  assert.equal(dataset.format, 'heimdall-capture-clip-v1');
  assert.equal(dataset.sampleCount, 4);
  assert.equal(dataset.links.length, 2);
  assert.ok(dataset.geometry);
  assert.equal(dataset.geometry.positions.length, 2);
  assert.ok(dataset.eventMax > dataset.eventMin);
});

test('nearest samples selects the n closest to the scrub time', () => {
  const samples: DatasetSample[] = [0, 1, 2, 3, 4, 5].map((event) => ({
    from: 0, to: 1, event_s: event, marker_aligned: 4, magnitude: new Float32Array(4),
  }));
  const nearest = nearestSamples(samples, 2.4, 4);
  assert.equal(nearest.length, 4);
  const events = nearest.map((sample) => sample.event_s).sort((a, b) => a - b);
  assert.deepEqual(events, [1, 2, 3, 4]);
  assert.deepEqual(nearestSamples(samples, 0, 3).map((sample) => sample.event_s), [0, 1, 2]);
  assert.deepEqual(nearestSamples(samples, 10, 3).map((sample) => sample.event_s), [5, 4, 3]);
});

test('bounds snap to node positions with padding', () => {
  const positions: Vec3[] = [{ x: 0, y: 0, z: 0 }, { x: 2, y: 0, z: 0 }, { x: 0, y: 1, z: 0 }];
  const auto = boundsFromPositions(positions);
  assert.deepEqual([...auto.min], [-1, -1, -1]);
  assert.deepEqual([...auto.max], [3, 2, 1]);
  const padded = boundsFromPositions(positions, 0.5);
  assert.deepEqual([...padded.min], [-0.5, -0.5, -0.5]);
  assert.deepEqual([...padded.max], [2.5, 1.5, 0.5]);
});

test('buildGrid honors explicit bounds', () => {
  const grid = buildGrid(geometry, 0.1, DEFAULT_MAP_CONFIG.maxVoxels, {
    min: [0, 0, 0],
    max: [4, 2, 0],
  });
  assert.equal(grid.shape[2], 41);
  assert.equal(grid.shape[1], 21);
  assert.equal(grid.min[0], 0);
  assert.ok(Math.abs(grid.spacing[0] - 0.1) < 1e-9);
});

const fusionGrid = { min: [0, 0, 0] as [number, number, number], spacing: [1, 1, 1] as [number, number, number], shape: [1, 1, 1] as [number, number, number] };
const fusionGeometry: MapGeometry = {
  positions: [{ x: 0, y: 0, z: 0 }, { x: 1, y: 0, z: 0 }, { x: 0, y: 1, z: 0 }],
  revision: 'fusion',
  provenance: { calibration_status: 'surveyed-calibrated' },
};

function fusionProfiles(values: number[]): LinkProfile[] {
  const links = [[0, 1], [1, 0], [0, 2], [2, 0], [1, 2], [2, 1]];
  return links.map(([from, to], index) => ({
    from,
    to,
    excessTaps: new Float32Array([0, 100]),
    rawMagnitude: new Float32Array([1, 1]),
    magnitude: new Float32Array([values[index], values[index]]),
    medianCorrelation: 1,
    acceptedFrames: 8,
  }));
}

test('standard reconstruction remains equivalent to backprojection', () => {
  const profiles = fusionProfiles([1, 2, 3, 4, 5, 6]);
  const standard = backproject(profiles, fusionGeometry, fusionGrid);
  const reconstructed = reconstruct(profiles, fusionGeometry, fusionGrid, {
    ...DEFAULT_RECONSTRUCTION_CONFIG,
    mode: 'standard',
  });
  assert.deepEqual([...reconstructed.volume], [...standard.volume]);
  assert.deepEqual([...reconstructed.confidence], [...standard.confidence]);
  assert.deepEqual([...reconstructed.intensity], [...standard.intensity]);
});

test('MGBP rejects an isolated link outlier and handles zero MAD', () => {
  const profiles = fusionProfiles([1, 1, 1, 1, 1, 100]);
  const volume = reconstruct(profiles, fusionGeometry, fusionGrid, {
    ...DEFAULT_RECONSTRUCTION_CONFIG,
    mode: 'mgbp',
  });
  assert.equal(volume.validLinks[0], 6);
  assert.equal(volume.supportLinks[0], 5);
  assert.equal(volume.volume[0], 1);
  assert.ok(Math.abs(volume.consensus[0] - 5 / 6) < 1e-6);
});

test('CGBP requires distributed active evidence and supports baseline merging', () => {
  const profiles = fusionProfiles([3, 3, 3, 3, 0.5, 0.5]);
  const directed = reconstruct(profiles, fusionGeometry, fusionGrid, {
    ...DEFAULT_RECONSTRUCTION_CONFIG,
    mode: 'cgbp',
  });
  assert.equal(directed.validLinks[0], 6);
  assert.equal(directed.supportLinks[0], 4);
  assert.equal(directed.volume[0], 3);

  const baselines = reconstruct(profiles, fusionGeometry, fusionGrid, {
    ...DEFAULT_RECONSTRUCTION_CONFIG,
    mode: 'cgbp',
    cgbpVoteBasis: 'baseline',
    cgbpMinValid: 3,
    cgbpMinActive: 2,
  });
  assert.equal(baselines.validLinks[0], 3);
  assert.equal(baselines.supportLinks[0], 2);
  assert.equal(baselines.volume[0], 3);
});

// --- Soft-consensus confidence -------------------------------------------------

const fusionVoxel: Vec3 = { x: fusionGrid.min[0], y: fusionGrid.min[1], z: fusionGrid.min[2] };

function excessTapFor(from: number, to: number): number {
  const tx = fusionGeometry.positions[from];
  const rx = fusionGeometry.positions[to];
  const direct = distance(tx, rx);
  const excess = distance(fusionVoxel, tx) + distance(fusionVoxel, rx) - direct;
  return excess / METRES_PER_TAP;
}

function softLinkProfile(
  from: number,
  to: number,
  background: number,
  targetTap: number,
  targetHeight: number
): LinkProfile {
  const tapCount = 401; // 0.0 .. 40.0 in 0.1-tap steps
  const excessTaps = new Float32Array(tapCount);
  const rawMagnitude = new Float32Array(tapCount);
  for (let i = 0; i < tapCount; i++) {
    const tap = i * 0.1;
    excessTaps[i] = tap;
    const d = (tap - targetTap) / 0.5;
    rawMagnitude[i] = background + targetHeight * Math.exp(-0.5 * d * d);
  }
  return {
    from,
    to,
    excessTaps,
    rawMagnitude,
    magnitude: rawMagnitude.slice(),
    medianCorrelation: 0.9,
    acceptedFrames: 16,
  };
}

const softDirectedLinks: [number, number][] = [[0, 1], [1, 0], [0, 2], [2, 0], [1, 2], [2, 1]];

function buildSoftScenario(
  bumpedLinks: Set<string>,
  height: number,
  scales: Partial<Record<string, number>> = {}
): LinkProfile[] {
  return softDirectedLinks.map(([from, to]) => {
    const key = `${from}>${to}`;
    const scale = scales[key] ?? 1;
    const tap = excessTapFor(from, to);
    const bumped = bumpedLinks.has(key);
    return softLinkProfile(from, to, 1 * scale, tap, (bumped ? height : 0) * scale);
  });
}

const softConfig = {
  ...DEFAULT_RECONSTRUCTION_CONFIG,
  mode: 'soft' as const,
  softNoiseStartTap: 5,
  softNoiseEndTap: 15,
};

test('estimateSoftBackground and estimateNoiseFloor read the configured tap window', () => {
  const profile = softLinkProfile(0, 1, 2, 20, 8);
  const background = estimateSoftBackground(profile, 5, 15);
  assert.ok(Math.abs(background - 2) < 0.05, `background estimate ${background}`);
  const floor = estimateNoiseFloor(profile, 5, 15, 3);
  assert.ok(floor >= background, `noise floor ${floor} should be >= plain background ${background}`);
  assert.equal(estimateSoftBackground(profile, 50, 60), Infinity);
});

test('soft consensus stays near zero when no link shows evidence above its own background', () => {
  const profiles = buildSoftScenario(new Set(), 0);
  const volume = reconstruct(profiles, fusionGeometry, fusionGrid, softConfig);
  assert.equal(volume.validLinks[0], 6);
  assert.ok(volume.volume[0] < 0.05, `confidence ${volume.volume[0]}`);
});

test('soft consensus suppresses a single dominant link regardless of its amplitude', () => {
  const profiles = buildSoftScenario(new Set(['0>1']), 50);
  const volume = reconstruct(profiles, fusionGeometry, fusionGrid, softConfig);
  assert.ok(volume.volume[0] < 0.1, `confidence ${volume.volume[0]}`);
});

test('soft consensus confidence rises continuously as more links agree', () => {
  const at = (links: string[]) =>
    reconstruct(buildSoftScenario(new Set(links), 5), fusionGeometry, fusionGrid, softConfig).volume[0];
  const zero = at([]);
  const one = at(['0>1']);
  const three = at(['0>1', '1>0', '0>2']);
  const six = at(['0>1', '1>0', '0>2', '2>0', '1>2', '2>1']);
  assert.ok(zero < one, `zero ${zero} should be < one ${one}`);
  assert.ok(one < three, `one ${one} should be < three ${three}`);
  assert.ok(three < six, `three ${three} should be < six ${six}`);
  assert.ok(six > 0.8, `six-link agreement confidence ${six} should be high`);
});

test('soft consensus is invariant to per-link amplitude scale (unequal link powers)', () => {
  const bumped = new Set(['0>1', '1>0', '0>2']);
  const baseline = reconstruct(buildSoftScenario(bumped, 5), fusionGeometry, fusionGrid, softConfig).volume[0];
  const scaled = reconstruct(
    buildSoftScenario(bumped, 5, { '0>1': 100, '1>0': 0.01, '0>2': 7, '2>0': 250, '1>2': 0.3, '2>1': 40 }),
    fusionGeometry,
    fusionGrid,
    softConfig
  ).volume[0];
  assert.ok(Math.abs(baseline - scaled) < 1e-3, `baseline=${baseline} scaled=${scaled}`);
});
