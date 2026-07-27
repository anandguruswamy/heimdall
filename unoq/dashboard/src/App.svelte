<script lang="ts">
  import { onMount } from 'svelte';
  import PlotCanvas from './lib/PlotCanvas.svelte';
  import { HeimdallApi } from './lib/api';
  import { linksFor } from './lib/demo';
  import { LiveStore } from './lib/live';
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
  let compactMode = $state(false);
  let waterfallSeconds = $state(5);
  let slowFftSeconds = $state(2);
  let settingsTimer: ReturnType<typeof setTimeout> | undefined;
  let healthTimer: ReturnType<typeof setInterval> | undefined;
  let swipeStartX: number | null = null;
  let api = $state.raw(null as unknown as HeimdallApi);
  const live = new LiveStore(() => liveRevision++);

  const links = $derived(linksFor(nodeCount));
  const selected = $derived(links.find((link) => link.id === selectedId) ?? links[0]);
  const calibrationPairs = $derived(links.filter((link) => link.from < link.to));
  const calibrationLive = $derived.by(() => { void liveRevision; return live.calibration?.live && typeof live.calibration.live === 'object' ? live.calibration.live as Record<string, unknown> : null; });
  const EMPTY_FRAME: PlotFrame = { series: [], min: 0, max: 1 };
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
      if (topic === 'fast-fft' && periodogramMode && frame?.series?.[0]) {
        const original = frame.series[0].data;
        const squared = new Float32Array(original.length);
        for (let i = 0; i < original.length; i++) squared[i] = original[i] * original[i];
        const [min, max] = [0, Math.max(...squared)];
        return { series: [{ data: squared, color: frame.series[0].color }], min, max, xLabel: frame.xLabel, yLabel: 'power' };
      }
      if (topic === 'waterfall' && waterfallFixedScale && frame) {
        const scaleMin = dbMode ? waterfallScaleMin : Math.pow(10, waterfallScaleMin / 20);
        const scaleMax = dbMode ? waterfallScaleMax : Math.pow(10, waterfallScaleMax / 20);
        frame = { ...frame, min: scaleMin, max: scaleMax };
      }
      if (dbMode && frame) return toDbFrame(frame);
      return frame ?? EMPTY_FRAME;
    };
  }

  function toDbFrame(frame: PlotFrame): PlotFrame {
    const toDb = (value: number) => 20 * Math.log10(Math.max(value, 1e-12));
    const convert = (src: Float32Array) => { const out = new Float32Array(src.length); for (let i = 0; i < src.length; i++) out[i] = toDb(src[i]); return out; };
    const series = frame.series?.map((s) => ({ ...s, data: convert(s.data) }));
    const heatmap = frame.heatmap ? convert(frame.heatmap) : undefined;
    const min = frame.min !== undefined && frame.max !== undefined ? toDb(Math.max(frame.min, 1e-12)) : undefined;
    const max = frame.min !== undefined && frame.max !== undefined ? toDb(frame.max) : undefined;
    return { ...frame, series, heatmap, min, max, yLabel: 'dB' };
  }

  function topicFor(tab: Tab): TopicKey {
    return ({ 'Network Health':'health','Live Distance':'distance','Instantaneous CIR':'cir','CIR Waterfall':'waterfall','Slow-Time FFT':'slow-fft','Fast-Time FFT':'fast-fft','CFO':'cfo','Distance Calibration':'calibration' } as const)[tab];
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
    const qualities = related.map((link) => Number((link.latest_cir as Record<string,unknown> | undefined)?.quality)).filter(Number.isFinite);
    return { delivery: qualities.length ? Math.min(100, qualities.filter((value)=>value===0).length / qualities.length * 100) : null, samples: related.reduce((sum,link)=>sum+Number(link.observations ?? 0),0) };
  }

  function linkHealth(link: Link) {
    void liveRevision;
    const topologyLinks = Array.isArray(live.topology?.links) ? live.topology.links as Record<string,unknown>[] : [];
    const item = topologyLinks.find((value) => Number(value.from) === link.from && Number(value.to) === link.to);
    const quality = Number((item?.latest_cir as Record<string,unknown> | undefined)?.quality);
    return !item ? { label: 'NO DATA', width: 0 } : quality === 0 ? { label: 'OK', width: 100 } : { label: `Q${quality}`, width: 35 };
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
    if (active === 'Instantaneous CIR') return ['DISPLAY INTERPOLATION','ALIGNED TAPS','FIRST PATH'];
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
    let round = 0;
    live.links.forEach((link) => Object.values(link.payloads ?? {}).forEach((payload) => { const sample=payload?.sample as Record<string,unknown>|undefined; round=Math.max(round,Number(payload?.round ?? sample?.round ?? 0)); }));
    return round ? round.toLocaleString() : '—';
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
      [time,'SYS',`Clients ${Number(live.health?.websocket_clients??0)} · queue drops ${Number(live.health?.processing_queue_drops??0)} · RSS ${(Number((live.health?.process as Record<string,unknown>|undefined)?.rss_bytes??0)/1_000_000).toFixed(1)} MB`,'info'],
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

  async function saveClip() {
    if (clipState === 'capturing') return;
    clipState = 'capturing'; clipProgress = 0; backendMessage = 'Capturing 30 seconds before and after trigger…';
    try {
      const created=await api.saveClip(clipName.trim(),clipNote.trim()) as Record<string,unknown>;
      const clipId=Number(created.id);
      const started = Date.now();
      while (Date.now() - started < 65_000) {
        await new Promise((resolve) => setTimeout(resolve, 1_000));
        clipProgress = Math.min(99, (Date.now() - started) / 600);
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
      if (data.settings) { live.settings = data.settings as Record<string,unknown>; const value=(live.settings.value ?? {}) as Record<string,unknown>; halfLife=Number(value.cfo_half_life_s ?? 2); smoothing=Number(value.distance_smoothing_s ?? 1); range=Number(value.reference_half_life_s ?? 4); slowFftSeconds=Number(value.slow_fft_history_s ?? 2); waterfallClutter=Boolean(value.waterfall_clutter); waterfallMagnitudeClutter=value.waterfall_magnitude_clutter !== false; waterfallNuisanceFit=value.waterfall_nuisance_fit !== false; waterfallRejectSpikes=value.waterfall_reject_spikes !== false; waterfallPathLoss=Boolean(value.waterfall_path_loss); waterfallNoiseClipDb=Number(value.waterfall_noise_clip_db ?? 12); waterfallTapMin=Number(value.waterfall_tap_min ?? -20); waterfallTapMax=Number(value.waterfall_tap_max ?? 50); }
      if (data.calibration) {
        live.calibration = data.calibration as Record<string,unknown>;
        const calibration=data.calibration as Record<string,unknown>, liveState=calibration.live as Record<string,unknown>|undefined, applied=calibration.applied as Record<string,unknown>|undefined, stored=applied?.value as Record<string,unknown>|undefined;
        if (liveState?.status === 'complete') { snapshotState='complete'; snapshotProgress=100; }
        if (stored?.solution && typeof stored.solution === 'object') calibrationSolution=stored.solution as Record<string,unknown>;
      }
      backendMessage = data.health || data.topology || data.settings ? 'Backend connected' : 'Backend unavailable · no live data'; liveRevision++;
    }, (message) => { live.lastError = message; backendMessage = message; liveRevision++; }, () => {
      live.resetStream();
      backendMessage = 'Resynchronizing live state';
    });
    void api.getClips().then((value) => { clips = Array.isArray(value) ? value as Record<string,unknown>[] : []; });
    api.subscribe(active);
    api.connect();
    healthTimer=setInterval(async()=>{ if(active!=='Network Health') return; try { const [health,topology]=await Promise.all([api.get('/health'),api.get('/topology')]); live.health=health as Record<string,unknown>; live.loadTopology(topology); liveRevision++; } catch {} },2_000);
    return () => { clearTimeout(settingsTimer); clearInterval(healthTimer); compact.removeEventListener('change', updateCompact); api.close(); };
  });
