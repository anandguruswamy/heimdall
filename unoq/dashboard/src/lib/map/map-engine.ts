export const METRES_PER_TAP = 299_702_547 / 998_400_000;

export type Vec3 = { x: number; y: number; z: number };

export type MapGeometry = {
  positions: Vec3[];
  revision: string | null;
  provenance: { calibration_status: string };
};

export type LinkProfile = {
  from: number;
  to: number;
  excessTaps: Float32Array;
  magnitude: Float32Array;
  medianCorrelation: number;
  acceptedFrames: number;
};

export type MapConfig = {
  frames: number;
  minCorrelation: number;
  minAccepted: number;
  directPathGuardTaps: number;
  spacingM: number;
  maxVoxels: number;
};

export const DEFAULT_MAP_CONFIG: MapConfig = {
  frames: 32,
  minCorrelation: 0.5,
  minAccepted: 4,
  directPathGuardTaps: 1.0,
  spacingM: 0.1,
  maxVoxels: 200_000,
};

export type GridSpec = {
  min: [number, number, number];
  spacing: [number, number, number];
  shape: [number, number, number];
};

export type MapVolume = {
  volume: Float32Array;
  confidence: Float32Array;
  grid: GridSpec;
};

export type MapPoint = { x: number; y: number; z: number; magnitude: number; confidence: number };

export type PointCloud = {
  points: MapPoint[];
  threshold: number;
  valueRange: [number, number];
};

export type LinkStats = {
  from: number;
  to: number;
  frames: number;
  accepted: number;
  medianCorrelation: number;
};

export type MapSnapshot = {
  profiles: LinkStats[];
  grid: GridSpec;
  geometry: MapGeometry;
  spacingM: number;
  framesConfig: number;
  takenAt: number;
  volume: MapVolume;
};

const toNumber = (value: unknown): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) ? value : undefined;

const toFloatArray = (value: unknown): Float32Array | undefined => {
  if (value instanceof Float32Array) return value;
  if (!Array.isArray(value)) return undefined;
  const output = new Float32Array(value.length);
  for (let i = 0; i < value.length; i++) output[i] = typeof value[i] === 'number' ? value[i] : 0;
  return output;
};

const median = (values: number[]): number => {
  if (!values.length) return 0;
  const sorted = values.slice().sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
};

const interpUnit = (x: number, data: Float32Array): number => {
  const last = data.length - 1;
  if (x < 0 || x > last || last < 1) return 0;
  const lo = Math.floor(x);
  if (lo >= last) return data[last];
  const fraction = x - lo;
  return data[lo] * (1 - fraction) + data[lo + 1] * fraction;
};

const interp = (x: number, axis: Float32Array, values: Float32Array): number => {
  if (x < axis[0] || x > axis[axis.length - 1] || axis.length < 2) return 0;
  let lo = 0;
  let hi = axis.length - 1;
  while (lo + 1 < hi) {
    const mid = (lo + hi) >> 1;
    if (axis[mid] < x) lo = mid;
    else hi = mid;
  }
  const span = axis[hi] - axis[lo];
  const fraction = span === 0 ? 0 : (x - axis[lo]) / span;
  return values[lo] + (values[hi] - values[lo]) * fraction;
};

export function geometryFromBoardFreeze(freeze: unknown): MapGeometry | null {
  const value = (freeze && typeof freeze === 'object' ? freeze : {}) as {
    solution?: unknown;
    configurationRevision?: unknown;
  };
  const solution = (value.solution && typeof value.solution === 'object' ? value.solution : {}) as {
    positions?: unknown;
  };
  if (!Array.isArray(solution.positions) || solution.positions.length < 2) return null;
  const positions: Vec3[] = [];
  for (const raw of solution.positions) {
    const point = (raw && typeof raw === 'object' ? raw : {}) as { x?: unknown; y?: unknown; z?: unknown };
    const x = toNumber(point.x);
    const y = toNumber(point.y);
    const z = toNumber(point.z);
    if (x === undefined || y === undefined || z === undefined) return null;
    positions.push({ x, y, z });
  }
  const revision =
    value.configurationRevision === undefined ? null : String(value.configurationRevision);
  return { positions, revision, provenance: { calibration_status: 'range-derived' } };
}

