import type { Envelope, LinkLiveData, PlotFrame, TopicKey } from './types';

const colors = { amber: '#f4bd62', teal: '#45e0c1', violet: '#b995ff', blue: '#57a9ff' };
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' ? value as Record<string, unknown> : {};
const number = (value: unknown): number | undefined => typeof value === 'number' && Number.isFinite(value) ? value : undefined;
const numbers = (value: unknown): Float32Array | undefined => {
  if (value instanceof Float32Array) return value;
  if (!Array.isArray(value)) return undefined;
  const output = new Float32Array(value.length);
  for (let i = 0; i < value.length; i++) output[i] = typeof value[i] === 'number' ? value[i] : 0;
  return output;
};
const firstArray = (payload: Record<string, unknown>, keys: string[]) => keys.map((key) => numbers(payload[key])).find(Boolean);
const extrema = (arrays: (Float32Array | undefined)[], fallback: [number, number]): [number, number] => {
  let min = Infinity, max = -Infinity;
  arrays.forEach((array) => array?.forEach((value) => { min = Math.min(min, value); max = Math.max(max, value); }));
  return Number.isFinite(min) && max > min ? [min, max] : fallback;
};
const resample = (raw: Float32Array, length: number) => {
  const output = new Float32Array(length);
  for (let i = 0; i < length; i++) { const at = i * (raw.length - 1) / (length - 1), lo = Math.floor(at), t = at - lo; output[i] = raw[lo] * (1 - t) + raw[Math.min(raw.length - 1, lo + 1)] * t; }
  return output;
};

function lineFrame(topic: TopicKey, payload: Record<string, unknown>): PlotFrame | undefined {
  if (topic === 'distance') {
    const centimetres = (cmKeys: string[], metreKeys: string[]) => {
      const cm = firstArray(payload, cmKeys); if (cm) return cm;
      const metres = firstArray(payload, metreKeys); if (!metres) return undefined;
      const output = new Float32Array(metres.length); for (let i=0;i<metres.length;i++) output[i]=metres[i]*100; return output;
    };
    const rawSs = centimetres(['raw_ss_cm','ss_raw_cm'], ['raw_ss','ss_raw']);
    const smoothSs = centimetres(['smoothed_ss_cm','ss_smoothed_cm'], ['smoothed_ss','ss_smoothed']);
    const rawDs = centimetres(['raw_ds_cm','ds_raw_cm'], ['raw_ds','ds_raw','ds']);
    const smoothDs = centimetres(['smoothed_ds_cm','ds_smoothed_cm'], ['smoothed_ds','ds_smoothed']);
    if (!rawSs && !smoothSs && !rawDs && !smoothDs) return undefined;
    const [min,max] = extrema([rawSs,smoothSs,rawDs,smoothDs],[0,1000]);
    return { series: [rawSs && { data: rawSs, color: colors.amber }, smoothSs && { data: smoothSs, color: colors.teal, width: 2 }, rawDs && { data: rawDs, color: colors.violet }, smoothDs && { data: smoothDs, color: colors.blue, width: 2 }].filter(Boolean) as NonNullable<PlotFrame['series']>, min, max, xLabel:'history', yLabel:'cm' };
  }
  if (topic === 'fast-fft') {
    const magnitude = firstArray(payload, ['magnitude', 'magnitudes', 'db']);
    const phase = firstArray(payload, ['phase', 'phases']);
    const data = magnitude ?? phase;
    if (!data) return undefined;
    const [min,max] = extrema([data],[0,1]);
    return { series: [{ data, color: magnitude ? colors.teal : colors.violet }], min, max, xLabel:'channel frequency bin', yLabel:magnitude?'magnitude':'rad' };
  }
  if (topic === 'cfo') {
    const raw = firstArray(payload, ['raw_ppm', 'raw', 'history']);
    const filtered = firstArray(payload, ['filtered_ppm', 'filtered']);
    if (!raw && !filtered) return undefined;
    const [min,max] = extrema([raw,filtered],[-10,10]);
    return { series: [raw && { data: raw, color: colors.amber }, filtered && { data: filtered, color: colors.teal, width: 2 }].filter(Boolean) as NonNullable<PlotFrame['series']>, min, max, xLabel:'history', yLabel:'ppm' };
  }
  return undefined;
}

