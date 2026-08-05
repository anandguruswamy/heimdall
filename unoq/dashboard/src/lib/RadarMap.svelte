<script lang="ts">
  import { onMount } from 'svelte';
  import MapScene from './MapScene.svelte';
  import { LiveStore } from './live';
  import type { Tab } from './types';
  import {
    DEFAULT_MAP_CONFIG,
    DEFAULT_RECONSTRUCTION_CONFIG,
    boundsFromPositions,
    buildGrid,
    buildLinkProfiles,
    geometryFromBoardFreeze,
    reconstruct,
    volumeToPoints,
    type LinkProfile,
    type LinkStats,
    type MapBounds,
    type MapSnapshot,
    type ReconstructionConfig,
  } from './map/map-engine';
  import { nearestSamples, parseDatasetZip, type Dataset } from './map/dataset';

  let {
    live,
    liveRevision,
    snapshot,
    dataset,
    onSnapshot,
    onDataset,
    onNavigate,
  }: {
    live: LiveStore;
    liveRevision: number;
    snapshot: MapSnapshot | null;
    dataset: Dataset | null;
    onSnapshot: (snapshot: MapSnapshot) => void;
    onDataset: (dataset: Dataset) => void;
    onNavigate: (tab: Tab) => void;
  } = $props();

  let mode = $state<'live' | 'dataset'>('live');
  let frames = $state(32);
  let spacing = $state(0.1);
  let percentile = $state(85);
  let pointSize = $state(3);
  let message = $state('Waiting for aligned CIR samples on this tab…');
  let timePct = $state(50);
  let playing = $state(false);
  let playSpeed = $state(1);
  let datasetMap = $state<MapSnapshot | null>(null);
  let datasetMessage = $state('');
  let importBusy = $state(false);
  let fileInput = $state<HTMLInputElement | undefined>(undefined);
  let computeRaf = 0;
  let playRaf = 0;
  let lastPlayTick = 0;
  let mapBounds = $state<MapBounds | null>(null);
  let boundsPadding = $state<number | null>(null);
  let linkStatsOpen = $state(false);
  let reconstruction = $state<ReconstructionConfig>({ ...DEFAULT_RECONSTRUCTION_CONFIG });
  let currentGeometryKey = '';
  const perGeometryBounds = new Map<string, MapBounds>();
  let recomputeRaf = 0;
  let liveFrozen = $state(false);
  let liveUpdateTimer: ReturnType<typeof setTimeout> | undefined;
  let liveUpdateRaf = 0;
  let lastLiveUpdateAt = 0;
  const LIVE_UPDATE_INTERVAL_MS = 500;

  const liveGeometry = $derived(geometryFromBoardFreeze(live.boardFreeze));
  const datasetGeometry = $derived(dataset?.geometry ?? null);
  const geometry = $derived(mode === 'live' ? liveGeometry : datasetGeometry);
  const geometryKey = $derived(
    geometry ? geometry.positions.map((point) => `${point.x.toFixed(4)},${point.y.toFixed(4)},${point.z.toFixed(4)}`).join('|') : ''
  );
  const sceneBounds = $derived(
    mapBounds
      ? {
          min: { x: mapBounds.min[0], y: mapBounds.min[1], z: mapBounds.min[2] },
          max: { x: mapBounds.max[0], y: mapBounds.max[1], z: mapBounds.max[2] },
        }
      : null
  );
  const activeSnapshot = $derived(mode === 'live' ? snapshot : datasetMap);
  const cloud = $derived(activeSnapshot ? volumeToPoints(activeSnapshot.volume, percentile, 50_000) : null);
  const nodes = $derived(geometry?.positions ?? []);
  const voxelCount = $derived(
    activeSnapshot
      ? activeSnapshot.grid.shape[0] * activeSnapshot.grid.shape[1] * activeSnapshot.grid.shape[2]
      : 0
  );
  const peakSupport = $derived.by(() => {
    if (!activeSnapshot) return '—';
    const { volume, supportLinks, validLinks, consensus } = activeSnapshot.volume;
    let peak = -Infinity;
    let index = -1;
    for (let i = 0; i < volume.length; i++) {
      if (volume[i] > peak && activeSnapshot.volume.confidence[i] > 0) {
        peak = volume[i];
        index = i;
      }
    }
    return index < 0 ? '—' : `${supportLinks[index]}/${validLinks[index]} · ${(consensus[index] * 100).toFixed(0)}%`;
  });
  const timeDisplay = $derived.by(() => {
    if (!dataset || dataset.eventMax <= dataset.eventMin) return '—';
    const time = dataset.eventMin + (timePct / 100) * (dataset.eventMax - dataset.eventMin);
    return `${(time - dataset.eventMin).toFixed(2)} s / ${(dataset.eventMax - dataset.eventMin).toFixed(2)} s`;
  });

  $effect(() => {
    const key = geometryKey;
    if (!geometry || key === currentGeometryKey) return;
    currentGeometryKey = key;
    applyBounds(
      perGeometryBounds.get(key) ??
        boundsFromPositions(geometry.positions, boundsPadding === null ? undefined : boundsPadding)
    );
  });

  $effect(() => {
    void dataset;
    void timePct;
    void frames;
    void spacing;
    void mode;
    void mapBounds;
    void reconstruction.mode;
    void reconstruction.mgbpMadMultiplier;
    void reconstruction.mgbpMinValid;
    void reconstruction.mgbpMinRetained;
    void reconstruction.cgbpNoiseStartTap;
    void reconstruction.cgbpNoiseEndTap;
    void reconstruction.cgbpNoiseMargin;
    void reconstruction.cgbpConsensusFraction;
    void reconstruction.cgbpMinValid;
    void reconstruction.cgbpMinActive;
    void reconstruction.cgbpVoteBasis;
    if (mode !== 'dataset') return;
    scheduleDatasetCompute();
  });

  $effect(() => {
    void mapBounds;
    void frames;
    void spacing;
    void reconstruction.mode;
    void reconstruction.mgbpMadMultiplier;
    void reconstruction.mgbpMinValid;
    void reconstruction.mgbpMinRetained;
    void reconstruction.cgbpNoiseStartTap;
    void reconstruction.cgbpNoiseEndTap;
    void reconstruction.cgbpNoiseMargin;
    void reconstruction.cgbpConsensusFraction;
    void reconstruction.cgbpMinValid;
    void reconstruction.cgbpMinActive;
    void reconstruction.cgbpVoteBasis;
    if (mode !== 'live') return;
    if (liveFrozen) scheduleLiveRecompute();
    else scheduleLiveUpdate();
  });

  $effect(() => {
    void liveRevision;
    if (mode !== 'live' || liveFrozen) return;
    scheduleLiveUpdate();
  });

  function applyBounds(next: MapBounds): void {
    perGeometryBounds.set(currentGeometryKey, next);
    mapBounds = next;
  }

  function reconstructionConfig(): ReconstructionConfig {
    return { ...reconstruction };
  }

  function resetBounds(): void {
    if (!geometry) return;
    applyBounds(boundsFromPositions(geometry.positions, boundsPadding === null ? undefined : boundsPadding));
  }

  function setBound(axis: 0 | 1 | 2, edge: 'min' | 'max', value: number): void {
    if (!mapBounds) return;
    const next: MapBounds = {
      min: [...mapBounds.min],
      max: [...mapBounds.max],
    };
    if (edge === 'min') next.min[axis] = Math.min(value, next.max[axis]);
    else next.max[axis] = Math.max(value, next.min[axis]);
    applyBounds(next);
  }

  function scheduleLiveRecompute(): void {
    if (recomputeRaf) return;
    recomputeRaf = requestAnimationFrame(() => {
      recomputeRaf = 0;
      if (mode !== 'live' || !snapshot?.linkProfiles?.length || !geometry) return;
      const grid = buildGrid(geometry, spacing, DEFAULT_MAP_CONFIG.maxVoxels, mapBounds ?? undefined);
      const nextReconstruction = reconstructionConfig();
      const volume = reconstruct(snapshot.linkProfiles, geometry, grid, nextReconstruction);
      onSnapshot({ ...snapshot, grid, spacingM: spacing, reconstruction: nextReconstruction, volume });
    });
  }

  function scheduleLiveUpdate(): void {
    if (liveFrozen || liveUpdateTimer || liveUpdateRaf) return;
    const delay = Math.max(0, LIVE_UPDATE_INTERVAL_MS - (performance.now() - lastLiveUpdateAt));
    liveUpdateTimer = setTimeout(() => {
      liveUpdateTimer = undefined;
      liveUpdateRaf = requestAnimationFrame(() => {
        liveUpdateRaf = 0;
        rebuildLiveMap();
      });
    }, delay);
  }

  function toggleLiveFreeze(): void {
    liveFrozen = !liveFrozen;
    if (liveFrozen) {
      if (liveUpdateTimer) clearTimeout(liveUpdateTimer);
      liveUpdateTimer = undefined;
      cancelAnimationFrame(liveUpdateRaf);
      liveUpdateRaf = 0;
      message = snapshot ? 'Map frozen at the latest reconstructed live window' : 'Map frozen before a qualified live window was available';
      return;
    }
    message = 'Live map updates enabled';
    scheduleLiveUpdate();
  }

  function scheduleDatasetCompute(): void {
    if (computeRaf) return;
    computeRaf = requestAnimationFrame(() => {
      computeRaf = 0;
      computeDataset();
    });
  }

  function computeDataset(): void {
    const current = dataset;
    if (!current) return;
    if (!current.geometry) {
      datasetMap = null;
      datasetMessage = 'This dataset carries no usable antenna geometry';
      return;
    }
    const time = current.eventMin + (timePct / 100) * (current.eventMax - current.eventMin);
    const config = { ...DEFAULT_MAP_CONFIG, frames, spacingM: spacing };
    const profiles: LinkProfile[] = [];
    const stats: LinkStats[] = [];
    for (const link of current.links) {
      const samples = nearestSamples(link.samples, time, config.frames);
      const profile = buildLinkProfiles(samples, config);
      if (profile) {
        profiles.push(profile);
        stats.push({
          from: link.from,
          to: link.to,
          frames: samples.length,
          accepted: profile.acceptedFrames,
          medianCorrelation: profile.medianCorrelation,
        });
      }
    }
    if (!profiles.length) {
      datasetMap = null;
      datasetMessage = 'No qualified aligned CIR frames at this instant';
      return;
    }
    const grid = buildGrid(current.geometry, spacing, DEFAULT_MAP_CONFIG.maxVoxels, mapBounds ?? undefined);
    const reconstructionConfigValue = reconstructionConfig();
    const volume = reconstruct(profiles, current.geometry, grid, reconstructionConfigValue);
    datasetMap = {
      profiles: stats,
      linkProfiles: profiles,
      grid,
      geometry: current.geometry,
      spacingM: spacing,
      framesConfig: frames,
      reconstruction: reconstructionConfigValue,
      takenAt: time,
      volume,
    };
    datasetMessage = `${stats.length} link${stats.length === 1 ? '' : 's'} · ${grid.shape[0]}×${grid.shape[1]}×${grid.shape[2]} voxels`;
  }

  function togglePlay(): void {
    if (!dataset || dataset.eventMax <= dataset.eventMin) return;
    if (playing) {
      playing = false;
      cancelAnimationFrame(playRaf);
      return;
    }
    playing = true;
    lastPlayTick = performance.now();
    const tick = () => {
      if (!playing || !dataset) return;
      const now = performance.now();
      const dt = (now - lastPlayTick) / 1000;
      lastPlayTick = now;
      const span = dataset.eventMax - dataset.eventMin;
      if (span > 0) timePct = (timePct + (dt * playSpeed) / span * 100) % 100;
      playRaf = requestAnimationFrame(tick);
    };
    playRaf = requestAnimationFrame(tick);
  }

  async function onFileSelected(): Promise<void> {
    const file = fileInput?.files?.[0];
    if (!file) return;
    importBusy = true;
    try {
      const parsed = await parseDatasetZip(file);
      onDataset(parsed);
      datasetMap = null;
      timePct = 50;
      mode = 'dataset';
      datasetMessage = `Loaded ${parsed.name || file.name} · ${parsed.sampleCount.toLocaleString()} samples · ${parsed.links.length} links`;
    } catch (error) {
      datasetMessage = error instanceof Error ? error.message : 'Dataset import failed';
    } finally {
      importBusy = false;
      if (fileInput) fileInput.value = '';
    }
  }

  function rebuildLiveMap(): void {
    if (liveFrozen || mode !== 'live') return;
    lastLiveUpdateAt = performance.now();
    const currentGeometry = liveGeometry;
    if (!currentGeometry) {
      message = 'Freeze the board on the Board Positions tab to supply antenna geometry';
      return;
    }
    const config = { ...DEFAULT_MAP_CONFIG, frames, spacingM: spacing };
    const profiles: LinkProfile[] = [];
    const stats: LinkStats[] = [];
    for (let from = 0; from < currentGeometry.positions.length; from++) {
      for (let to = 0; to < currentGeometry.positions.length; to++) {
        if (from === to) continue;
        const id = `${from}>${to}`;
        const samples = live.cirSamples(id, config.frames);
        const profile = buildLinkProfiles(samples, config);
        if (profile) {
          profiles.push(profile);
          stats.push({
            from,
            to,
            frames: samples.length,
            accepted: profile.acceptedFrames,
            medianCorrelation: profile.medianCorrelation,
          });
        }
      }
    }
    if (!profiles.length) {
      message = 'Waiting for qualified aligned CIRs on the live feed';
      return;
    }
    const grid = buildGrid(currentGeometry, spacing, DEFAULT_MAP_CONFIG.maxVoxels, mapBounds ?? undefined);
    const reconstructionConfigValue = reconstructionConfig();
    const volume = reconstruct(profiles, currentGeometry, grid, reconstructionConfigValue);
    onSnapshot({
      profiles: stats,
      linkProfiles: profiles,
      grid,
      geometry: currentGeometry,
      spacingM: spacing,
      framesConfig: frames,
      reconstruction: reconstructionConfigValue,
      takenAt: Date.now(),
      volume,
    });
    message = `Live · ${stats.length} link${stats.length === 1 ? '' : 's'} · ${grid.shape[0]}×${grid.shape[1]}×${grid.shape[2]} voxels`;
  }

  onMount(() => {
    return () => {
      cancelAnimationFrame(computeRaf);
      cancelAnimationFrame(playRaf);
      cancelAnimationFrame(recomputeRaf);
      if (liveUpdateTimer) clearTimeout(liveUpdateTimer);
      cancelAnimationFrame(liveUpdateRaf);
    };
  });
