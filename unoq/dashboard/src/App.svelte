<script lang="ts">
  import { onMount } from 'svelte';
  import PlotCanvas from './lib/PlotCanvas.svelte';
  import BoardPositions from './lib/BoardPositions.svelte';
  import RadarMap from './lib/RadarMap.svelte';
  import { HeimdallApi } from './lib/api';
  import { linksFor } from './lib/demo';
  import { LiveStore } from './lib/live';
  import type { MapSnapshot } from './lib/map/map-engine';
  import type { Dataset } from './lib/map/dataset';
  import { tabs, type Link, type PlotFrame, type StreamStatus, type Tab, type TopicKey } from './lib/types';

  let active: Tab = $state('Network Health');
  let nodeCount = $state(5);
  let selectedId = $state('0>1');
  let focused: Link | null = $state(null);
  let settingsOpen = $state(false);
  let status: StreamStatus = $state('connecting');
  let phaseMode = $state(false);
  let periodogramMode = $state(false);
  let dbMode = $state(false);
  let distanceMode: 'ss'|'ds'|'both' = $state('ds');
  let distanceSmoothed = $state(true);
  let distanceCensorNegative = $state(false);
  let distanceShowBridged = $state(true);
  let distanceHalfRangeCm = $state(10);
  let waterfallFixedScale = $state(true);
  let waterfallScaleMin = $state(-60);
  let waterfallScaleMax = $state(-10);
  let waterfallTapMin = $state(-20);
  let waterfallTapMax = $state(50);
  let waterfallClutter = $state(false);
  let waterfallMagnitudeClutter = $state(true);
  let waterfallNuisanceFit = $state(true);
  let waterfallRejectSpikes = $state(true);
  let waterfallPathLoss = $state(false);
  let waterfallNoiseClipDb = $state(12);
  let halfLife = $state(2);
  let smoothing = $state(1);
  let range = $state(4);
  let alignmentMode = $state('correlation');
  let dgcCorrectionDb = $state(2.65);
  let cirLockScale = $state(false);
  let cirFitAlgorithm: 'off'|'linear_ls'|'robust_grid' = $state('off');
  let cirGridReferenceMode = $state('frozen');
  let cirGridReferenceWindow = $state(32);
  let cirGridScoreMode = $state('soft');
  let cirGridGainMinDb = $state(-10);
  let cirGridGainMaxDb = $state(10);
  let cirGridDelayRange = $state(2);
  let cirGridResamplingFactor = $state(16);
  let cirGridEta = $state(0.25);
  let cirGridReferencePeakDb = $state(-10);
  let calibrationPair = $state('0>1');
  let referencesM = $state<Record<string, string>>({});
  let calibrationSolution = $state<Record<string, unknown> | null>(null);
  let liveRevision = $state(0);
  let backendMessage = $state('Backend unavailable · controls remain inspectable');
  let snapshotProgress = $state(0);
  let snapshotState = $state<'idle'|'capturing'|'complete'|'error'>('idle');
  let clipState = $state<'idle'|'capturing'|'complete'|'error'>('idle');
  let clipProgress = $state(0);
  let clips = $state<Record<string, unknown>[]>([]);
  let clipName = $state('');
  let clipNote = $state('');
  let clipDurationS = $state(10);
  let mapSnapshot = $state<MapSnapshot | null>(null);
  let mapDataset = $state.raw<Dataset | null>(null);
  let compactMode = $state(false);
  let uiCommitLatencyMs = $state(0);
  let waterfallSeconds = $state(5);
  let slowFftSeconds = $state(2);
  let settingsTimer: ReturnType<typeof setTimeout> | undefined;
  let healthTimer: ReturnType<typeof setInterval> | undefined;
  let swipeStartX: number | null = null;
  let api = $state.raw(null as unknown as HeimdallApi);
  let revisionRaf = 0;
  function requestUiRevision() {
    if (revisionRaf) return;
    revisionRaf = requestAnimationFrame(() => {
      revisionRaf = 0;
      live.flushFrames();
      if (live.latestReceivedAtMs) uiCommitLatencyMs = performance.now() - live.latestReceivedAtMs;
      liveRevision++;
    });
  }
  const live = new LiveStore(requestUiRevision);
  const CIR_DB_OFFSET = -10 - 20 * Math.log10(11.2);
  const CIR_LINEAR_SCALE = Math.pow(10, CIR_DB_OFFSET / 20);

  const links = $derived(linksFor(nodeCount));
  const selected = $derived(links.find((link) => link.id === selectedId) ?? links[0]);
  const calibrationPairs = $derived(links.filter((link) => link.from < link.to));
  const calibrationLive = $derived.by(() => { void liveRevision; return live.calibration?.live && typeof live.calibration.live === 'object' ? live.calibration.live as Record<string, unknown> : null; });
  const EMPTY_FRAME: PlotFrame = { series: [], min: 0, max: 1 };
  const dbFrameCache = new WeakMap<PlotFrame, Map<string, PlotFrame>>();
  const normalizedCirFrameCache = new WeakMap<PlotFrame, PlotFrame>();
  const distanceFrameCache = new WeakMap<PlotFrame, Map<string, PlotFrame>>();
  let calibrationFrameSource: unknown;
  let cachedCalibrationFrame: PlotFrame = EMPTY_FRAME;

  function setTab(tab: Tab) {
    active = tab;
    focused = null;
    api?.subscribe(tab);
  }

  function plotFor(tab: Tab, link: Link) {
    return () => {
      void liveRevision;
      const topic = topicFor(tab);
      const data = live.links.get(link.id);
      if (topic === 'fast-fft' && phaseMode) {
        return data?.fastFftPhase ?? EMPTY_FRAME;
      }
      let frame = data?.[topic];
      if (topic === 'cir' && frame) frame = normalizedCirFrame(frame);
      if (topic === 'cir' && cirLockScale && frame) {
        frame = { ...frame, min: 0, max: 1 };
      }
      if (topic === 'distance' && frame?.series) {
        frame = distanceDisplayFrame(frame);
      }
      if (topic === 'fast-fft' && periodogramMode && frame?.series?.[0]) {
        const original = frame.series[0].data;
        const squared = new Float32Array(original.length);
        for (let i = 0; i < original.length; i++) squared[i] = original[i] * original[i];
        const [min, max] = [0, Math.max(...squared)];
        return { series: [{ data: squared, color: frame.series[0].color }], min, max, xLabel: frame.xLabel, yLabel: 'power' };
      }
      const cirOffset = topic === 'waterfall' ? CIR_DB_OFFSET : 0;
      if (dbMode && frame) {
        const fixedBounds: [number,number] | undefined =
          topic === 'waterfall' && waterfallFixedScale ? [waterfallScaleMin, waterfallScaleMax]
          : topic === 'cir' && cirLockScale ? [-60,0]
          : undefined;
        return toDbFrame(frame, cirOffset, fixedBounds);
      }
      if (topic === 'waterfall' && waterfallFixedScale && frame) {
        frame = {
          ...frame,
          min: Math.pow(10, (waterfallScaleMin - cirOffset) / 20),
          max: Math.pow(10, (waterfallScaleMax - cirOffset) / 20),
        };
      }
      return frame ?? EMPTY_FRAME;
    };
  }

  function normalizedCirFrame(frame: PlotFrame): PlotFrame {
    const cached=normalizedCirFrameCache.get(frame);if(cached)return cached;
    const scale=(src:Float32Array)=>{const out=new Float32Array(src.length);for(let i=0;i<src.length;i++)out[i]=src[i]*CIR_LINEAR_SCALE;return out;};
    const normalized={...frame,series:frame.series?.map((item)=>({...item,data:scale(item.data)})),min:frame.min===undefined?undefined:frame.min*CIR_LINEAR_SCALE,max:frame.max===undefined?undefined:frame.max*CIR_LINEAR_SCALE,yLabel:'normalized magnitude'};
    normalizedCirFrameCache.set(frame,normalized);return normalized;
  }

  function distanceDisplayFrame(frame: PlotFrame): PlotFrame {
    const key = `${distanceMode}:${distanceSmoothed}:${distanceCensorNegative}:${distanceHalfRangeCm}`;
    const cached = distanceFrameCache.get(frame)?.get(key);
    if (cached) return cached;
    const series = frame.series?.filter((item) => (distanceMode === 'both' || item.ranging === distanceMode) && (distanceSmoothed || !item.smoothed));
    const filtered = distanceCensorNegative
      ? censorNegativeDistance({ ...frame, series }, distanceHalfRangeCm)
      : centeredDistanceRange(frame, series, distanceHalfRangeCm);
    const variants = distanceFrameCache.get(frame) ?? new Map<string, PlotFrame>();
    variants.set(key, filtered); distanceFrameCache.set(frame, variants);
    return filtered;
  }

  function centeredDistanceRange(frame: PlotFrame, series: PlotFrame['series'], halfRangeCm: number): PlotFrame {
    const ds = series?.find((item) => item.ranging === 'ds' && item.smoothed)?.data;
    const candidates = ds ? [ds] : series?.map((item) => item.data) ?? [];
    let center: number | undefined;
    for (const values of candidates) {
      for (let index = values.length - 1; index >= 0; index--) {
        if (Number.isFinite(values[index])) { center=values[index]; break; }
      }
      if (center !== undefined) break;
    }
    if (center === undefined) return { ...frame, series, min: 0, max: halfRangeCm * 2 };
    return { ...frame, series, min: center - halfRangeCm, max: center + halfRangeCm };
  }

  function censorNegativeDistance(frame: PlotFrame, halfRangeCm: number): PlotFrame {
    const series = frame.series?.map((item) => {
      const data = new Float32Array(item.data.length);
      for (let i = 0; i < item.data.length; i++) data[i] = item.data[i] < 0 ? Number.NaN : item.data[i];
      return { ...item, data };
    });
    return centeredDistanceRange(frame, series, halfRangeCm);
  }

  function toDbFrame(frame: PlotFrame, offset = 0, fixedBounds?: [number,number]): PlotFrame {
    const key = `${offset}:${fixedBounds?.[0] ?? 'auto'}:${fixedBounds?.[1] ?? 'auto'}`;
    const cached = dbFrameCache.get(frame)?.get(key);
    if (cached) return cached;
    const toDb = (value: number) => 20 * Math.log10(Math.max(value, 1e-12)) + offset;
    const convert = (src: Float32Array) => { const out = new Float32Array(src.length); for (let i = 0; i < src.length; i++) out[i] = toDb(src[i]); return out; };
    const series = frame.series?.map((s) => ({ ...s, data: convert(s.data) }));
    const heatmap = frame.heatmap ? convert(frame.heatmap) : undefined;
    const values = heatmap ?? series?.[0]?.data;
    const bounds = fixedBounds ?? finiteBounds(values, Boolean(heatmap));
    const converted = { ...frame, series, heatmap, min: bounds?.[0], max: bounds?.[1], yLabel: heatmap ? frame.yLabel : 'dB' };
    const variants = dbFrameCache.get(frame) ?? new Map<string, PlotFrame>();
    variants.set(key, converted); dbFrameCache.set(frame, variants);
    return converted;
  }

  function finiteBounds(values: Float32Array | undefined, robust: boolean): [number,number] | undefined {
    if (!values?.length) return undefined;
    let min=Infinity,max=-Infinity,count=0;
    for (const value of values) if (Number.isFinite(value)) { min=Math.min(min,value);max=Math.max(max,value);count++; }
    if (!count) return undefined;
    if (!robust || max <= min) return [min,max];
    const bins=new Uint32Array(256),span=max-min;
    for (const value of values) if (Number.isFinite(value)) bins[Math.min(255,Math.floor((value-min)/span*256))]++;
    const quantile=(fraction:number)=>{const target=(count-1)*fraction;let total=0;for(let i=0;i<bins.length;i++){total+=bins[i];if(total>target)return min+(i+.5)/bins.length*span;}return max;};
    return [quantile(.05),quantile(.95)];
  }

  function topicFor(tab: Tab): TopicKey {
    return ({ 'Network Health':'health','Live Distance':'distance','Board Positions':'distance','Live CIR':'cir','CIR Waterfall':'waterfall','Slow-Time FFT':'slow-fft','Fast-Time FFT':'fast-fft','CFO':'cfo','Distance Calibration':'calibration','Radar Map':'cir' } as const)[tab];
  }

  function linkMetric(link: Link) {
    void liveRevision;
    const cm = live.links.get(link.id)?.distanceCm;
    return cm === undefined ? '—' : cm.toFixed(1);
  }

  function chooseNodeCount(value: number) {
    nodeCount = value;
    selectedId = '0>1';
    focused = null;
  }

  function nodeHealth(node: number) {
    void liveRevision;
    const topologyLinks = Array.isArray(live.topology?.links) ? live.topology.links as Record<string,unknown>[] : [];
    const related = topologyLinks.filter((link) => Number(link.from) === node || Number(link.to) === node);
    const cycleUs=Number((live.topology?.config as Record<string,unknown>|undefined)?.cycle_us),expectedRate=cycleUs>0?1_000_000/cycleUs:0;
    const rates = related.map((link) => Number(link.rate_hz)).filter(Number.isFinite);
    const delivery = rates.length && expectedRate ? rates.reduce((sum,rate)=>sum+Math.min(1,rate/expectedRate),0)/rates.length*100 : null;
    const healthy = expectedRate ? rates.filter((rate)=>rate>=expectedRate*.9).length : 0;
    return { delivery, healthy, links: related.length, samples: related.reduce((sum,link)=>sum+Number(link.observations ?? 0),0) };
  }

  function linkHealth(link: Link) {
    void liveRevision;
    const topologyLinks = Array.isArray(live.topology?.links) ? live.topology.links as Record<string,unknown>[] : [];
    const item = topologyLinks.find((value) => Number(value.from) === link.from && Number(value.to) === link.to);
    const rate=Number(item?.rate_hz),cycleUs=Number((live.topology?.config as Record<string,unknown>|undefined)?.cycle_us),expectedRate=cycleUs>0?1_000_000/cycleUs:0;
    return !item || !Number.isFinite(rate) ? { label: 'NO DATA', width: 0 } : { label: `${rate.toFixed(1)} Hz`, width: expectedRate ? Math.min(100,rate/expectedRate*100) : 0 };
  }

  function calibrated() {
    const applied = live.calibration?.applied as Record<string,unknown> | undefined;
    return applied?.id !== null && applied?.id !== undefined;
  }

  function linkFooter(link: Link) {
    void liveRevision;
    const data = live.links.get(link.id);
    if (active === 'CFO') return data?.cfoPpm === undefined ? 'NO DATA' : `${data.cfoPpm.toFixed(3)} ppm`;
    if (active === 'Slow-Time FFT') {
      const filled=Number(data?.payloads?.['slow-fft']?.filled_percent);
      return Number.isFinite(filled) ? `FILLED ${filled.toFixed(1)}%` : 'NO DATA';
    }
    return data?.updatedAt ? `Q ${data.quality ?? 0}` : 'NO DATA';
  }

  function mobileLegend(): string[] {
    if (active === 'Live Distance') return ['SS RAW','SS SMOOTHED','DS RAW / SMOOTHED'];
    if (active === 'Live CIR') return ['DISPLAY INTERPOLATION','ALIGNED TAPS','FIRST PATH'];
    if (active === 'CIR Waterfall') return ['AMPLITUDE','SLOW TIME','FIRST PATH'];
    if (active === 'Slow-Time FFT') return ['CIR TAP','DOPPLER','MAGNITUDE'];
    if (active === 'Fast-Time FFT') return ['CHANNEL RESPONSE',phaseMode?'PHASE':'MAGNITUDE'];
    if (active === 'CFO') return ['RAW CFO','FILTERED CFO'];
    return [];
  }

  function pairValue(id: string) {
    void liveRevision;
    const [a,b] = id.split('>').map(Number);
    const pairs = Array.isArray(calibrationSolution?.pairs) ? calibrationSolution.pairs as Record<string,unknown>[] : Array.isArray(calibrationLive?.pairs) ? calibrationLive.pairs as Record<string,unknown>[] : [];
    const pair = pairs.find((item)=>Number(item.a)===a&&Number(item.b)===b);
    return pair?.measured_bias_m === null || pair?.measured_bias_m === undefined ? undefined : Number(pair.measured_bias_m);
  }

  function calibrationPreview(): PlotFrame {
    void liveRevision;
    const source=calibrationSolution ?? calibrationLive;
    if (source === calibrationFrameSource) return cachedCalibrationFrame;
    calibrationFrameSource=source;
    const values = Array.isArray(calibrationSolution?.residuals) ? (calibrationSolution.residuals as number[]).map((value)=>value*100) : null;
    if (!values) { cachedCalibrationFrame=EMPTY_FRAME; return cachedCalibrationFrame; }
    const data = new Float32Array(values), bound = Math.max(1,...values.map(Math.abs));
    cachedCalibrationFrame={ series: [{ data, color: '#45e0c1', points: true }], min: -bound, max: bound, xLabel:'referenced pair', yLabel:'cm residual' };
    return cachedCalibrationFrame;
  }

  function recommendedPair() {
    const pair=calibrationSolution?.recommended_next_pair;
    return Array.isArray(pair)&&pair.length===2 ? `${pair[0]}↔${pair[1]}` : '—';
  }

  function boardOffsets() {
    const offsets=Array.isArray(calibrationSolution?.board_offsets) ? calibrationSolution.board_offsets as [number,number][] : [];
    return offsets.map(([board,value])=>`N${board} ${value>=0?'+':''}${(value*100).toFixed(2)} cm`).join(' · ');
  }

  function residualSummary() {
    const residuals=Array.isArray(calibrationSolution?.residuals) ? calibrationSolution.residuals as number[] : [];
    return residuals.map((value,index)=>`R${index} ${value>=0?'+':''}${(value*100).toFixed(2)} cm`).join(' · ');
  }

  function headerRound() {
    void liveRevision;
    return live.currentRound === undefined ? '—' : live.currentRound.toLocaleString();
  }

  function headerRate() {
    void liveRevision;
    const topologyLinks=Array.isArray(live.topology?.links) ? live.topology.links as Record<string,unknown>[] : [];
    const rates=topologyLinks.map((link)=>Number(link.rate_hz)).filter((rate)=>Number.isFinite(rate)&&rate>0);
    return rates.length ? (rates.reduce((a,b)=>a+b,0)/rates.length).toFixed(1) : '—';
  }

  function healthEvents(): string[][] {
    void liveRevision;
    const pipeline = live.health?.pipeline && typeof live.health.pipeline === 'object' ? live.health.pipeline as Record<string,unknown> : null;
    const parserValue = pipeline?.parser ?? live.topology?.parser;
    const parser = parserValue && typeof parserValue === 'object' ? parserValue as Record<string,unknown> : null;
    const configValue = pipeline?.config ?? live.topology?.config;
    const config = configValue && typeof configValue === 'object' ? configValue as Record<string,unknown> : null;
    const time = new Date().toISOString().slice(11,23);
    if (!pipeline && !parser && !config) return [[time,'GW','Waiting for backend health','warn']];
    const errors=Number(parser?.crc_failures??0)+Number(parser?.framing_errors??0);
    const archive=live.health?.archive && typeof live.health.archive === 'object' ? live.health.archive as Record<string,unknown> : {};
    return [
      [time,'GW',`Records ${Number(pipeline?.records??0).toLocaleString()} · observations ${Number(pipeline?.observations??0).toLocaleString()} · rejected ${Number(pipeline?.rejected??0).toLocaleString()}`,Number(pipeline?.rejected??0)>0?'warn':'ok'],
      [time,'RX',`Parser CRC ${Number(parser?.crc_failures??0)} · framing ${Number(parser?.framing_errors??0)} · sequence gaps ${Number(parser?.sequence_gaps??0)}`,errors?'warn':'ok'],
      [time,'CFG',config ? `N=${Number(config.n_nodes??nodeCount)} · CH${Number(config.channel??9)} · configuration epoch ${Number(pipeline?.configuration_epoch??live.topology?.configuration_epoch??0)}` : 'Awaiting radio configuration','info'],
      [time,'NET',`Links ${Number(pipeline?.links_with_samples??0)} / ${Number(pipeline?.expected_links??links.length)} · processing epoch ${Number(pipeline?.processing_epoch??live.topology?.processing_epoch??0)}`,'info'],
      [time,'SYS',`Clients ${Number(live.health?.websocket_clients??0)} · queue drops ${Number(live.health?.processing_queue_drops??0)} · UI commit ${uiCommitLatencyMs.toFixed(1)} ms · RSS ${(Number((live.health?.process as Record<string,unknown>|undefined)?.rss_bytes??0)/1_000_000).toFixed(1)} MB`,'info'],
      [time,'DSK',`Archive ${(Number(archive.closed_bytes??0)/1_000_000).toFixed(1)} MB · free ${Number(archive.free_percent??0).toFixed(1)}%${archive.last_error ? ` · ${String(archive.last_error)}` : ''}`,archive.paused===true?'warn':'ok']
    ];
  }

  function distanceValue(link: Link, keys: string[]): string {
    void liveRevision;
    const payload=live.links.get(link.id)?.payloads?.distance;
    if (!payload) return '—';
    for (const key of keys) {
      const value=Number(payload[key]);
      if (Number.isFinite(value)) return `${(key.endsWith('_cm') ? value : value*100).toFixed(2)} cm`;
    }
    return '—';
  }

  function footerRadio(): string {
    void liveRevision;
    const config=(live.topology?.config ?? (live.health?.pipeline as Record<string,unknown>|undefined)?.config) as Record<string,unknown>|undefined;
    if (!config) return 'RADIO CONFIGURATION UNAVAILABLE';
    const hash=Number(config.config_hash);
    return `N=${Number(config.n_nodes)} · M=${Number(config.m_slots)} · ${Number(config.cir_taps)} TAPS · CONFIG ${Number.isFinite(hash) ? `0x${hash.toString(16).toUpperCase()}` : '—'}`;
  }

  function exportHealth() {
    const blob=new Blob([JSON.stringify({ exported_at:new Date().toISOString(), health:live.health, topology:live.topology },null,2)],{type:'application/json'});
    const href=URL.createObjectURL(blob),anchor=document.createElement('a');
    anchor.href=href; anchor.download=`heimdall-health-${new Date().toISOString().replaceAll(/[:.]/g,'-')}.json`; anchor.click(); URL.revokeObjectURL(href);
  }

  function finishLinkSwipe(clientX: number) {
    if (swipeStartX === null) return;
    const delta=clientX-swipeStartX; swipeStartX=null;
    if (Math.abs(delta)<45) return;
    const index=links.findIndex((link)=>link.id===selectedId);
    const next=Math.max(0,Math.min(links.length-1,index+(delta<0?1:-1)));
    if (links[next]) selectedId=links[next].id;
  }

  function settingChanged(key: string, value: unknown) {
    const current = live.settings?.value && typeof live.settings.value === 'object' ? live.settings.value as Record<string,unknown> : {};
    live.settings = { ...live.settings, value: { ...current, [key]: value } };
    clearTimeout(settingsTimer);
    settingsTimer = setTimeout(async () => { try { live.settings = await api.putSettings((live.settings as Record<string,unknown>).value) as Record<string,unknown>; backendMessage = 'Settings applied'; } catch { backendMessage = 'Backend unavailable · setting not applied'; } liveRevision++; }, 250);
  }

  function setCirFitAlgorithm(value: 'off'|'linear_ls'|'robust_grid') {
    cirFitAlgorithm=value;
    settingChanged('cir_fit_algorithm',value);
  }

  function waterfallScaleChanged(bound: 'min'|'max', value: number) {
    if (!Number.isFinite(value)) return;
    if (bound === 'min') {
      waterfallScaleMin=Math.min(value,waterfallScaleMax-1);
      settingChanged('waterfall_fixed_scale_min',waterfallScaleMin);
    } else {
      waterfallScaleMax=Math.max(value,waterfallScaleMin+1);
      settingChanged('waterfall_fixed_scale_max',waterfallScaleMax);
    }
  }

  async function takeSnapshot() {
    snapshotState = 'capturing'; snapshotProgress = 0; backendMessage = 'Capturing calibration samples…';
    const timer = setInterval(() => snapshotProgress = Math.min(99, snapshotProgress + 1), 100);
    try {
      const [outcome] = await Promise.all([
        api.snapshotCalibration().then((value) => ({ value })).catch((error: unknown) => ({ error })),
        new Promise((resolve) => setTimeout(resolve, 10_000))
      ]);
      if ('error' in outcome) throw outcome.error;
      void outcome.value;
      live.calibration = await api.getCalibration() as Record<string,unknown>; calibrationSolution=null; snapshotProgress = 100; snapshotState = 'complete'; backendMessage = 'Snapshot complete · enter tape references and solve';
    } catch { snapshotState = 'error'; backendMessage = 'Backend unavailable · snapshot was not recorded'; }
    finally { clearInterval(timer); liveRevision++; }
  }

  async function solveCalibration() {
    const values=Object.fromEntries(Object.entries(referencesM).filter(([,value])=>value.trim()!==''&&Number.isFinite(Number(value))&&Number(value)>0).map(([pair,value])=>[pair,Number(value)]));
    if (!Object.keys(values).length) { backendMessage='Enter at least one positive tape reference'; return; }
    try { calibrationSolution=await api.solveCalibration(values) as Record<string,unknown>; backendMessage='Calibration solution ready'; }
    catch { backendMessage='Backend unavailable · calibration solve failed'; }
  }

  async function applyCalibration() {
    if (!confirm('Apply this full-rank calibration to live ranging?')) return;
    try { const applied=await api.applyCalibration() as Record<string,unknown>; live.calibration={ ...(live.calibration??{}), applied }; backendMessage = 'Calibration applied'; }
    catch { backendMessage = 'Backend unavailable · calibration not applied'; }
    liveRevision++;
  }


  async function rollbackCalibration() {
    if (!confirm('Restore the previous calibration offsets?')) return;
    try { const applied=await api.rollbackCalibration() as Record<string,unknown>; live.calibration={ ...(live.calibration??{}), applied }; backendMessage='Previous calibration restored'; }
    catch { backendMessage='No previous calibration is available'; }
    liveRevision++;
  }

  function frozenBoardPositions() {
    const freeze = live.boardFreeze;
    if (!freeze) return null;
    return {
      schema: 'heimdall-geometry/1',
      units: 'm',
      revision: freeze.configurationRevision,
      frame: {
        name: 'dashboard-range-derived',
        origin: `N${freeze.origin} antenna phase centre`,
        axes: `+X toward N${freeze.xAxis}, +Y side selected by N${freeze.xyPlane}, +Z side selected by N${freeze.above}`,
      },
      source: freeze.source,
      node_count: freeze.nodeCount,
      configuration_revision: freeze.configurationRevision,
      nodes: freeze.solution.positions.map((point, node_id) => ({ node_id, position_m: [point.x, point.y, point.z] })),
    };
  }

  async function saveClip() {
    if (clipState === 'capturing') return;
    clipState = 'capturing'; clipProgress = 0; backendMessage = `Capturing ${clipDurationS} seconds from trigger…`;
    try {
      const created = await api.saveClip({ name: clipName.trim(), note: clipNote.trim(), duration_s: clipDurationS, board_positions: frozenBoardPositions() }) as Record<string,unknown>;
      const clipId = Number(created.id);
      const started = Date.now();
      const pollMs = Math.max(15_000, clipDurationS * 1000 + 5_000);
      while (Date.now() - started < pollMs) {
        await new Promise((resolve) => setTimeout(resolve, 1_000));
        clipProgress = Math.min(99, (Date.now() - started) / pollMs * 100);
        const value = await api.getClips();
        clips = Array.isArray(value) ? value as Record<string,unknown>[] : [];
        const row=clips.find((clip)=>Number(clip.id)===clipId);
        const latest = row?.value as Record<string,unknown> | undefined;
        if (latest?.status === 'complete') { clipState='complete'; clipProgress=100; backendMessage='Protected capture clip ready'; return; }
        if (latest?.status === 'failed') throw new Error(String(latest.error ?? 'Clip failed'));
      }
      throw new Error('Clip finalization timed out');
    } catch (error) { clipState='error'; backendMessage=error instanceof Error ? error.message : 'Capture clip failed'; }
  }

  async function deleteClip(id: number) {
    if (!confirm(`Delete protected clip ${id}?`)) return;
    try { await api.deleteClip(id); clips=clips.filter((clip)=>Number(clip.id)!==id); backendMessage=`Clip ${id} deleted`; }
    catch { backendMessage=`Clip ${id} could not be deleted`; }
  }

  onMount(() => {
    const compact = matchMedia('(max-width: 900px)');
    const updateCompact = () => compactMode = compact.matches;
    updateCompact();
    compact.addEventListener('change', updateCompact);
    api = new HeimdallApi('/api', (next) => status = next, (envelope) => live.ingest(envelope), (data) => {
      if (data.health) live.health = data.health as Record<string,unknown>;
      if (data.topology) { live.loadTopology(data.topology); const config=(data.topology as Record<string,unknown>).config as Record<string,unknown>|undefined; const count = Number(config?.n_nodes); if (count >= 2 && count <= 8) chooseNodeCount(count); }
      if (data.distanceHistory) live.loadDistanceHistory(data.distanceHistory);
      if (data.settings) { live.settings = data.settings as Record<string,unknown>; const value=(live.settings.value ?? {}) as Record<string,unknown>; halfLife=Number(value.cfo_half_life_s ?? 2); smoothing=Number(value.distance_smoothing_s ?? 1); range=Number(value.reference_half_life_s ?? 4); alignmentMode=String(value.cir_alignment_mode ?? 'correlation'); slowFftSeconds=Number(value.slow_fft_history_s ?? 2); waterfallClutter=Boolean(value.waterfall_clutter); waterfallMagnitudeClutter=value.waterfall_magnitude_clutter !== false; waterfallNuisanceFit=value.waterfall_nuisance_fit !== false; waterfallRejectSpikes=value.waterfall_reject_spikes !== false; waterfallPathLoss=Boolean(value.waterfall_path_loss); waterfallNoiseClipDb=Number(value.waterfall_noise_clip_db ?? 12); waterfallTapMin=Number(value.waterfall_tap_min ?? -20); waterfallTapMax=Number(value.waterfall_tap_max ?? 50); waterfallScaleMin=Number(value.waterfall_fixed_scale_min ?? -60); waterfallScaleMax=Number(value.waterfall_fixed_scale_max ?? -10); dgcCorrectionDb=Number(value.dgc_correction_db_per_step ?? 2.65); const configured=String(value.cir_fit_algorithm ?? 'off'); cirFitAlgorithm=(configured==='off'&&Boolean(value.cir_nuisance_fit)?'linear_ls':configured) as typeof cirFitAlgorithm; cirGridReferenceMode=String(value.cir_grid_reference_mode ?? 'frozen'); cirGridReferenceWindow=Number(value.cir_grid_reference_window ?? 32); cirGridScoreMode=String(value.cir_grid_score_mode ?? 'soft'); cirGridGainMinDb=Number(value.cir_grid_gain_min_db ?? -10); cirGridGainMaxDb=Number(value.cir_grid_gain_max_db ?? 10); cirGridDelayRange=Number(value.cir_grid_delay_range_samples ?? 2); cirGridResamplingFactor=Number(value.cir_grid_resampling_factor ?? 16); cirGridEta=Number(value.cir_grid_eta ?? .25); cirGridReferencePeakDb=Number(value.cir_grid_reference_peak_db ?? -10); }
      if (data.calibration) {
        live.calibration = data.calibration as Record<string,unknown>;
        const calibration=data.calibration as Record<string,unknown>, liveState=calibration.live as Record<string,unknown>|undefined, applied=calibration.applied as Record<string,unknown>|undefined, stored=applied?.value as Record<string,unknown>|undefined;
        if (liveState?.status === 'complete') { snapshotState='complete'; snapshotProgress=100; }
        if (stored?.solution && typeof stored.solution === 'object') calibrationSolution=stored.solution as Record<string,unknown>;
      }
      backendMessage = data.health || data.topology || data.settings ? 'Backend connected' : 'Backend unavailable · no live data'; requestUiRevision();
    }, (message) => { live.lastError = message; backendMessage = message; requestUiRevision(); }, () => {
      live.resetStream();
      backendMessage = 'Resynchronizing live state';
    });
    void api.getClips().then((value) => { clips = Array.isArray(value) ? value as Record<string,unknown>[] : []; });
    api.subscribe(active);
    api.connect();
    healthTimer=setInterval(async()=>{ if(active!=='Network Health') return; try { const [health,topology]=await Promise.all([api.get('/health'),api.get('/topology')]); live.health=health as Record<string,unknown>; live.loadTopology(topology); requestUiRevision(); } catch {} },500);
    return () => { clearTimeout(settingsTimer); clearInterval(healthTimer); if(revisionRaf)cancelAnimationFrame(revisionRaf); compact.removeEventListener('change', updateCompact); api.close(); };
  });
