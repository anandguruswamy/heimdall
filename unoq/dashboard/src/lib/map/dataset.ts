import { geometryFromBoardPositions, type MapGeometry } from './map-engine.ts';
import { readZipEntries } from './zip-reader.ts';

export type DatasetSample = {
  from: number;
  to: number;
  event_s: number;
  marker_aligned: number;
  correlation?: number;
  match_score?: number;
  magnitude: Float32Array;
};

export type DatasetLink = {
  from: number;
  to: number;
  samples: DatasetSample[];
};

export type Dataset = {
  name: string;
  note: string;
  format: string;
  durationS: number;
  eventMin: number;
  eventMax: number;
  sampleCount: number;
  geometry: MapGeometry | null;
  links: DatasetLink[];
};

const toNumber = (value: unknown): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) ? value : undefined;

const toFloat32Array = (value: unknown): Float32Array | undefined => {
  if (value instanceof Float32Array) return value;
  if (!Array.isArray(value)) return undefined;
  const output = new Float32Array(value.length);
  for (let index = 0; index < value.length; index++) {
    output[index] = typeof value[index] === 'number' ? value[index] : 0;
  }
  return output;
};

const jsonEntry = (bytes: Uint8Array | undefined): Record<string, unknown> | null => {
  if (!bytes) return null;
  try {
    const value = JSON.parse(new TextDecoder('utf-8').decode(bytes)) as unknown;
    return value && typeof value === 'object' ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
};

export async function parseDatasetZip(file: Blob): Promise<Dataset> {
  const entries = await readZipEntries(await file.arrayBuffer());
  const ndjson = entries.get('aligned-cirs.ndjson');
  if (!ndjson) throw new Error('Dataset zip is missing aligned-cirs.ndjson');
  const manifest = jsonEntry(entries.get('manifest.json'));
  const metadata = jsonEntry(entries.get('metadata.json'));

  const byLink = new Map<string, DatasetSample[]>();
  const text = new TextDecoder('utf-8').decode(ndjson);
  let sampleCount = 0;
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    let record: Record<string, unknown>;
    try {
      record = JSON.parse(line) as Record<string, unknown>;
    } catch {
      continue;
    }
    const from = toNumber(record.from);
    const to = toNumber(record.to);
    const eventS = toNumber(record.event_s);
    const marker = toNumber(record.marker_aligned);
    const magnitude = toFloat32Array(record.magnitude);
    if (
      from === undefined || to === undefined || eventS === undefined ||
      marker === undefined || !magnitude || magnitude.length < 2
    ) {
      continue;
    }
    const key = `${from}>${to}`;
    let list = byLink.get(key);
    if (!list) {
      list = [];
      byLink.set(key, list);
    }
    list.push({
      from,
      to,
      event_s: eventS,
      marker_aligned: marker,
      correlation: toNumber(record.correlation),
      match_score: toNumber(record.match_score),
      magnitude,
    });
    sampleCount++;
  }

  let eventMin = Infinity;
  let eventMax = -Infinity;
  const links: DatasetLink[] = [];
  for (const [key, samples] of byLink) {
    samples.sort((a, b) => a.event_s - b.event_s);
    eventMin = Math.min(eventMin, samples[0].event_s);
    eventMax = Math.max(eventMax, samples[samples.length - 1].event_s);
    const [from, to] = key.split('>').map(Number);
    links.push({ from, to, samples });
  }
  links.sort((a, b) => a.from - b.from || a.to - b.to);
  if (!links.length) throw new Error('Dataset zip contains no usable aligned CIR samples');

  const boardPositions = metadata?.board_positions ?? null;
  const pipelineConfig = metadata?.pipeline && typeof metadata.pipeline === 'object'
    ? (metadata.pipeline as Record<string, unknown>).config
    : null;
  const name = String(manifest?.name ?? '');
  return {
    name,
    note: String(manifest?.note ?? ''),
    format: String(manifest?.format ?? 'heimdall-capture-clip-v1'),
    durationS: toNumber(manifest?.duration_s) ?? 0,
    eventMin: Number.isFinite(eventMin) ? eventMin : 0,
    eventMax: Number.isFinite(eventMax) ? eventMax : eventMin + 1,
    sampleCount,
    geometry: geometryFromBoardPositions(boardPositions),
    links,
  };
}

export function nearestSamples(
  samples: DatasetSample[],
  time: number,
  count: number
): DatasetSample[] {
  if (!samples.length || count <= 0) return [];
  let lower = 0;
  let upper = samples.length;
  while (lower < upper) {
    const middle = (lower + upper) >> 1;
    if (samples[middle].event_s < time) lower = middle + 1;
    else upper = middle;
  }
  let left = lower - 1;
  let right = lower;
  const output: DatasetSample[] = [];
  while (output.length < count && (left >= 0 || right < samples.length)) {
    if (right >= samples.length || (left >= 0 && time - samples[left].event_s <= samples[right].event_s - time)) {
      output.push(samples[left--]);
    } else {
      output.push(samples[right++]);
    }
  }
  return output;
}