function cirFrame(payload: Record<string, unknown>): PlotFrame | undefined {
  const sample = record(payload.sample ?? payload);
  const i = firstArray(sample, ['cir_i', 'i']);
  const q = firstArray(sample, ['cir_q', 'q']);
  let raw = firstArray(sample, ['magnitude', 'raw']);
  if (!raw && i) { raw = new Float32Array(i.length); for (let n=0;n<i.length;n++) raw[n] = Math.hypot(i[n],q?.[n] ?? 0); }
  if (!raw?.length) return undefined;
  const curve = firstArray(sample, ['resampled', 'curve']) ?? resample(raw, Math.max(2, raw.length * 16));
  const [,max] = extrema([raw,curve],[0,1]);
  const markerRaw=number(sample.marker_raw),markerAligned=number(sample.marker_aligned),denominator=Math.max(1,raw.length-1);
  return { series: [{ data: curve, color: colors.teal, width: 2 }, { data: raw, color: colors.amber, points: true }], min: 0, max: max * 1.08, markers: [markerRaw !== undefined && { at: Math.max(0,Math.min(1,markerRaw/denominator)), color: colors.amber }, markerAligned !== undefined && { at: Math.max(0,Math.min(1,markerAligned/denominator)), color: colors.violet }].filter(Boolean) as NonNullable<PlotFrame['markers']>, xLabel:'aligned CIR tap', yLabel:'linear magnitude' };
}

function heatFrame(payload: Record<string, unknown>): PlotFrame | undefined {
  const data = firstArray(payload, ['values', 'data', 'heatmap', 'magnitude']);
  if (!data) return undefined;
  const width = number(payload.width) ?? number(payload.bins) ?? data.length;
  const height = number(payload.height) ?? number(payload.rows) ?? Math.max(1, Math.floor(data.length / width));
  const [min,max] = extrema([data],[0,1]);
  return { heatmap: data, heatWidth: width, heatHeight: height, min, max, xLabel:'frequency bin', yLabel:'CIR tap' };
}

export class LiveStore {
  readonly links = new Map<string, LinkLiveData>();
  health: Record<string, unknown> | null = null;
  topology: Record<string, unknown> | null = null;
  settings: Record<string, unknown> | null = null;
  calibration: Record<string, unknown> | null = null;
  lastError = '';
  private histories = new Map<string, number[]>();
  private waterfallRows = new Map<string, { width: number; rows: Float32Array[] }>();
  private waterfallSeconds = 5;
  constructor(private readonly changed: () => void) {}

  resetStream(): void {
    this.links.clear();
    this.histories.clear();
    this.waterfallRows.clear();
    this.lastError = '';
    this.changed();
  }

  ingest(envelope: Envelope): void {
    const payload = record(envelope.payload);
    this.ingestPayload(envelope.topic, payload);
  }