</script>

<svelte:head><meta name="description" content="Heimdall UWB sensing instrument" /></svelte:head>

<div class="instrument">
  <header class="topbar">
    <div class="identity">
      <div class="mark" aria-hidden="true"><i></i><i></i><i></i></div>
      <div><strong>HEIMDALL</strong><span>UWB DASHBOARD</span></div>
    </div>
    <div class="header-metrics">
      <div><span>ROUND</span><b>{headerRound()}</b></div>
      <div><span>RATE</span><b>{headerRate()} <small>Hz</small></b></div>
      <div><span>NODES</span><b>{nodeCount}/8</b></div>
    </div>
    <div class="actions">
      {#if !calibrated()}<span class="calibration-warning">UNCALIBRATED</span>{/if}
      <span class:offline={status === 'offline'} class="status"><i></i>{status.toUpperCase()}</span>
      <button class="icon-button" onclick={saveClip} disabled={clipState === 'capturing'}>{clipState === 'capturing' ? `CLIP ${Math.round(clipProgress)}%` : `CAPTURE ${clipDurationS}S`}</button>
      <button class="icon-button" onclick={() => settingsOpen = !settingsOpen} aria-expanded={settingsOpen} aria-label="Open settings">TUNE</button>
    </div>
  </header>

  <nav class="tabs" aria-label="Analysis modes" style={`--tab-count:${tabs.length}`}>
    {#each tabs as tab, i}
      <button class:active={active === tab} onclick={() => setTab(tab)}><em>{String(i + 1).padStart(2, '0')}</em>{tab}</button>
    {/each}
  </nav>

  <main>
    <section class:waterfall-mode={active === 'CIR Waterfall'} class="mode-head">
      <div><p>ANALYSIS / {active.toUpperCase()}</p><h1>{active}</h1></div>
      <div class="mode-controls">
        {#if active === 'Live Distance'}
          <div class="segmented" aria-label="Ranging series"><button class:active={distanceMode === 'ss'} onclick={() => distanceMode = 'ss'}>SS-TWR</button><button class:active={distanceMode === 'ds'} onclick={() => distanceMode = 'ds'}>DS-TWR</button><button class:active={distanceMode === 'both'} onclick={() => distanceMode = 'both'}>Both</button></div>
          <label class="display-toggle"><input type="checkbox" bind:checked={distanceSmoothed} />SMOOTHED LINES</label>
          <label class="display-toggle"><input type="checkbox" bind:checked={distanceCensorNegative} />CENSOR NEGATIVE</label>
          <label class="display-toggle"><input type="checkbox" bind:checked={distanceShowBridged} onchange={() => live.setShowBridgedDs(distanceShowBridged)} />SHOW BRIDGED</label>
          <label class="display-toggle">Y RANGE<select aria-label="Distance y-axis range" bind:value={distanceHalfRangeCm}>{#each [10,20,50,100,200] as value}<option value={value}>+/- {value} cm</option>{/each}</select></label>
        {:else if active === 'Live CIR'}
          <label class="display-toggle"><input type="checkbox" bind:checked={cirLockScale} />LOCK Y SCALE</label>
          <div class="segmented" aria-label="Live CIR fitting algorithm"><button class:active={cirFitAlgorithm==='off'} onclick={()=>setCirFitAlgorithm('off')}>OFF</button><button class:active={cirFitAlgorithm==='linear_ls'} onclick={()=>setCirFitAlgorithm('linear_ls')}>LINEAR LS</button><button class:active={cirFitAlgorithm==='robust_grid'} onclick={()=>setCirFitAlgorithm('robust_grid')}>ROBUST GRID</button></div>
        {:else if active === 'Fast-Time FFT'}
          <div class="segmented" aria-label="FFT display"><button class:active={!phaseMode && !periodogramMode} onclick={() => { phaseMode = false; periodogramMode = false; }}>Magnitude</button><button class:active={!periodogramMode && phaseMode} onclick={() => { phaseMode = true; periodogramMode = false; }}>Phase</button><button class:active={periodogramMode} onclick={() => { phaseMode = false; periodogramMode = true; }}>Periodogram</button></div>
        {:else if active === 'CFO'}
          <label>HALF-LIFE <output>{halfLife.toFixed(1)} s</output><input type="range" min="0.1" max="30" step="0.1" value={halfLife} oninput={(e) => { halfLife=+e.currentTarget.value; settingChanged('cfo_half_life_s',halfLife); }} /></label>
        {:else if active === 'CIR Waterfall'}
          <div class="waterfall-controls">
            <div class="control-group history-control"><span>HISTORY</span><output>{waterfallSeconds.toFixed(0)} s</output><input aria-label="Waterfall history seconds" type="range" min="1" max="30" step="1" value={waterfallSeconds} oninput={(e) => { waterfallSeconds=+e.currentTarget.value; live.setWaterfallSeconds(waterfallSeconds); }} /></div>
            <div class="control-group tap-control"><span>TAP WINDOW</span><label>FROM<input aria-label="First aligned tap" type="number" min="-64" max={waterfallTapMax - 1} step="1" value={waterfallTapMin} onchange={(e) => { waterfallTapMin=+e.currentTarget.value; settingChanged('waterfall_tap_min', waterfallTapMin); }} /></label><label>TO<input aria-label="Last aligned tap" type="number" min={waterfallTapMin + 1} max="63" step="1" value={waterfallTapMax} onchange={(e) => { waterfallTapMax=+e.currentTarget.value; settingChanged('waterfall_tap_max', waterfallTapMax); }} /></label></div>
            <div class="control-group scale-control"><span>COLOR SCALE</span><label class="toggle"><input type="checkbox" bind:checked={waterfallFixedScale} />FIXED</label><label>MIN<input aria-label="Waterfall color scale minimum dB" type="number" min="-180" max={waterfallScaleMax-1} step="1" value={waterfallScaleMin} onchange={(e)=>waterfallScaleChanged('min',+e.currentTarget.value)} /></label><label>MAX<input aria-label="Waterfall color scale maximum dB" type="number" min={waterfallScaleMin+1} max="60" step="1" value={waterfallScaleMax} onchange={(e)=>waterfallScaleChanged('max',+e.currentTarget.value)} /></label><b>dB</b></div>
            <div class="control-group"><span>STATIC REMOVAL</span><label class="toggle"><input type="checkbox" bind:checked={waterfallClutter} onchange={() => settingChanged('waterfall_clutter', waterfallClutter)} />REMOVE CLUTTER</label><label class="toggle" title="Subtract temporal mean magnitude instead of complex I/Q"><input type="checkbox" bind:checked={waterfallMagnitudeClutter} disabled={!waterfallClutter} onchange={() => settingChanged('waterfall_magnitude_clutter', waterfallMagnitudeClutter)} />MAGNITUDE ONLY</label><label class="toggle" title="Fit residual gain, phase, and fractional delay"><input type="checkbox" bind:checked={waterfallNuisanceFit} disabled={!waterfallClutter || waterfallMagnitudeClutter} onchange={() => settingChanged('waterfall_nuisance_fit', waterfallNuisanceFit)} />FIT GAIN/PHASE/DELAY</label></div>
            <div class="control-group"><span>COMPENSATION</span><label class="toggle"><input type="checkbox" bind:checked={waterfallRejectSpikes} onchange={() => settingChanged('waterfall_reject_spikes', waterfallRejectSpikes)} />REJECT ISOLATED SPIKES</label><label class="toggle"><input type="checkbox" bind:checked={waterfallPathLoss} onchange={() => settingChanged('waterfall_path_loss', waterfallPathLoss)} />PATH-LOSS GAIN</label>{#if waterfallPathLoss}<label class="clip-control">NOISE CLIP <output>{waterfallNoiseClipDb} dB</output><input aria-label="Noise clipping threshold in decibels" type="range" min="0" max="40" step="0.5" value={waterfallNoiseClipDb} onchange={(e) => { waterfallNoiseClipDb=+e.currentTarget.value; settingChanged('waterfall_noise_clip_db', waterfallNoiseClipDb); }} /></label>{/if}</div>
          </div>
        {:else if active === 'Slow-Time FFT'}
          <label>HISTORY <output>{slowFftSeconds.toFixed(0)} s</output><input type="range" min="1" max="30" step="1" value={slowFftSeconds} oninput={(e) => { slowFftSeconds=+e.currentTarget.value; settingChanged('slow_fft_history_s',slowFftSeconds); }} /></label>
        {/if}
        {#if ['Live CIR','CIR Waterfall','Slow-Time FFT','Fast-Time FFT'].includes(active)}
          <div class="segmented" aria-label="Scale"><button class:active={!dbMode} onclick={() => dbMode = false}>Linear</button><button class:active={dbMode} onclick={() => dbMode = true}>dB</button></div>
        {/if}
        <label class="node-picker">ACTIVE NODES<select value={nodeCount} onchange={(e) => chooseNodeCount(+e.currentTarget.value)}>{#each [2,3,4,5,6,7,8] as n}<option value={n}>{n}</option>{/each}</select></label>
      </div>
    </section>

    {#if active === 'Board Positions'}
      <BoardPositions {live} {api} {nodeCount} revision={liveRevision} />
    {:else if active === 'Network Health'}
      <section class="health-layout">
        <div class="node-strip">
          {#each Array(nodeCount) as _, i}
            <article class="node-card"><div class="node-id"><span>N{i}</span><i class:offline={status !== 'live' || !nodeHealth(i).samples}></i></div><strong>{nodeHealth(i).delivery === null ? '—' : `${nodeHealth(i).delivery?.toFixed(1)}%`}</strong><small>LINK DELIVERY</small><dl><dt>SAMPLES</dt><dd>{nodeHealth(i).samples}</dd><dt>LINKS</dt><dd>{nodeHealth(i).healthy}/{nodeHealth(i).links}</dd><dt>STATE</dt><dd>{status === 'live' ? (nodeHealth(i).samples ? 'LIVE' : 'NO DATA') : 'OFFLINE'}</dd></dl></article>
          {/each}
        </div>
        <div class="health-lower">
          <article class="panel topology"><header><span>DIRECTED LINK QUALITY</span><b>{links.length} paths</b></header><div class="mini-matrix">{#each links as link}<button onclick={() => { selectedId = link.id; focused = link; }} aria-label={`Focus node ${link.from} to node ${link.to}`} style={`--q:${linkHealth(link).width}%`}><span>{link.from}→{link.to}</span><i></i><small>{linkHealth(link).label}</small></button>{/each}</div></article>
          <article class="panel event-log"><header><span>BACKEND HEALTH SNAPSHOT</span><button onclick={exportHealth}>EXPORT</button></header><div class="events">{#each healthEvents() as event}<div><time>{event[0]}</time><b>{event[1]}</b><span>{event[2]}</span><i class={event[3]}>{event[3]}</i></div>{/each}</div></article>
        </div>
      </section>
    {:else if active === 'Distance Calibration'}
      <section class="cal-layout">
        <article class="panel pair-matrix"><header><span>UNDIRECTED PAIRS</span><b>{calibrationLive?.status ?? 'idle'}</b></header><div>{#each calibrationPairs as link}<button class:active={calibrationPair === link.id} onclick={() => calibrationPair = link.id}><span>{link.from}↔{link.to}</span>{#if pairValue(link.id) !== undefined}<b>{((pairValue(link.id) ?? 0)*100).toFixed(2)}</b><small>cm bias</small>{:else}<b>{referencesM[link.id] || '—'}</b><small>m ref</small>{/if}</button>{/each}</div></article>
        <article class="panel editor"><header><span>CALIBRATION CAPTURE</span><b>{snapshotState.toUpperCase()}</b></header><button class="snapshot" onclick={takeSnapshot} disabled={snapshotState === 'capturing'}>START ONE 10 SECOND SNAPSHOT</button><div class="capture-progress"><i style={`width:${snapshotProgress}%`}></i></div><div class="pair-label">PAIR <span>{calibrationPair.replace('>', ' ↔ ')}</span></div><label>TAPE REFERENCE <span>metres</span><input value={referencesM[calibrationPair] ?? ''} oninput={(e)=>referencesM={...referencesM,[calibrationPair]:e.currentTarget.value}} inputmode="decimal" min="0.01" type="number" step="0.001" placeholder="5.000" /></label><button class="snapshot" onclick={solveCalibration} disabled={snapshotState !== 'complete'}>SOLVE REFERENCES</button><div class="offset-readout"><span>BOARD OFFSETS</span><b>{boardOffsets() || 'Solve to calculate offsets'}</b><span>RESIDUALS</span><b>{residualSummary() || 'No solved residuals'}</b><span>FIT RMSE / REGULARIZATION</span><b>{calibrationSolution ? `${(Number(calibrationSolution.residual_rmse_m)*100).toFixed(2)} cm${calibrationSolution.poor_fit ? ' · WARNING > 5 cm' : ''} · λ ${Number(calibrationSolution.regularization).toExponential(1)}` : 'Not solved'}</b></div><button class="primary" onclick={applyCalibration} disabled={!calibrationSolution || calibrationSolution.has_full_rank !== true}>REVIEW AND APPLY FULL-RANK SOLUTION</button><button class="snapshot" onclick={rollbackCalibration}>ROLL BACK PREVIOUS CALIBRATION</button><p class:error={snapshotState === 'error' || calibrationSolution?.poor_fit === true || (calibrationSolution !== null && calibrationSolution.has_full_rank !== true)}>{calibrationSolution !== null && calibrationSolution.has_full_rank !== true ? 'More independent pair references are required before apply.' : backendMessage}</p></article>
        <article class="panel preview"><header><span>FIT PREVIEW</span><b>RESIDUAL / CENTIMETRES</b></header><div class="preview-plot"><PlotCanvas frame={calibrationPreview} revision={liveRevision} label="Calibration residual preview" /></div><div class="fit-stats"><div><span>RANK</span><b>{Number(calibrationSolution?.rank ?? 0)} / {Number(calibrationSolution?.columns ?? nodeCount)}</b></div><div><span>CONDITION</span><b>{Number(calibrationSolution?.condition_number ?? 0).toFixed(1)}</b></div><div><span>NEXT PAIR</span><b>{recommendedPair()}</b></div></div></article>
      </section>
    {:else if active === 'Radar Map'}
      <RadarMap {live} snapshot={mapSnapshot} dataset={mapDataset} onSnapshot={(value) => mapSnapshot = value} onDataset={(value) => mapDataset = value} onNavigate={(tab) => setTab(tab)} />
    {:else}
      <section class="link-workspace">
        {#if compactMode}
          <aside class="compact-overview panel">
            <header><span>LINKS</span><b>{links.length} directed</b></header>
            <div>{#each links as link}<button class:active={selected.id === link.id} onclick={() => selectedId = link.id}><i></i>{link.from}→{link.to}<b>{linkMetric(link)} cm</b></button>{/each}</div>
          </aside>
          <article class="mobile-detail panel" onpointerdown={(event)=>swipeStartX=event.clientX} onpointerup={(event)=>finishLinkSwipe(event.clientX)} onpointercancel={()=>swipeStartX=null}>
            <header><span>N{selected.from} → N{selected.to}</span><b>{linkMetric(selected)} cm</b></header>
            <div><PlotCanvas frame={plotFor(active, selected)} revision={liveRevision} label={`${active} selected link`} webgl axes={active === 'Live Distance' ? 'bounds' : 'none'} /></div>
            <footer>{#each mobileLegend() as item}<span>{item}</span>{/each}</footer>
          </article>
        {:else}
          <div class="link-grid" style={`--nodes:${nodeCount}`}>
            {#each links as link}
              <button class="link-cell" onclick={() => { selectedId = link.id; focused = link; }} aria-label={`Open ${active} for node ${link.from} to node ${link.to}`}>
                <header><b>N{link.from}<i>→</i>N{link.to}</b><span>{linkMetric(link)} cm</span></header>
                <div class="cell-plot"><PlotCanvas frame={plotFor(active, link)} revision={liveRevision} label={`${active}, node ${link.from} to node ${link.to}`} axes={active === 'Live Distance' ? 'bounds' : 'none'} /></div>
                <footer><span>{linkFooter(link)}</span><i></i></footer>
              </button>
            {/each}
          </div>
        {/if}
      </section>
    {/if}
  </main>

  <footer class="system-foot"><span>GATEWAY USB CDC</span><span>{footerRadio()}</span><span>UTC {new Date().toISOString().slice(11,23)}</span></footer>
</div>

{#if focused}
  <div class="focus-backdrop" role="presentation" onclick={(e) => { if (e.target === e.currentTarget) focused = null; }}>
    <div class="focus-panel" role="dialog" aria-modal="true" tabindex="-1" aria-label={`Focused link ${focused.from} to ${focused.to}`}>
      <header><div><p>{active.toUpperCase()}</p><h2>NODE {focused.from} <i>→</i> NODE {focused.to}</h2></div><div><strong>{linkMetric(focused)} cm</strong><button onclick={() => focused = null} aria-label="Close focus view">CLOSE</button></div></header>
      <div class="focus-plot"><PlotCanvas frame={plotFor(active === 'Network Health' ? 'Live Distance' : active, focused)} revision={liveRevision} label="Focused link plot" webgl axes={active === 'Live Distance' || active === 'Network Health' || active === 'Live CIR' ? 'full' : 'none'} /></div>
      <footer><span>SS RAW <b>{distanceValue(focused,['raw_ss_cm','raw_ss'])}</b></span><span>SS SMOOTHED <b>{distanceValue(focused,['smoothed_ss_cm','smoothed_ss'])}</b></span><span>DS-TWR <b>{distanceValue(focused,['smoothed_ds_cm','smoothed_ds','raw_ds_cm','raw_ds'])}</b></span><span>QUALITY <b>{live.links.get(focused.id)?.quality ?? '—'}</b></span></footer>
    </div>
  </div>
{/if}

<aside class:open={settingsOpen} class="settings" aria-label="Instrument settings">
  <header><div><p>INSTRUMENT</p><h2>Settings</h2></div><button onclick={() => settingsOpen = false} aria-label="Close settings">×</button></header>
  <div class="settings-body">
    <fieldset><legend>Acquisition</legend><label>Slow FFT cadence<select onchange={(e)=>settingChanged('slow_fft_cadence_s',+e.currentTarget.value)}><option value="1">1.0 s</option><option value="0.5">0.5 s</option><option value="2">2.0 s</option></select></label><label>Reference half-life<output>{range.toFixed(1)} s</output><input type="range" min="0.1" max="30" step="0.1" value={range} oninput={(e)=>{range=+e.currentTarget.value;settingChanged('reference_half_life_s',range);}} /></label></fieldset>
    <fieldset><legend>Signal processing</legend><label>CIR alignment<select value={alignmentMode} onchange={(e)=>{alignmentMode=e.currentTarget.value;settingChanged('cir_alignment_mode',alignmentMode);}}><option value="correlation">Reference correlation</option><option value="first_path">DW3000 first path</option></select></label><label>DGC correction<select value={dgcCorrectionDb} onchange={(e)=>{dgcCorrectionDb=+e.currentTarget.value;settingChanged('dgc_correction_db_per_step',dgcCorrectionDb);}}><option value={2.65}>2.65 dB/step (current)</option><option value={6}>6 dB/step (Qorvo RSL)</option><option value={0}>Disabled</option></select></label><label>Distance smoothing<output>{smoothing.toFixed(1)} s</output><input type="range" min="1" max="30" step="0.1" value={smoothing} oninput={(e)=>{smoothing=+e.currentTarget.value;settingChanged('distance_smoothing_s',smoothing);}} /></label><label>Hampel radius<input type="number" min="0" max="64" value="5" onchange={(e)=>settingChanged('hampel_radius',+e.currentTarget.value)} /></label><label>FFT window<select onchange={(e)=>settingChanged('fft_window',e.currentTarget.value)}><option value="hann">Hann</option><option value="hamming">Hamming</option><option value="blackman">Blackman</option><option value="rectangular">Rectangular</option></select></label></fieldset>
    <fieldset><legend>Live CIR robust grid</legend><label>Reference<select value={cirGridReferenceMode} onchange={(e)=>{cirGridReferenceMode=e.currentTarget.value;settingChanged('cir_grid_reference_mode',cirGridReferenceMode);}}><option value="frozen">Frozen board</option><option value="rolling_medoid">Rolling medoid</option><option value="rolling_mean">Rolling mean</option></select></label><label>Reference window<input type="number" min="3" max="64" step="1" value={cirGridReferenceWindow} onchange={(e)=>{cirGridReferenceWindow=+e.currentTarget.value;settingChanged('cir_grid_reference_window',cirGridReferenceWindow);}} /></label><label>Reference peak dB<input type="number" min="-30" max="0" step="1" value={cirGridReferencePeakDb} onchange={(e)=>{cirGridReferencePeakDb=+e.currentTarget.value;settingChanged('cir_grid_reference_peak_db',cirGridReferencePeakDb);}} /></label><label>Score<select value={cirGridScoreMode} onchange={(e)=>{cirGridScoreMode=e.currentTarget.value;settingChanged('cir_grid_score_mode',cirGridScoreMode);}}><option value="soft">Soft weights</option><option value="count">Match count</option></select></label><label>Gain min dB<input type="number" min="-40" max={cirGridGainMaxDb-.5} step=".5" value={cirGridGainMinDb} onchange={(e)=>{cirGridGainMinDb=+e.currentTarget.value;settingChanged('cir_grid_gain_min_db',cirGridGainMinDb);}} /></label><label>Gain max dB<input type="number" min={cirGridGainMinDb+.5} max="40" step=".5" value={cirGridGainMaxDb} onchange={(e)=>{cirGridGainMaxDb=+e.currentTarget.value;settingChanged('cir_grid_gain_max_db',cirGridGainMaxDb);}} /></label><label>Delay +/- taps<input type="number" min="0" max="8" step=".25" value={cirGridDelayRange} onchange={(e)=>{cirGridDelayRange=+e.currentTarget.value;settingChanged('cir_grid_delay_range_samples',cirGridDelayRange);}} /></label><label>Resample P<select value={cirGridResamplingFactor} onchange={(e)=>{cirGridResamplingFactor=+e.currentTarget.value;settingChanged('cir_grid_resampling_factor',cirGridResamplingFactor);}}>{#each [1,2,4,8,16] as factor}<option value={factor}>{factor}x</option>{/each}</select></label><label>Eta<input type="number" min=".01" max="4" step=".01" value={cirGridEta} onchange={(e)=>{cirGridEta=+e.currentTarget.value;settingChanged('cir_grid_eta',cirGridEta);}} /></label></fieldset>
    <fieldset><legend>Display</legend><label><span>Peak markers</span><input type="checkbox" checked /></label><label><span>30 FPS plots</span><input type="checkbox" checked disabled /></label></fieldset>
    <fieldset><legend>Protected clips</legend><label>Name<input maxlength="120" bind:value={clipName} placeholder="Optional label" /></label><label>Note<input maxlength="2000" bind:value={clipNote} placeholder="Optional context" /></label><label>Length<select bind:value={clipDurationS}>{#each [5,10,15,30,60] as seconds}<option value={seconds}>{seconds} s</option>{/each}</select></label><button class="snapshot" onclick={saveClip} disabled={clipState === 'capturing'}>SAVE {clipDurationS} SECOND CLIP</button><div class="capture-progress"><i style={`width:${clipProgress}%`}></i></div>{#each clips as clip}<p>{String((clip.value as Record<string,unknown>)?.status ?? 'unknown').toUpperCase()} · CLIP {String(clip.id)} {String((clip.value as Record<string,unknown>)?.name ?? '')} {#if (clip.value as Record<string,unknown>)?.status === 'complete'}<a href={api?.clipDownload(Number(clip.id))}>DOWNLOAD ZIP</a>{/if} <button class="text-button" onclick={()=>deleteClip(Number(clip.id))} disabled={(clip.value as Record<string,unknown>)?.status === 'collecting'}>DELETE</button></p>{/each}</fieldset>
    <fieldset><legend>Backend</legend><label>REST base<input value="/api" readonly /></label><p>{backendMessage}. Binary HMT1 WebSocket follows the active analysis topic and reconnects with exponential backoff.</p></fieldset>
  </div>
</aside>
{#if settingsOpen}<button class="drawer-shade" onclick={() => settingsOpen = false} aria-label="Close settings"></button>{/if}
