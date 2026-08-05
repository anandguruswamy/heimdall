import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DEFAULT_MAP_CONFIG,
  METRES_PER_TAP,
  backproject,
  buildGrid,
  buildLinkProfiles,
  geometryFromBoardFreeze,
  volumeToPoints,
  type LinkProfile,
  type MapGeometry,
  type Vec3,
} from '../src/lib/map/map-engine.ts';

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
    frames.push({ from, to, marker_aligned: marker + (Math.random() - 0.5) * 0.4, correlation: 0.92, quality: 0, magnitude, resampled });
  }
  for (let n = 0; n < 2; n++) frames.push({ from, to, marker_aligned: marker, correlation: 0.1, quality: 0, magnitude, resampled });
  frames.push({ from, to, marker_aligned: marker + 6, correlation: 0.9, quality: 0, magnitude, resampled });
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