  ingestPayload(topic: TopicKey, payload: Record<string, unknown>): void {
    if (topic === 'health') this.health = payload;
    if (topic === 'calibration') this.calibration = payload;
    const from = number(payload.from), to = number(payload.to);
    if (from === undefined || to === undefined) { this.changed(); return; }
    const id = `${from}>${to}`;
    const current = this.links.get(id) ?? { payloads: {} };
    const plottingPayload = { ...payload };
    if (topic === 'distance') {
      const keys=['raw_ss_cm','smoothed_ss_cm','raw_ds_cm','smoothed_ds_cm','raw_ss','smoothed_ss','raw_ds','smoothed_ds'];
      keys.forEach((key) => {
        const value = number(payload[key]); if (value !== undefined) plottingPayload[key] = this.append(`${id}:distance:${key}`,value);
      });
      keys.forEach((key) => { const history=this.histories.get(`${id}:distance:${key}`); if (history) plottingPayload[key]=history; });
    }
    if (topic === 'cfo') {
      ['raw_ppm','filtered_ppm','raw','filtered','cfo_ppm'].forEach((key) => {
        const value = number(payload[key]); if (value !== undefined) plottingPayload[key === 'cfo_ppm' ? 'filtered_ppm' : key] = this.append(`${id}:cfo:${key}`,value);
      });
    }
    const frame = topic === 'cir' ? cirFrame(plottingPayload) : topic === 'waterfall' ? this.waterfallFrame(id, plottingPayload) : topic === 'slow-fft' ? heatFrame(plottingPayload) : lineFrame(topic, plottingPayload);
    if (frame) current[topic] = frame;
    if (topic === 'fast-fft') {
      const phase=firstArray(payload,['phase','phases']);
      if (phase) current.fastFftPhase={series:[{data:phase,color:colors.violet}],min:-Math.PI,max:Math.PI,xLabel:'channel frequency bin',yLabel:'rad'};
    }
    current.updatedAt = Date.now(); current.payloads ??= {}; current.payloads[topic] = payload;
    current.quality = number(payload.quality) ?? number(record(payload.sample).quality) ?? current.quality;
    current.cfoPpm = number(payload.filtered_ppm) ?? number(payload.cfo_ppm) ?? number(record(payload.sample).cfo_ppm) ?? current.cfoPpm;
    const cm = number(payload.distance_cm) ?? number(payload.smoothed_ss_cm);
    const metres = number(payload.smoothed_ss) ?? number(payload.smoothed_ds) ?? number(payload.raw_ss) ?? number(payload.raw_ds) ?? number(payload.distance_m) ?? number(payload.distance);
    current.distanceCm = cm ?? (metres === undefined ? current.distanceCm : metres * 100);
    this.links.set(id, current); this.changed();
  }

  setWaterfallSeconds(seconds: number): void { this.waterfallSeconds=Math.max(1,Math.min(30,seconds)); }

  private append(key: string, value: number): number[] {
    const history = this.histories.get(key) ?? [];
    history.push(value); if (history.length > 256) history.shift(); this.histories.set(key,history); return history;
  }

  private waterfallFrame(id: string, payload: Record<string, unknown>): PlotFrame | undefined {
    const row = numbers(payload.row), width = number(payload.width);
    if (!row || !width || row.length !== width) return undefined;
    let ring = this.waterfallRows.get(id);
    if (!ring || ring.width !== width) { ring = { width, rows: [] }; this.waterfallRows.set(id, ring); }
    ring.rows.push(row); const retained=Math.ceil(this.waterfallSeconds*30); if (ring.rows.length > retained) ring.rows.splice(0,ring.rows.length-retained);
    const stride=Math.max(1,Math.ceil(ring.rows.length/128)),displayed=ring.rows.filter((_,index)=>index%stride===0);
    const data = new Float32Array(displayed.length * width);
    displayed.forEach((values,index) => data.set(values,index*width));
    const [min,max] = extrema([data],[0,1]);
    return { heatmap: data, heatWidth: width, heatHeight: displayed.length, min, max, xLabel:'aligned delay', yLabel:'slow time' };
  }

  loadTopology(value: unknown): void {
    this.topology = record(value);
    const entries = Array.isArray(this.topology.links) ? this.topology.links : [];
    entries.forEach((entry) => {
      const item=record(entry), latestFast=record(item.latest_fast_fft), latestSlow=record(item.latest_slow_fft), distance=record(item.distance);
      if (Object.keys(latestFast).length) this.ingestPayload('fast-fft',{ from:item.from,to:item.to,...latestFast });
      if (Object.keys(latestSlow).length) this.ingestPayload('slow-fft',{ from:item.from,to:item.to,...latestSlow });
      if (Object.keys(distance).length) this.ingestPayload('distance',{ from:item.from,to:item.to,[`${String(distance.kind ?? 'ss') === 'ds' ? 'smoothed_ds' : 'smoothed_ss'}`]:distance.moving_average_m ?? distance.calibrated_m });
    });
    this.changed();
  }

  loadDistanceHistory(value: unknown): void {
    const history=record(value),entries=Array.isArray(history.links)?history.links:[];
    entries.forEach((entry)=>{
      const item=record(entry);
      this.ingestPayload('distance',{from:item.from,to:item.to,raw_ss:item.raw_ss,smoothed_ss:item.smoothed_ss,raw_ds:item.raw_ds,smoothed_ds:item.smoothed_ds});
    });
  }
}