</script>

<section class="map-layout">
  <div class="map-side">
  <div class="map-head">
    <div>
      <p>RADAR MAP / {mode === 'live' ? (liveFrozen ? 'LIVE WINDOW FROZEN' : 'LIVE ALIGNED-CIR FEED') : 'DATASET SCRUB'}</p>
      <h2>
        {#if mode === 'live'}
          {snapshot ? (liveFrozen ? `Frozen ${new Date(snapshot.takenAt).toLocaleTimeString()}` : `Live ${new Date(snapshot.takenAt).toLocaleTimeString()}`) : 'Waiting for live CIRs'}
        {:else}
          {dataset ? `${dataset.name || 'dataset'} · ${timeDisplay}` : 'Import a dataset zip to scrub'}
        {/if}
      </h2>
    </div>
    <div class="map-actions">
      <div class="segmented" aria-label="Radar map source">
        <button class:active={mode === 'live'} onclick={() => mode = 'live'}>LIVE</button>
        <button class:active={mode === 'dataset'} onclick={() => mode = 'dataset'}>DATASET</button>
      </div>
      {#if mode === 'live'}
        {#if !liveGeometry}
          <button class="primary" onclick={() => onNavigate('Board Positions')}>FREEZE BOARD ON BOARD POSITIONS TAB</button>
        {:else}
          <button class="primary" class:frozen={liveFrozen} onclick={toggleLiveFreeze}>{liveFrozen ? 'UNFREEZE MAP' : 'FREEZE MAP'}</button>
        {/if}
      {:else}
        <button class="primary" onclick={() => fileInput?.click()} disabled={importBusy}>{importBusy ? 'IMPORTING…' : dataset ? 'REPLACE DATASET' : 'IMPORT DATASET ZIP'}</button>
        <input type="file" accept=".zip,application/zip" bind:this={fileInput} hidden onchange={onFileSelected} />
      {/if}
    </div>
  </div>

  {#if mode === 'live'}
    {#if !geometry}
      <div class="prompt panel">
        <strong>NO ANTENNA GEOMETRY</strong>
        <p>Radar mapping needs node antenna phase-centre coordinates. Freeze the board on the Board Positions tab to derive them from live ranging; the map will show the range-derived-geometry warning.</p>
      </div>
    {:else if !cloud || !cloud.points.length}
      <div class="prompt panel">
        <strong>WAITING FOR ALIGNED CIRS</strong>
        <p>The Radar Map tab is rebuilding from the live aligned-CIR stream. Keep this tab open while per-link histories fill, then use FREEZE MAP to hold a reconstructed window.</p>
      </div>
    {/if}
  {:else}
    {#if !dataset}
      <div class="prompt panel">
        <strong>IMPORT A DATASET</strong>
        <p>Import a capture clip zip (the format written by the gateway's clip capture, e.g. the examples in <code>datasets/</code>). Its own recorded board geometry is used; the scrub bar selects the time instant the map is built for.</p>
      </div>
    {:else if !geometry}
      <div class="prompt panel">
        <strong>DATASET HAS NO GEOMETRY</strong>
        <p>The imported zip does not contain a <code>board_positions</code> geometry block, so no map can be built.</p>
      </div>
    {:else if !cloud || !cloud.points.length}
      <div class="prompt panel">
        <strong>NO FRAMES AT THIS INSTANT</strong>
        <p>No qualified aligned CIR frames fall in the nearest-{frames}-frame window at the scrubbed time. Move the scrub bar or adjust the frame gate.</p>
      </div>
    {/if}
  {/if}

  <div class="controls panel">
    <label class="mode"><span>RECONSTRUCTION</span><select value={reconstruction.mode} onchange={(e) => reconstruction.mode = e.currentTarget.value as ReconstructionConfig['mode']} aria-label="Reconstruction mode"><option value="standard">Standard BP</option><option value="mgbp">MGBP</option><option value="cgbp">CGBP</option></select></label>
    {#if mode === 'dataset' && dataset}
      <label class="scrub"><span>TIME <output>{timeDisplay}</output></span><input type="range" min="0" max="100" step="0.1" bind:value={timePct} aria-label="Dataset scrub position" /></label>
      <button class="play" onclick={togglePlay}>{playing ? 'PAUSE' : 'PLAY'}</button>
      <label><span>SPEED</span><select value={playSpeed} onchange={(e) => playSpeed = Number(e.currentTarget.value)} aria-label="Playback speed"><option value="0.5">0.5×</option><option value="1">1×</option><option value="2">2×</option><option value="4">4×</option></select></label>
    {/if}
    <label><span>FRAMES PER LINK <output>{frames}</output></span><input type="range" min="4" max="64" step="1" bind:value={frames} aria-label="CIR frames per link for the snapshot median" /></label>
    <label><span>GRID SPACING</span><select value={spacing} onchange={(e) => spacing = Number(e.currentTarget.value)} aria-label="Voxel grid spacing"><option value="0.05">0.05 m</option><option value="0.1">0.1 m</option><option value="0.2">0.2 m</option></select></label>
    <label><span>EVIDENCE PERCENTILE <output>{percentile.toFixed(1)}%</output></span><input type="range" min="50" max="99.5" step="0.5" bind:value={percentile} aria-label="Evidence percentile threshold" /></label>
    <label><span>POINT SCALE <output>{pointSize.toFixed(1)}</output></span><input type="range" min="1" max="8" step="0.5" bind:value={pointSize} aria-label="Point size" /></label>
    {#if reconstruction.mode === 'mgbp'}
      <div class="mode-settings">
        <header>MGBP · median/MAD gate</header>
        <label><span>MAD K</span><input type="number" min="0.5" max="10" step="0.5" bind:value={reconstruction.mgbpMadMultiplier} aria-label="MGBP MAD multiplier" /></label>
        <label><span>MIN VALID</span><input type="number" min="1" max="20" step="1" bind:value={reconstruction.mgbpMinValid} aria-label="MGBP minimum valid links" /></label>
        <label><span>MIN KEPT</span><input type="number" min="1" max="20" step="1" bind:value={reconstruction.mgbpMinRetained} aria-label="MGBP minimum retained links" /></label>
      </div>
    {:else if reconstruction.mode === 'cgbp'}
      <div class="mode-settings">
        <header>CGBP · consensus gate</header>
        <label><span>VOTE BASIS</span><select value={reconstruction.cgbpVoteBasis} onchange={(e) => reconstruction.cgbpVoteBasis = e.currentTarget.value as ReconstructionConfig['cgbpVoteBasis']} aria-label="CGBP vote basis"><option value="directed">Directed links</option><option value="baseline">Baselines</option></select></label>
        <label><span>NOISE START</span><input type="number" min="0" step="0.5" bind:value={reconstruction.cgbpNoiseStartTap} aria-label="CGBP noise reference start tap" /></label>
        <label><span>NOISE END</span><input type="number" min="0" step="0.5" bind:value={reconstruction.cgbpNoiseEndTap} aria-label="CGBP noise reference end tap" /></label>
        <label><span>NOISE λ</span><input type="number" min="0" max="10" step="0.5" bind:value={reconstruction.cgbpNoiseMargin} aria-label="CGBP noise margin" /></label>
        <label><span>CONSENSUS</span><input type="number" min="0" max="100" step="5" value={reconstruction.cgbpConsensusFraction * 100} onchange={(e) => reconstruction.cgbpConsensusFraction = Number(e.currentTarget.value) / 100} aria-label="CGBP consensus percentage" /></label>
        <label><span>MIN VALID</span><input type="number" min="1" max="20" step="1" bind:value={reconstruction.cgbpMinValid} aria-label="CGBP minimum valid votes" /></label>
        <label><span>MIN ACTIVE</span><input type="number" min="1" max="20" step="1" bind:value={reconstruction.cgbpMinActive} aria-label="CGBP minimum active votes" /></label>
      </div>
    {/if}
  </div>

  {#if geometry && mapBounds}
    <div class="bounds panel">
      <header><span>MAP BOUNDS</span><b>metres · fixed</b></header>
      {#each ['X', 'Y', 'Z'] as axisLabel, axis}
        <div class="bound-row">
          <span>{axisLabel}</span>
          <input type="number" step="0.05" value={mapBounds.min[axis].toFixed(2)} onchange={(e) => setBound(axis as 0 | 1 | 2, 'min', +e.currentTarget.value)} aria-label={`${axisLabel} minimum bound`} />
          <i>–</i>
          <input type="number" step="0.05" value={mapBounds.max[axis].toFixed(2)} onchange={(e) => setBound(axis as 0 | 1 | 2, 'max', +e.currentTarget.value)} aria-label={`${axisLabel} maximum bound`} />
        </div>
      {/each}
      <div class="bound-reset">
        <label><span>PADDING</span><input type="number" step="0.1" min="0" value={boundsPadding === null ? '' : boundsPadding} onchange={(e) => { boundsPadding = e.currentTarget.value === '' ? null : +e.currentTarget.value; resetBounds(); }} aria-label="Boundary padding in metres" /></label>
        <button onclick={resetBounds}>RESET TO NODES</button>
      </div>
    </div>
  {/if}

  <div class="diagnostics panel">
    <div><span>GRID</span><strong>{activeSnapshot ? `${activeSnapshot.grid.shape[0]}×${activeSnapshot.grid.shape[1]}×${activeSnapshot.grid.shape[2]}` : '—'}</strong></div>
    <div><span>VOXELS</span><strong>{voxelCount ? voxelCount.toLocaleString() : '—'}</strong></div>
    <div><span>SPACING</span><strong>{activeSnapshot ? `${activeSnapshot.spacingM.toFixed(2)} m` : '—'}</strong></div>
    <div><span>LINKS USED</span><strong>{activeSnapshot ? activeSnapshot.profiles.length : '—'}</strong></div>
    <div><span>PEAK</span><strong>{cloud?.valueRange[1] ? cloud.valueRange[1].toExponential(2) : '—'}</strong></div>
    <div><span>MODE</span><strong>{activeSnapshot?.reconstruction.mode?.toUpperCase() ?? '—'}</strong></div>
    <div><span>PEAK SUPPORT</span><strong>{peakSupport}</strong></div>
    <div><span>GEOMETRY</span><strong class:unsafe={activeSnapshot !== null}>{activeSnapshot ? 'RANGE-DERIVED' : '—'}</strong></div>
  </div>

  <p class="status-line">{mode === 'live' ? message : datasetMessage}</p>
  </div>

  <div class="map-scene">
    <MapScene points={cloud?.points ?? []} {nodes} valueRange={cloud?.valueRange ?? [0, 1]} {pointSize} fixedBounds={sceneBounds} />
    {#if activeSnapshot?.profiles?.length}
      <button class:open={linkStatsOpen} class="link-drawer-toggle" onclick={() => linkStatsOpen = !linkStatsOpen} aria-expanded={linkStatsOpen}>
        {linkStatsOpen ? 'CLOSE' : 'LINK DETAILS'} · {activeSnapshot.profiles.length}
      </button>
      <aside class:open={linkStatsOpen} class="link-drawer">
        <header><span>PER-LINK ACCEPTANCE</span><button onclick={() => linkStatsOpen = false} aria-label="Close link details">×</button></header>
        <p>accepted / sampled · median correlation</p>
        <div class="link-drawer-list">
          {#each activeSnapshot.profiles as stat}
            <div class="link-stat"><span>N{stat.from}→N{stat.to}</span><i style={`width:${stat.accepted / Math.max(1, stat.frames) * 100}%`}></i><b>{stat.accepted}/{stat.frames}</b><small>r {stat.medianCorrelation.toFixed(2)}</small></div>
          {/each}
        </div>
      </aside>
    {/if}
  </div>
</section>

<style>
  .map-layout { display: grid; grid-template-columns: minmax(250px, 280px) minmax(0, 1fr); grid-template-rows: minmax(0, 1fr); gap: 10px; height: 100%; min-height: 0; }
  .map-side { min-width: 0; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; padding-right: 2px; scrollbar-width: thin; }
  .map-side > * { flex: 0 0 auto; }
  .map-scene { min-width: 0; min-height: 0; position: relative; border: 1px solid var(--line); background: linear-gradient(145deg, #10191d, #0b1114); overflow: hidden; }
  .map-scene :global(.scene) { width: 100%; height: 100%; }
  .map-head { display: flex; flex-direction: column; gap: 10px; }
  .map-head p { margin: 0 0 4px; color: #61757b; font: 9px DM Mono, monospace; letter-spacing: .14em; }
  .map-head h2 { margin: 0; font: 16px DM Mono, monospace; color: #dbe5e7; font-weight: 700; }
  .map-actions { display: grid; grid-template-columns: 1fr; gap: 7px; }
  .map-actions .segmented { display: flex; border: 1px solid #385056; }
  .map-actions .segmented button { border: 0; background: #0b1215; color: #9fb0b4; padding: 9px 12px; font: 9px DM Mono, monospace; letter-spacing: .1em; cursor: pointer; }
  .map-actions .segmented button.active { background: #0e2a27; color: #45e0c1; }
  .map-actions button.primary { font: 8px DM Mono, monospace; letter-spacing: .08em; padding: 9px 10px; border: 1px solid #2c6a5c; background: #0e2a27; color: #45e0c1; cursor: pointer; }
  .map-actions button.primary:disabled { opacity: .5; cursor: wait; }
  .map-actions button.primary.frozen { border-color: #6b5732; background: #241d0d; color: #f4bd62; }
  .prompt { padding: 28px 24px; text-align: center; }
  .prompt strong { display: block; color: #f4bd62; font: 11px DM Mono, monospace; letter-spacing: .14em; margin-bottom: 8px; }
  .prompt p { margin: 0; color: #9fb0b4; font: 12px DM Mono, monospace; line-height: 1.6; max-width: 560px; }
  .prompt code { color: #45e0c1; }
  .controls { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px 10px; align-items: end; padding: 10px; }
  .controls label { display: flex; flex-direction: column; gap: 5px; color: #61757b; font: 9px DM Mono, monospace; letter-spacing: .08em; }
  .controls label span { display: flex; gap: 6px; align-items: center; justify-content: space-between; }
  .controls label output { color: #45e0c1; }
  .controls label.scrub { grid-column: 1 / -1; min-width: 0; }
  .controls label.mode { grid-column: 1 / -1; }
  .controls input[type="range"] { width: 100%; }
  .controls select { box-sizing: border-box; height: 28px; background: #0b1215; color: #dbe5e7; border: 1px solid #385056; font: 9px DM Mono, monospace; padding: 4px; width: 100%; }
  .controls button.play { width: 100%; }
  .mode-settings { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 10px; padding: 8px; border: 1px solid #304147; background: #0b1215; }
  .mode-settings header { grid-column: 1 / -1; color: #f4bd62; font: 8px DM Mono, monospace; letter-spacing: .1em; }
  .mode-settings label { min-width: 0; }
  .mode-settings input { width: 100%; box-sizing: border-box; padding: 4px; color: #dbe5e7; background: #071013; border: 1px solid #304147; font: 9px DM Mono, monospace; }
  .controls button.play { border: 1px solid #2c6a5c; background: #0e2a27; color: #45e0c1; padding: 8px 12px; font: 9px DM Mono, monospace; letter-spacing: .1em; cursor: pointer; }
  .bounds { padding: 0 0 8px; }
  .bounds header { height: 32px; padding: 0 11px; border-bottom: 1px solid var(--line); display: flex; align-items: center; justify-content: space-between; font: 9px DM Mono, monospace; letter-spacing: .1em; }
  .bounds header span { color: #91a1a6; }
  .bounds header b { color: #61757b; font-weight: 400; }
  .bound-row { display: grid; grid-template-columns: 12px minmax(0, 1fr) 7px minmax(0, 1fr); align-items: center; gap: 4px; padding: 5px 8px 0; color: #61757b; font: 9px DM Mono, monospace; }
  .bound-row span { width: 14px; color: #91a1a6; }
  .bound-row i { font-style: normal; color: #405159; }
  .bound-row input { width: 100%; min-width: 0; box-sizing: border-box; padding: 4px; font: 9px DM Mono, monospace; color: #dbe5e7; background: #0b1215; border: 1px solid #304147; }
  .bound-reset { display: flex; align-items: flex-end; gap: 8px; padding: 8px 8px 0; }
  .bound-reset label { display: flex; flex-direction: column; gap: 4px; flex: 1; color: #61757b; font: 8px DM Mono, monospace; letter-spacing: .1em; }
  .bound-reset input { width: 100%; padding: 4px 6px; font: 9px DM Mono, monospace; color: #dbe5e7; background: #0b1215; border: 1px solid #304147; }
  .bound-reset button { border: 1px solid #6b5732; background: #241d0d; color: #f4bd62; padding: 6px 10px; font: 8px DM Mono, monospace; letter-spacing: .08em; cursor: pointer; white-space: nowrap; }
  .diagnostics { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 10px; padding: 9px 10px; }
  .diagnostics div { display: flex; flex-direction: column; gap: 3px; }
  .diagnostics span { color: #61757b; font: 8px DM Mono, monospace; letter-spacing: .12em; }
  .diagnostics strong { color: #dbe5e7; font: 11px DM Mono, monospace; }
  .diagnostics strong.unsafe { color: #f4bd62; }
  .link-drawer-toggle { position: absolute; z-index: 3; top: 10px; right: 10px; border: 1px solid #385056; background: #0b1215e8; color: #9fb0b4; padding: 7px 9px; font: 8px DM Mono, monospace; letter-spacing: .08em; transition: right .2s ease; }
  .link-drawer-toggle.open { right: 290px; }
  .link-drawer { position: absolute; z-index: 2; top: 0; right: 0; bottom: 0; width: 270px; display: grid; grid-template-rows: 38px auto minmax(0, 1fr); border-left: 1px solid #385056; background: #0b1215f5; transform: translateX(101%); transition: transform .2s ease; }
  .link-drawer.open { transform: none; }
  .link-drawer header { display: flex; align-items: center; justify-content: space-between; padding: 0 10px; border-bottom: 1px solid var(--line); }
  .link-drawer header span { color: #9fb0b4; font: 9px DM Mono, monospace; letter-spacing: .1em; }
  .link-drawer header button { border: 0; background: transparent; color: #9fb0b4; font-size: 20px; line-height: 1; }
  .link-drawer > p { margin: 0; padding: 8px 10px; color: #61757b; border-bottom: 1px solid var(--line); font: 8px DM Mono, monospace; }
  .link-drawer-list { overflow-y: auto; padding: 10px; }
  .link-stat { display: flex; align-items: center; gap: 10px; font: 9px DM Mono, monospace; color: #9fb0b4; }
  .link-stat span { width: 74px; }
  .link-stat i { display: inline-block; height: 5px; background: #2c6a5c; }
  .link-stat b { color: #dbe5e7; width: 58px; }
  .link-stat small { color: #61757b; }
  .status-line { margin: 0; color: #9fb0b4; font: 9px DM Mono, monospace; letter-spacing: .08em; }
  @media (max-width: 900px) {
    .map-layout { display: flex; flex-direction: column; height: auto; }
    .map-scene { order: -1; height: 55vh; flex: none; }
    .map-side { overflow-y: visible; }
    .link-drawer { width: min(270px, 88%); }
    .link-drawer-toggle.open { right: min(290px, calc(88% + 10px)); }
  }
</style>
