<script lang="ts">
  import MapScene from './MapScene.svelte';
  import { LiveStore } from './live';
  import type { Tab } from './types';
  import {
    DEFAULT_MAP_CONFIG,
    backproject,
    buildGrid,
    buildLinkProfiles,
    geometryFromBoardFreeze,
    volumeToPoints,
    type LinkProfile,
    type LinkStats,
    type MapGeometry,
    type MapSnapshot,
  } from './map/map-engine';

  let {
    live,
    snapshot,
    onSnapshot,
    onNavigate,
  }: {
    live: LiveStore;
    snapshot: MapSnapshot | null;
    onSnapshot: (snapshot: MapSnapshot) => void;
    onNavigate: (tab: Tab) => void;
  } = $props();

  let frames = $state(32);
  let spacing = $state(0.1);
  let percentile = $state(85);
  let pointSize = $state(3);
  let busy = $state(false);
  let message = $state('Waiting for aligned CIR samples on this tab…');

  const geometry = $derived(geometryFromBoardFreeze(live.boardFreeze));
  const cloud = $derived(snapshot ? volumeToPoints(snapshot.volume, percentile, 50_000) : null);
  const nodes = $derived(geometry?.positions ?? []);
  const voxelCount = $derived(
    snapshot ? snapshot.grid.shape[0] * snapshot.grid.shape[1] * snapshot.grid.shape[2] : 0
  );

  async function takeSnapshot() {
    if (busy) return;
    const currentGeometry = geometry;
    if (!currentGeometry) {
      message = 'Freeze the board on the Board Positions tab to supply antenna geometry';
      return;
    }
    busy = true;
    message = 'Collecting aligned CIRs and backprojecting…';
    await new Promise((resolve) => setTimeout(resolve, 0));
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
    busy = false;
    if (!profiles.length) {
      message = 'No qualified aligned CIRs captured yet — stay on this tab while samples stream in';
      return;
    }
    const grid = buildGrid(currentGeometry, spacing, DEFAULT_MAP_CONFIG.maxVoxels);
    const volume = backproject(profiles, currentGeometry, grid);
    onSnapshot({
      profiles: stats,
      grid,
      geometry: currentGeometry,
      spacingM: spacing,
      framesConfig: frames,
      takenAt: Date.now(),
      volume,
    });
    message = `Snapshot complete · ${stats.length} link${stats.length === 1 ? '' : 's'} used · ${grid.shape[0]}×${grid.shape[1]}×${grid.shape[2]} voxels`;
  }
</script>