export function buildLinkProfiles(frames: unknown[], config: MapConfig): LinkProfile | null {
  let from: number | undefined;
  let to: number | undefined;
  const candidates: { marker: number; correlation: number; resampled?: Float32Array; magnitude?: Float32Array }[] = [];
  for (const raw of frames) {
    const frame = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
    if (from === undefined) from = toNumber(frame.from);
    if (to === undefined) to = toNumber(frame.to);
    const marker = toNumber(frame.marker_aligned);
    const correlation = toNumber(frame.correlation);
    if (marker === undefined || correlation === undefined || correlation < config.minCorrelation) {
      continue;
    }
    const resampled = toFloatArray(frame.resampled);
    const magnitude = toFloatArray(frame.magnitude);
    if (resampled && resampled.length >= 2) {
      candidates.push({ marker, correlation, resampled });
    } else if (magnitude && magnitude.length >= 2) {
      candidates.push({ marker, correlation, magnitude });
    }
  }
  if (from === undefined || to === undefined || candidates.length < config.minAccepted) return null;

  const markers = candidates.map((candidate) => candidate.marker);
  const markerMedian = median(markers);
  const tolerance = Math.max(1.0, 0.1 * Math.abs(markerMedian) + 1.0);
  const kept = candidates.filter(
    (candidate) => Math.abs(candidate.marker - markerMedian) <= tolerance
  );
  if (kept.length < config.minAccepted) return null;

  const axisStep = 1 / 16;
  let maximumExcessTap = 0;
  const sources: { data: Float32Array; indexOf: (tap: number) => number }[] = [];
  for (const candidate of kept) {
    if (candidate.resampled) {
      const data = candidate.resampled;
      sources.push({ data, indexOf: (tap) => (tap + candidate.marker) * 16 });
      maximumExcessTap = Math.max(maximumExcessTap, (data.length - 1) / 16 - candidate.marker);
    } else if (candidate.magnitude) {
      const data = candidate.magnitude;
      sources.push({ data, indexOf: (tap) => tap + candidate.marker });
      maximumExcessTap = Math.max(maximumExcessTap, data.length - 1 - candidate.marker);
    }
  }
  if (sources.length < config.minAccepted) return null;

  const tapCount = Math.max(1, Math.floor(maximumExcessTap * 16) + 1);
  const excessTaps = new Float32Array(tapCount);
  for (let i = 0; i < tapCount; i++) excessTaps[i] = i * axisStep;

  const rows: number[][] = [];
  for (const source of sources) {
    const row: number[] = new Array(tapCount);
    for (let i = 0; i < tapCount; i++) row[i] = interpUnit(source.indexOf(excessTaps[i]), source.data);
    rows.push(row);
  }
  const magnitude = new Float32Array(tapCount);
  for (let i = 0; i < tapCount; i++) {
    const column: number[] = new Array(rows.length);
    for (let r = 0; r < rows.length; r++) column[r] = rows[r][i];
    magnitude[i] = median(column);
    if (excessTaps[i] <= config.directPathGuardTaps) magnitude[i] = 0;
  }

  const correlations = kept.map((candidate) => candidate.correlation);
  return {
    from,
    to,
    excessTaps,
    magnitude,
    medianCorrelation: median(correlations),
    acceptedFrames: kept.length,
  };
}

export function buildGrid(
  geometry: MapGeometry,
  spacingM: number,
  maxVoxels: number = DEFAULT_MAP_CONFIG.maxVoxels
): GridSpec {
  const minimum = [Infinity, Infinity, Infinity];
  const maximum = [-Infinity, -Infinity, -Infinity];
  for (const point of geometry.positions) {
    minimum[0] = Math.min(minimum[0], point.x);
    maximum[0] = Math.max(maximum[0], point.x);
    minimum[1] = Math.min(minimum[1], point.y);
    maximum[1] = Math.max(maximum[1], point.y);
    minimum[2] = Math.min(minimum[2], point.z);
    maximum[2] = Math.max(maximum[2], point.z);
  }
  const span = Math.max(
    maximum[0] - minimum[0],
    maximum[1] - minimum[1],
    maximum[2] - minimum[2]
  );
  const margin = Math.max(1.0, 0.5 * span);
  const lo: [number, number, number] = [
    minimum[0] - margin,
    minimum[1] - margin,
    minimum[2] - margin,
  ];
  const hi: [number, number, number] = [
    maximum[0] + margin,
    maximum[1] + margin,
    maximum[2] + margin,
  ];
  for (let axis = 0; axis < 3; axis++) {
    if (hi[axis] - lo[axis] < 0.4) {
      const centre = (lo[axis] + hi[axis]) / 2;
      lo[axis] = centre - 0.2;
      hi[axis] = centre + 0.2;
    }
  }
  const spacing = Math.max(1e-4, spacingM);
  const count = (axis: number) => Math.max(1, Math.floor((hi[axis] - lo[axis]) / spacing + 1e-9) + 1);
  let voxels = count(0) * count(1) * count(2);
  const effectiveSpacing = voxels > maxVoxels ? spacing * Math.cbrt(voxels / maxVoxels) : spacing;
  const shape: [number, number, number] = [
    Math.max(1, Math.floor((hi[2] - lo[2]) / effectiveSpacing + 1e-9) + 1),
    Math.max(1, Math.floor((hi[1] - lo[1]) / effectiveSpacing + 1e-9) + 1),
    Math.max(1, Math.floor((hi[0] - lo[0]) / effectiveSpacing + 1e-9) + 1),
  ];
  return {
    min: lo,
    spacing: [
      shape[2] > 1 ? (hi[0] - lo[0]) / (shape[2] - 1) : 1,
      shape[1] > 1 ? (hi[1] - lo[1]) / (shape[1] - 1) : 1,
      shape[0] > 1 ? (hi[2] - lo[2]) / (shape[0] - 1) : 1,
    ],
    shape,
  };
}

