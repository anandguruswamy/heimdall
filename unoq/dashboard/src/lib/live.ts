import type { Envelope, LinkLiveData, PlotFrame, PositionRange, TopicKey } from './types';

const colors = { amber: '#f4bd62', teal: '#45e0c1', violet: '#b995ff', blue: '#57a9ff' };
const MAX_LIVE_HISTORY = 160;
const lowerBound = (values: number[], value: number) => { let lo=0,hi=values.length;while(lo<hi){const mid=(lo+hi)>>1;if(values[mid]<value)lo=mid+1;else hi=mid;}return lo; };
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
    return { series: [smoothSs && { data: smoothSs, color: colors.amber, width: 2, ranging: 'ss' as const, smoothed: true }, rawSs && { data: rawSs, color: colors.amber, points: true, ranging: 'ss' as const }, smoothDs && { data: smoothDs, color: colors.violet, width: 2, ranging: 'ds' as const, smoothed: true }, rawDs && { data: rawDs, color: colors.violet, points: true, ranging: 'ds' as const }].filter(Boolean) as NonNullable<PlotFrame['series']>, min, max, xLabel:'history sample', yLabel:'cm' };
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
  const backendCurve = firstArray(sample, ['resampled', 'curve']);
  const curve = (backendCurve && backendCurve.length > 0) ? backendCurve : resample(raw, Math.max(2, raw.length * 16));
  const [,max] = extrema([raw,curve],[0,1]);
  const markerAligned=number(sample.marker_aligned),denominator=Math.max(1,raw.length-1);
  return { series: [{ data: curve, color: colors.teal, width: 2 }, { data: raw, color: colors.amber, points: true }], min: 0, max: max * 1.08, markers: [markerAligned !== undefined && { at: Math.max(0,Math.min(1,markerAligned/denominator)), color: colors.violet }].filter(Boolean) as NonNullable<PlotFrame['markers']>, xLabel:'aligned CIR tap', yLabel:'linear magnitude' };
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
  readonly positionRanges = new Map<string, PositionRange>();
  health: Record<string, unknown> | null = null;
  topology: Record<string, unknown> | null = null;
  settings: Record<string, unknown> | null = null;
  calibration: Record<string, unknown> | null = null;
  currentRound: number | undefined;
  latestReceivedAtMs = 0;
  lastError = '';
  private histories = new Map<string, Float32Array>();
  private positionSorted = new Map<string, number[]>();
  private dirtyFrames = new Set<string>();
  private framePayloads = new Map<string, Record<string, unknown>>();
  private waterfallRows = new Map<string, { width: number; rows: { eventS: number; values: Float32Array }[] }>();
  private waterfallSeconds = 5;
  private processingEpoch: bigint | undefined;
  private configurationEpoch: bigint | undefined;
  constructor(private readonly changed: () => void) {}

  resetStream(): void {
    this.links.clear();
    this.histories.clear();
    this.waterfallRows.clear();
    this.positionRanges.clear();
    this.positionSorted.clear();
    this.dirtyFrames.clear();
    this.framePayloads.clear();
    this.currentRound = undefined;
    this.lastError = '';
    this.changed();
  }

  ingest(envelope: Envelope): void {
    this.latestReceivedAtMs = envelope.receivedAtMs ?? performance.now();
    if ((this.processingEpoch !== undefined && this.processingEpoch !== envelope.processingEpoch)
      || (this.configurationEpoch !== undefined && this.configurationEpoch !== envelope.configurationEpoch)) {
      this.links.clear();
      this.histories.clear();
      this.waterfallRows.clear();
      this.positionRanges.clear();
      this.positionSorted.clear();
      this.dirtyFrames.clear();
      this.framePayloads.clear();
    }
    this.processingEpoch = envelope.processingEpoch;
    this.configurationEpoch = envelope.configurationEpoch;
    const payload = record(envelope.payload);
    this.currentRound = number(payload.current_round) ?? this.currentRound;
    this.ingestPayload(envelope.topic, payload);
  }

  ingestPayload(topic: TopicKey, payload: Record<string, unknown>): void {
    if (topic === 'health') this.health = { ...(this.health ?? {}), ...payload };
    if (topic === 'calibration') this.calibration = payload;
    const from = number(payload.from), to = number(payload.to);
    if (from === undefined || to === undefined) { this.changed(); return; }
    const id = `${from}>${to}`;
    const current = this.links.get(id) ?? { payloads: {} };
    if (topic === 'distance') {
      const keys=['raw_ss_cm','smoothed_ss_cm','raw_ds_cm','smoothed_ds_cm','raw_ss','smoothed_ss','raw_ds','smoothed_ds'];
      keys.forEach((key) => {
        const value = number(payload[key]); if (value !== undefined) { this.append(`${id}:distance:${key}`,value); if(!key.endsWith('_cm'))this.append(`${id}:distance:${key}_cm`,value*100); }
      });
      const sample=record(payload.sample),kind=String(sample.kind ?? (number(payload.raw_ds) !== undefined ? 'ds' : ''));
      if (kind === 'ds') {
        const sf=number(sample.from) ?? from,st=number(sample.to) ?? to,a=Math.min(sf,st),b=Math.max(sf,st),key=`${a}>${b}`;
        const raw=number(payload.raw_ds) ?? number(sample.raw_m),smoothed=number(payload.smoothed_ds) ?? number(sample.moving_average_m),eventS=number(sample.event_s) ?? number(payload.event_s) ?? 0,round=number(sample.round) ?? number(payload.round) ?? 0;
        const evidence=Array.isArray(sample.evidence) ? sample.evidence.join(':') : `${eventS}:${round}`;
        const previous=this.positionRanges.get(key),isNew=smoothed !== undefined && previous?.evidence !== evidence,window=isNew ? previous?.window.slice() ?? [] : previous?.window ?? [];
        let ultra=previous?.ultra;
        if (isNew) {
          const sorted=this.positionSorted.get(key) ?? window.slice().sort((x,y)=>x-y);
          if (window.length>=MAX_LIVE_HISTORY) { const removed=window.shift()!; sorted.splice(lowerBound(sorted,removed),1); }
          window.push(smoothed); sorted.splice(lowerBound(sorted,smoothed),0,smoothed); this.positionSorted.set(key,sorted);
          const middle=Math.floor(sorted.length/2);ultra=sorted.length%2 ? sorted[middle] : (sorted[middle-1]+sorted[middle])/2;
        }
        this.positionRanges.set(key,{a,b,raw,smoothed,ultra,outlier:Boolean(sample.outlier),eventS,round,evidence,window});
      }
    }
    if (topic === 'cfo') {
      ['raw_ppm','filtered_ppm','raw','filtered','cfo_ppm'].forEach((key) => {
        const value = number(payload[key]); if (value !== undefined) this.append(`${id}:cfo:${key}`,value);
      });
    }
    current.updatedAt = Date.now(); current.payloads ??= {}; current.payloads[topic] = payload;
    current.quality = number(payload.quality) ?? number(record(payload.sample).quality) ?? current.quality;
    current.cfoPpm = number(payload.filtered_ppm) ?? number(payload.cfo_ppm) ?? number(record(payload.sample).cfo_ppm) ?? current.cfoPpm;
    const cm = number(payload.distance_cm) ?? number(payload.smoothed_ss_cm);
    const metres = number(payload.smoothed_ss) ?? number(payload.smoothed_ds) ?? number(payload.raw_ss) ?? number(payload.raw_ds) ?? number(payload.distance_m) ?? number(payload.distance);
    current.distanceCm = cm ?? (metres === undefined ? current.distanceCm : metres * 100);
    if (topic === 'waterfall') {
      const frame=this.waterfallFrame(id,payload);if(frame)current[topic]=frame;
    } else {
      this.dirtyFrames.add(`${id}:${topic}`);
    }
    this.links.set(id, current); this.changed();
  }

  flushFrames(): void {
    for (const key of this.dirtyFrames) {
      const separator=key.lastIndexOf(':'),id=key.slice(0,separator),topic=key.slice(separator+1) as TopicKey,current=this.links.get(id),payload=current?.payloads?.[topic];
      if (!current || !payload) continue;
      let plottingPayload=payload;
      if (topic==='distance') {
        plottingPayload=this.framePayloads.get(key) ?? {};Object.assign(plottingPayload,payload);this.framePayloads.set(key,plottingPayload);
        ['raw_ss_cm','smoothed_ss_cm','raw_ds_cm','smoothed_ds_cm','raw_ss','smoothed_ss','raw_ds','smoothed_ds'].forEach((name)=>{const history=this.histories.get(`${id}:distance:${name}`);if(history)plottingPayload[name]=history;});
      } else if (topic==='cfo') {
        plottingPayload=this.framePayloads.get(key) ?? {};Object.assign(plottingPayload,payload);this.framePayloads.set(key,plottingPayload);
        ['raw_ppm','filtered_ppm','raw','filtered','cfo_ppm'].forEach((name)=>{const history=this.histories.get(`${id}:cfo:${name}`);if(history)plottingPayload[name==='cfo_ppm'?'filtered_ppm':name]=history;});
      }
      const frame=topic==='cir'?cirFrame(plottingPayload):topic==='slow-fft'?heatFrame(plottingPayload):lineFrame(topic,plottingPayload);
      if(frame)current[topic]=frame;
      if(topic==='fast-fft'){const phase=firstArray(payload,['phase','phases']);if(phase)current.fastFftPhase={series:[{data:phase,color:colors.violet}],min:-Math.PI,max:Math.PI,xLabel:'channel frequency bin',yLabel:'rad'};}
    }
    this.dirtyFrames.clear();
  }

  setWaterfallSeconds(seconds: number): void { this.waterfallSeconds=Math.max(1,Math.min(30,seconds)); }

  private append(key: string, value: number): Float32Array {
    let history = this.histories.get(key);
    if (!history) { history=new Float32Array(MAX_LIVE_HISTORY);history.fill(Number.NaN);this.histories.set(key,history); }
    history.copyWithin(0,1);history[history.length-1]=value;return history;
  }

  private waterfallFrame(id: string, payload: Record<string, unknown>): PlotFrame | undefined {
    const row = numbers(payload.row), width = number(payload.width);
    if (!row || !width || row.length !== width) return undefined;
    const eventS = number(payload.event_s) ?? Date.now() / 1000;
    let ring = this.waterfallRows.get(id);
    if (!ring || ring.width !== width) { ring = { width, rows: [] }; this.waterfallRows.set(id, ring); }
    ring.rows.push({ eventS, values: row });
    ring.rows = ring.rows.filter((item) => eventS - item.eventS <= this.waterfallSeconds);
    const stride=Math.max(1,Math.ceil(ring.rows.length/128));
    const displayed=ring.rows.filter((_,index)=>index%stride===0 || index===ring.rows.length-1).reverse();
    const data = new Float32Array(displayed.length * width);
    displayed.forEach((item,index) => data.set(item.values,index*width));
    const [min,max] = extrema([data],[0,1]);
    const xMin=number(payload.x_min),xMax=number(payload.x_max),marker=number(payload.marker);
    const markerAt=marker !== undefined && xMin !== undefined && xMax !== undefined && marker >= xMin && marker <= xMax ? (marker-xMin)/(xMax-xMin) : undefined;
    return { heatmap: data, heatWidth: width, heatHeight: displayed.length, min, max, markers: markerAt === undefined ? [] : [{at:markerAt,color:colors.violet}], xLabel:xMin === undefined || xMax === undefined ? 'aligned delay' : `${xMin}..${xMax} aligned taps`, yLabel:'newest → oldest' };
  }

  loadTopology(value: unknown): void {
    this.topology = record(value);
    this.currentRound = number(this.topology.current_round) ?? this.currentRound;
    const entries = Array.isArray(this.topology.links) ? this.topology.links : [];
    entries.forEach((entry) => {
      const item=record(entry), latestFast=record(item.latest_fast_fft), latestSlow=record(item.latest_slow_fft), distance=record(item.distance);
      if (Object.keys(latestFast).length) this.ingestPayload('fast-fft',{ from:item.from,to:item.to,...latestFast });
      if (Object.keys(latestSlow).length) this.ingestPayload('slow-fft',{ from:item.from,to:item.to,...latestSlow });
      if (Object.keys(distance).length) this.ingestPayload('distance',{ from:item.from,to:item.to,sample:distance,[`${String(distance.kind ?? 'ss') === 'ds' ? 'smoothed_ds' : 'smoothed_ss'}`]:distance.moving_average_m ?? distance.calibrated_m,[`${String(distance.kind ?? 'ss') === 'ds' ? 'raw_ds' : 'raw_ss'}`]:distance.raw_m });
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