</script>

<svelte:head><meta name="description" content="Heimdall UWB sensing instrument" /></svelte:head>

<div class="instrument">
  <header class="topbar">
    <div class="identity">
      <div class="mark" aria-hidden="true"><i></i><i></i><i></i></div>
      <div><strong>HEIMDALL</strong><span>UWB FUSION INSTRUMENT</span></div>
    </div>
    <div class="header-metrics">
      <div><span>ROUND</span><b>{headerRound()}</b></div>
      <div><span>RATE</span><b>{headerRate()} <small>Hz</small></b></div>
      <div><span>NODES</span><b>{nodeCount}/8</b></div>
    </div>
    <div class="actions">
      {#if !calibrated()}<span class="calibration-warning">UNCALIBRATED</span>{/if}
      <span class:offline={status === 'offline'} class="status"><i></i>{status.toUpperCase()}</span>
      <button class="icon-button" onclick={saveClip} disabled={clipState === 'capturing'}>{clipState === 'capturing' ? `CLIP ${Math.round(clipProgress)}%` : 'CAPTURE'}</button>
      <button class="icon-button" onclick={() => settingsOpen = !settingsOpen} aria-expanded={settingsOpen} aria-label="Open settings">TUNE</button>
    </div>
  </header>

  <nav class="tabs" aria-label="Analysis modes">
    {#each tabs as tab, i}
      <button class:active={active === tab} onclick={() => setTab(tab)}><em>{String(i + 1).padStart(2, '0')}</em>{tab}</button>
    {/each}
  </nav>

  <main>
    <section class="mode-head">
      <div><p>ANALYSIS / {active.toUpperCase()}</p><h1>{active}</h1></div>
      <div class="mode-controls">
        {#if active === 'Fast-Time FFT'}
          <div class="segmented" aria-label="FFT display"><button class:active={!phaseMode && !periodogramMode} onclick={() => { phaseMode = false; periodogramMode = false; }}>Magnitude</button><button class:active={!periodogramMode && phaseMode} onclick={() => { phaseMode = true; periodogramMode = false; }}>Phase</button><button class:active={periodogramMode} onclick={() => { phaseMode = false; periodogramMode = true; }}>Periodogram</button></div>
        {:else if active === 'CFO'}
          <label>HALF-LIFE <output>{halfLife.toFixed(1)} s</output><input type="range" min="0.1" max="30" step="0.1" value={halfLife} oninput={(e) => { halfLife=+e.currentTarget.value; settingChanged('cfo_half_life_s',halfLife); }} /></label>
        {:else if active === 'CIR Waterfall'}
          <label>HISTORY <output>{waterfallSeconds.toFixed(0)} s</output><input type="range" min="1" max="30" step="1" value={waterfallSeconds} oninput={(e) => { waterfallSeconds=+e.currentTarget.value; live.setWaterfallSeconds(waterfallSeconds); }} /></label>
          <label>TAPS<output>{waterfallTapMin}..{waterfallTapMax}</output><input type="range" min="-64" max="63" step="1" value={waterfallTapMin} oninput={(e) => { waterfallTapMin=+e.currentTarget.value; settingChanged('waterfall_tap_min', waterfallTapMin); }} /><input type="range" min="-64" max="63" step="1" value={waterfallTapMax} oninput={(e) => { waterfallTapMax=+e.currentTarget.value; settingChanged('waterfall_tap_max', waterfallTapMax); }} /></label>
          <label>FIXED dB <output>{waterfallScaleMin}..{waterfallScaleMax}</output><input type="checkbox" bind:checked={waterfallFixedScale} onchange={(e) => settingChanged('waterfall_fixed_scale_min', waterfallFixedScale ? waterfallScaleMin : 0)} /></label>
          <label><input type="checkbox" bind:checked={waterfallClutter} onchange={() => settingChanged('waterfall_clutter', waterfallClutter)} />CLUTTER</label>
          <label><input type="checkbox" bind:checked={waterfallMagnitudeClutter} disabled={!waterfallClutter} onchange={() => settingChanged('waterfall_magnitude_clutter', waterfallMagnitudeClutter)} />MAG ONLY</label>
          <label><input type="checkbox" bind:checked={waterfallNuisanceFit} disabled={!waterfallClutter || waterfallMagnitudeClutter} onchange={() => settingChanged('waterfall_nuisance_fit', waterfallNuisanceFit)} />NUIS FIT</label>
          <label><input type="checkbox" bind:checked={waterfallRejectSpikes} onchange={() => settingChanged('waterfall_reject_spikes', waterfallRejectSpikes)} />REJECT SPIKE</label>
          <label><input type="checkbox" bind:checked={waterfallPathLoss} onchange={() => settingChanged('waterfall_path_loss', waterfallPathLoss)} />PATH LOSS</label>
          {#if waterfallPathLoss}
            <label>CLIP dB<output>{waterfallNoiseClipDb}</output><input type="range" min="0" max="40" step="0.5" value={waterfallNoiseClipDb} oninput={(e) => { waterfallNoiseClipDb=+e.currentTarget.value; settingChanged('waterfall_noise_clip_db', waterfallNoiseClipDb); }} /></label>
          {/if}
        {:else if active === 'Slow-Time FFT'}
          <label>HISTORY <output>{slowFftSeconds.toFixed(0)} s</output><input type="range" min="1" max="30" step="1" value={slowFftSeconds} oninput={(e) => { slowFftSeconds=+e.currentTarget.value; settingChanged('slow_fft_history_s',slowFftSeconds); }} /></label>
        {/if}
        {#if ['Instantaneous CIR','CIR Waterfall','Slow-Time FFT','Fast-Time FFT'].includes(active)}
          <div class="segmented" aria-label="Scale"><button class:active={!dbMode} onclick={() => dbMode = false}>Linear</button><button class:active={dbMode} onclick={() => dbMode = true}>dB</button></div>
        {/if}
        <label class="node-picker">ACTIVE NODES<select value={nodeCount} onchange={(e) => chooseNodeCount(+e.currentTarget.value)}>{#each [2,3,4,5,6,7,8] as n}<option value={n}>{n}</option>{/each}</select></label>
      </div>
    </section>

    {#if active === 'Network Health'}
      <section class="health-layout">
        <div class="node-strip">
          {#each Array(nodeCount) as _, i}
            <article class="node-card"><div class="node-id"><span>N{i}</span><i class:offline={status !== 'live' || !nodeHealth(i).samples}></i></div><strong>{nodeHealth(i).delivery === null ? '—' : `${nodeHealth(i).delivery?.toFixed(1)}%`}</strong><small>HEALTHY CIR LINKS</small><dl><dt>SAMPLES</dt><dd>{nodeHealth(i).samples}</dd><dt>SYNC</dt><dd>UNKNOWN</dd><dt>STATE</dt><dd>{status === 'live' ? (nodeHealth(i).samples ? 'LIVE' : 'NO DATA') : 'OFFLINE'}</dd></dl></article>
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
        <article class="panel preview"><header><span>FIT PREVIEW</span><b>RESIDUAL / CENTIMETRES</b></header><div class="preview-plot"><PlotCanvas frame={calibrationPreview} label="Calibration residual preview" /></div><div class="fit-stats"><div><span>RANK</span><b>{Number(calibrationSolution?.rank ?? 0)} / {Number(calibrationSolution?.columns ?? nodeCount)}</b></div><div><span>CONDITION</span><b>{Number(calibrationSolution?.condition_number ?? 0).toFixed(1)}</b></div><div><span>NEXT PAIR</span><b>{recommendedPair()}</b></div></div></article>
      </section>
    {:else}
      <section class="link-workspace">
        {#if compactMode}
          <aside class="compact-overview panel">
            <header><span>LINKS</span><b>{links.length} directed</b></header>
            <div>{#each links as link}<button class:active={selected.id === link.id} onclick={() => selectedId = link.id}><i></i>{link.from}→{link.to}<b>{linkMetric(link)} cm</b></button>{/each}</div>
          </aside>
          <article class="mobile-detail panel" onpointerdown={(event)=>swipeStartX=event.clientX} onpointerup={(event)=>finishLinkSwipe(event.clientX)} onpointercancel={()=>swipeStartX=null}>
            <header><span>N{selected.from} → N{selected.to}</span><b>{linkMetric(selected)} cm</b></header>
            <div><PlotCanvas frame={plotFor(active, selected)} label={`${active} selected link`} webgl /></div>
            <footer>{#each mobileLegend() as item}<span>{item}</span>{/each}</footer>
          </article>
        {:else}
          <div class="link-grid" style={`--nodes:${nodeCount}`}>
            {#each links as link}
              <button class="link-cell" onclick={() => { selectedId = link.id; focused = link; }} aria-label={`Open ${active} for node ${link.from} to node ${link.to}`}>
                <header><b>N{link.from}<i>→</i>N{link.to}</b><span>{linkMetric(link)} cm</span></header>
                <div class="cell-plot"><PlotCanvas frame={plotFor(active, link)} label={`${active}, node ${link.from} to node ${link.to}`} /></div>
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
      <div class="focus-plot"><PlotCanvas frame={plotFor(active === 'Network Health' ? 'Live Distance' : active, focused)} label="Focused link plot" webgl /></div>
      <footer><span>SS RAW <b>{distanceValue(focused,['raw_ss_cm','raw_ss'])}</b></span><span>SS SMOOTHED <b>{distanceValue(focused,['smoothed_ss_cm','smoothed_ss'])}</b></span><span>DS-TWR <b>{distanceValue(focused,['smoothed_ds_cm','smoothed_ds','raw_ds_cm','raw_ds'])}</b></span><span>QUALITY <b>{live.links.get(focused.id)?.quality ?? '—'}</b></span></footer>
    </div>
  </div>
{/if}

<aside class:open={settingsOpen} class="settings" aria-label="Instrument settings">
  <header><div><p>INSTRUMENT</p><h2>Settings</h2></div><button onclick={() => settingsOpen = false} aria-label="Close settings">×</button></header>
  <div class="settings-body">
    <fieldset><legend>Acquisition</legend><label>Slow FFT cadence<select onchange={(e)=>settingChanged('slow_fft_cadence_s',+e.currentTarget.value)}><option value="1">1.0 s</option><option value="0.5">0.5 s</option><option value="2">2.0 s</option></select></label><label>Reference half-life<output>{range.toFixed(1)} s</output><input type="range" min="0.1" max="30" step="0.1" value={range} oninput={(e)=>{range=+e.currentTarget.value;settingChanged('reference_half_life_s',range);}} /></label></fieldset>
    <fieldset><legend>Signal processing</legend><label>Distance smoothing<output>{smoothing.toFixed(1)} s</output><input type="range" min="1" max="30" step="0.1" value={smoothing} oninput={(e)=>{smoothing=+e.currentTarget.value;settingChanged('distance_smoothing_s',smoothing);}} /></label><label>Hampel radius<input type="number" min="0" max="64" value="5" onchange={(e)=>settingChanged('hampel_radius',+e.currentTarget.value)} /></label><label>FFT window<select onchange={(e)=>settingChanged('fft_window',e.currentTarget.value)}><option value="hann">Hann</option><option value="hamming">Hamming</option><option value="blackman">Blackman</option><option value="rectangular">Rectangular</option></select></label></fieldset>
    <fieldset><legend>Display</legend><label><span>Peak markers</span><input type="checkbox" checked /></label><label><span>30 FPS plots</span><input type="checkbox" checked disabled /></label></fieldset>
    <fieldset><legend>Protected clips</legend><label>Name<input maxlength="120" bind:value={clipName} placeholder="Optional label" /></label><label>Note<input maxlength="2000" bind:value={clipNote} placeholder="Optional context" /></label><button class="snapshot" onclick={saveClip} disabled={clipState === 'capturing'}>SAVE 30 + 30 SECOND CLIP</button><div class="capture-progress"><i style={`width:${clipProgress}%`}></i></div>{#each clips as clip}<p>{String((clip.value as Record<string,unknown>)?.status ?? 'unknown').toUpperCase()} · CLIP {String(clip.id)} {String((clip.value as Record<string,unknown>)?.name ?? '')} {#if (clip.value as Record<string,unknown>)?.status === 'complete'}<a href={api?.clipDownload(Number(clip.id))}>DOWNLOAD ZIP</a>{/if} <button class="text-button" onclick={()=>deleteClip(Number(clip.id))} disabled={(clip.value as Record<string,unknown>)?.status === 'collecting'}>DELETE</button></p>{/each}</fieldset>
    <fieldset><legend>Backend</legend><label>REST base<input value="/api" readonly /></label><p>{backendMessage}. Binary HMT1 WebSocket follows the active analysis topic and reconnects with exponential backoff.</p></fieldset>
  </div>
</aside>
{#if settingsOpen}<button class="drawer-shade" onclick={() => settingsOpen = false} aria-label="Close settings"></button>{/if}