<section class="map-layout">
  <div class="map-head">
    <div>
      <p>RADAR MAP / STATIC ENVIRONMENT SNAPSHOT</p>
      <h2>{snapshot ? `Taken ${new Date(snapshot.takenAt).toLocaleTimeString()}` : 'No snapshot yet'}</h2>
    </div>
    <div class="map-actions">
      {#if !geometry}
        <button class="primary" onclick={() => onNavigate('Board Positions')}>FREEZE BOARD ON BOARD POSITIONS TAB</button>
      {:else}
        <button class="primary" onclick={takeSnapshot} disabled={busy}>{busy ? 'BACKPROJECTING…' : 'TAKE SNAPSHOT'}</button>
      {/if}
    </div>
  </div>

  {#if !geometry}
    <div class="prompt panel">
      <strong>NO ANTENNA GEOMETRY</strong>
      <p>Radar mapping needs node antenna phase-centre coordinates. Freeze the board on the Board Positions tab to derive them from live ranging; the map will show the range-derived-geometry warning.</p>
    </div>
  {:else if !cloud || !cloud.points.length}
    <div class="prompt panel">
      <strong>WAITING FOR ALIGNED CIRS</strong>
      <p>The Radar Map tab subscribes to the live aligned-CIR stream. Keep this tab open so per-link samples accumulate, then take a snapshot.</p>
    </div>
  {:else}
    <div class="controls panel">
      <label><span>FRAMES PER LINK <output>{frames}</output></span><input type="range" min="4" max="64" step="1" bind:value={frames} aria-label="CIR frames per link for the snapshot median" /></label>
      <label><span>GRID SPACING</span><select value={spacing} onchange={(e) => spacing = Number(e.currentTarget.value)} aria-label="Voxel grid spacing"><option value="0.05">0.05 m</option><option value="0.1">0.1 m</option><option value="0.2">0.2 m</option></select></label>
      <label><span>EVIDENCE PERCENTILE <output>{percentile.toFixed(1)}%</output></span><input type="range" min="50" max="99.5" step="0.5" bind:value={percentile} aria-label="Evidence percentile threshold" /></label>
      <label><span>POINT SCALE <output>{pointSize.toFixed(1)}</output></span><input type="range" min="1" max="8" step="0.5" bind:value={pointSize} aria-label="Point size" /></label>
      <button class="snapshot" onclick={takeSnapshot} disabled={busy}>RE-SNAPSHOT</button>
    </div>
  {/if}

  <div class="scene-panel panel">
    <MapScene points={cloud?.points ?? []} {nodes} valueRange={cloud?.valueRange ?? [0, 1]} {pointSize} />
  </div>

  <div class="diagnostics panel">
    <div><span>GRID</span><strong>{snapshot ? `${snapshot.grid.shape[0]}×${snapshot.grid.shape[1]}×${snapshot.grid.shape[2]}` : '—'}</strong></div>
    <div><span>VOXELS</span><strong>{voxelCount ? voxelCount.toLocaleString() : '—'}</strong></div>
    <div><span>SPACING</span><strong>{snapshot ? `${snapshot.spacingM.toFixed(2)} m` : '—'}</strong></div>
    <div><span>LINKS USED</span><strong>{snapshot ? snapshot.profiles.length : '—'}</strong></div>
    <div><span>PEAK</span><strong>{cloud?.valueRange[1] ? cloud.valueRange[1].toExponential(2) : '—'}</strong></div>
    <div><span>GEOMETRY</span><strong class:unsafe={snapshot !== null}>{snapshot ? 'RANGE-DERIVED' : '—'}</strong></div>
  </div>

  {#if snapshot?.profiles?.length}
    <div class="link-stats panel">
      <header><span>PER-LINK ACCEPTANCE</span><b>accepted / sampled · median correlation</b></header>
      {#each snapshot.profiles as stat}
        <div class="link-stat"><span>N{stat.from}→N{stat.to}</span><i style={`width:${stat.accepted / Math.max(1, stat.frames) * 100}%`}></i><b>{stat.accepted}/{stat.frames}</b><small>r {stat.medianCorrelation.toFixed(2)}</small></div>
      {/each}
    </div>
  {/if}

  <p class="status-line">{message}</p>
</section>

<style>
  .map-layout { display: flex; flex-direction: column; gap: 10px; min-height: 0; }
  .map-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; }
  .map-head p { margin: 0 0 4px; color: #61757b; font: 9px DM Mono, monospace; letter-spacing: .14em; }
  .map-head h2 { margin: 0; font: 16px DM Mono, monospace; color: #dbe5e7; font-weight: 700; }
  .map-actions button { font: 9px DM Mono, monospace; letter-spacing: .1em; padding: 10px 14px; border: 1px solid #2c6a5c; background: #0e2a27; color: #45e0c1; cursor: pointer; }
  .map-actions button:disabled { opacity: .5; cursor: wait; }
  .prompt { padding: 28px 24px; text-align: center; }
  .prompt strong { display: block; color: #f4bd62; font: 11px DM Mono, monospace; letter-spacing: .14em; margin-bottom: 8px; }
  .prompt p { margin: 0; color: #9fb0b4; font: 12px DM Mono, monospace; line-height: 1.6; max-width: 560px; }
  .controls { display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-end; padding: 12px 14px; }
  .controls label { display: flex; flex-direction: column; gap: 5px; color: #61757b; font: 9px DM Mono, monospace; letter-spacing: .08em; }
  .controls label span { display: flex; gap: 6px; align-items: center; }
  .controls label output { color: #45e0c1; }
  .controls input[type="range"] { width: 150px; }
  .controls select { background: #0b1215; color: #dbe5e7; border: 1px solid #385056; font: 9px DM Mono, monospace; padding: 4px; }
  .controls button.snapshot { margin-left: auto; border: 1px solid #6b5732; background: #241d0d; color: #f4bd62; padding: 8px 12px; font: 9px DM Mono, monospace; letter-spacing: .1em; cursor: pointer; }
  .scene-panel { height: 420px; min-height: 320px; padding: 0; overflow: hidden; }
  .diagnostics { display: flex; flex-wrap: wrap; gap: 10px 24px; padding: 10px 14px; }
  .diagnostics div { display: flex; flex-direction: column; gap: 3px; }
  .diagnostics span { color: #61757b; font: 8px DM Mono, monospace; letter-spacing: .12em; }
  .diagnostics strong { color: #dbe5e7; font: 11px DM Mono, monospace; }
  .diagnostics strong.unsafe { color: #f4bd62; }
  .link-stats { padding: 12px 14px; }
  .link-stats header { display: flex; justify-content: space-between; margin-bottom: 10px; }
  .link-stats header span { color: #61757b; font: 9px DM Mono, monospace; letter-spacing: .12em; }
  .link-stats header b { color: #9fb0b4; font: 8px DM Mono, monospace; }
  .link-stat { display: flex; align-items: center; gap: 10px; font: 9px DM Mono, monospace; color: #9fb0b4; }
  .link-stat span { width: 74px; }
  .link-stat i { display: inline-block; height: 5px; background: #2c6a5c; }
  .link-stat b { color: #dbe5e7; width: 58px; }
  .link-stat small { color: #61757b; }
  .status-line { margin: 0; color: #9fb0b4; font: 9px DM Mono, monospace; letter-spacing: .08em; }
</style>