export function backproject(
  profiles: LinkProfile[],
  geometry: MapGeometry,
  grid: GridSpec
): MapVolume {
  const [nz, ny, nx] = grid.shape;
  const total = nz * ny * nx;
  const volume = new Float32Array(total);
  const confidence = new Float32Array(total);
  const coordinates = [
    new Float32Array(nx),
    new Float32Array(ny),
    new Float32Array(nz),
  ] as const;
  for (let i = 0; i < nx; i++) coordinates[0][i] = grid.min[0] + i * grid.spacing[0];
  for (let i = 0; i < ny; i++) coordinates[1][i] = grid.min[1] + i * grid.spacing[1];
  for (let i = 0; i < nz; i++) coordinates[2][i] = grid.min[2] + i * grid.spacing[2];

  for (const profile of profiles) {
    const transmitter = geometry.positions[profile.from];
    const receiver = geometry.positions[profile.to];
    if (!transmitter || !receiver) continue;
    const direct = Math.hypot(
      transmitter.x - receiver.x,
      transmitter.y - receiver.y,
      transmitter.z - receiver.z
    );
    const weight = Math.max(0.05, profile.medianCorrelation);
    const axis = profile.excessTaps;
    const magnitude = profile.magnitude;
    const maximumTap = axis[axis.length - 1];
    let index = 0;
    for (let iz = 0; iz < nz; iz++) {
      const z = coordinates[2][iz];
      for (let iy = 0; iy < ny; iy++) {
        const y = coordinates[1][iy];
        for (let ix = 0; ix < nx; ix++, index++) {
          const x = coordinates[0][ix];
          const excess =
            Math.hypot(x - transmitter.x, y - transmitter.y, z - transmitter.z) +
            Math.hypot(x - receiver.x, y - receiver.y, z - receiver.z) -
            direct;
          const taps = excess / METRES_PER_TAP;
          if (taps < 0 || taps > maximumTap) continue;
          volume[index] += interp(taps, axis, magnitude) * weight;
          confidence[index] += weight;
        }
      }
    }
  }
  for (let i = 0; i < total; i++) {
    if (confidence[i] > 0) volume[i] /= confidence[i];
  }
  return { volume, confidence, grid };
}

export function volumeToPoints(volume: MapVolume, percentile: number, limit = 50_000): PointCloud {
  const values = volume.volume;
  const confidence = volume.confidence;
  const total = values.length;
  const valid: number[] = [];
  for (let i = 0; i < total; i++) {
    if (confidence[i] > 0 && Number.isFinite(values[i])) valid.push(values[i]);
  }
  if (!valid.length) return { points: [], threshold: 0, valueRange: [0, 0] };
  const sorted = valid.slice().sort((a, b) => a - b);
  const valueRange: [number, number] = [sorted[0], sorted[sorted.length - 1]];
  const rank = (sorted.length - 1) * Math.max(0, Math.min(100, percentile)) / 100;
  const lower = Math.floor(rank);
  const fraction = rank - lower;
  const upper = Math.min(sorted.length - 1, lower + 1);
  const threshold = sorted[lower] + (sorted[upper] - sorted[lower]) * fraction;

  const selected: number[] = [];
  for (let i = 0; i < total; i++) {
    if (confidence[i] > 0 && values[i] >= threshold) selected.push(i);
  }
  selected.sort((a, b) => values[b] - values[a]);
  if (selected.length > limit) selected.length = limit;

  const { shape, min, spacing } = volume.grid;
  const nx = shape[2];
  const ny = shape[1];
  const points: MapPoint[] = new Array(selected.length);
  for (let k = 0; k < selected.length; k++) {
    const index = selected[k];
    const iz = Math.floor(index / (ny * nx));
    const remainder = index - iz * ny * nx;
    const iy = Math.floor(remainder / nx);
    const ix = remainder - iy * nx;
    points[k] = {
      x: min[0] + ix * spacing[0],
      y: min[1] + iy * spacing[1],
      z: min[2] + iz * spacing[2],
      magnitude: values[index],
      confidence: confidence[index],
    };
  }
  return { points, threshold, valueRange };
}
