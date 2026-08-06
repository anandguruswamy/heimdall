<script lang="ts">
  import { onMount } from 'svelte';
  import type { HeimdallApi } from './api';
  import { seatClasses, type SeatClass, type TrainingArchitecture, type TrainingLinkMode, type TrainingMode, type TrainingVariant } from './types';

  let { api, boardPositions }: { api: HeimdallApi; boardPositions: () => unknown } = $props();

  // Display names differ from the class identifiers sent to the backend:
  // BackLeft/BackRight are shown as REAR LEFT/REAR RIGHT.
  const seatDisplay: Record<SeatClass, string> = {
    FrontLeft: 'FRONT LEFT',
    FrontRight: 'FRONT RIGHT',
    BackRight: 'REAR RIGHT',
    BackLeft: 'REAR LEFT',
    Empty: 'EMPTY',
  };

  type ClipRow = {
    id: number;
    name: string;
    status: string;
    error: string;
    durationS: number;
    seat: SeatClass | null;
    person: string;
    exclude: boolean;
    hasCir: boolean;
  };

  let clips = $state<ClipRow[]>([]);
  let clipsMessage = $state('');

  let captureName = $state('');
  let captureNote = $state('');
  let captureDurationS = $state(10);
  let captureSeat = $state<SeatClass | ''>('');
  let capturePerson = $state('');
  let captureState = $state<'idle' | 'capturing'>('idle');
  let captureProgress = $state(0);
  let captureMessage = $state('');
  let captureError = $state(false);

  let variant = $state<TrainingVariant>('raw');
  let mode = $state<TrainingMode>('seat');
  let architecture = $state<TrainingArchitecture>('standard');
  let linkMode = $state<TrainingLinkMode>('canonical');
  let tapsLeft = $state(8);
  let tapsRight = $state(24);
  let patience = $state(5);
  let epochs = $state(30);
  let training = $state<Record<string, unknown> | null>(null);
  let trainMessage = $state('');
  let logLines = $state<string[]>([]);
  let logPanel = $state<HTMLDivElement | null>(null);
  let logNext = 0;
  let runId = 0;
  let pollTimer: ReturnType<typeof setTimeout> | undefined;
  let destroyed = false;

  const trainable = $derived(clips.filter((clip) => clip.status === 'complete' && clip.seat !== null && !clip.exclude && clip.hasCir));
  const missingClasses = $derived(seatClasses.filter((seat) => !trainable.some((clip) => clip.seat === seat)));
  const needsSeatClasses = $derived(mode !== 'person');
  const requiredMissingClasses = $derived(needsSeatClasses ? missingClasses : missingClasses.filter((seat) => seat === 'Empty'));
  const needsPerson = $derived(mode !== 'seat');
  const unlabeledPeople = $derived(trainable.filter((clip) => clip.seat !== 'Empty' && clip.person.trim().length === 0));
  const hasPerson = $derived(trainable.some((clip) => clip.seat !== 'Empty' && clip.person.trim().length > 0));
  const trainingStatus = $derived(String(training?.status ?? 'idle'));
  const trainingRunning = $derived(trainingStatus === 'running');
  const trainingResult = $derived((training?.result ?? null) as Record<string, unknown> | null);
  const seatConfusion = $derived(matrixFrom(trainingResult?.seat_confusion ?? trainingResult?.seat_confusion_matrix ?? trainingResult?.confusion, seatClasses.length));
  const personClasses = $derived(stringsFrom(trainingResult?.person_names ?? trainingResult?.person_classes ?? trainingResult?.person_labels));
  const personConfusion = $derived(matrixFrom(trainingResult?.person_confusion ?? trainingResult?.person_confusion_matrix, personClasses.length));
  const seatAccuracy = $derived(numberFrom(trainingResult?.seat_accuracy, trainingResult?.seat_test_accuracy, trainingResult?.test_seat_accuracy, trainingResult?.test_accuracy));
  const personAccuracy = $derived(numberFrom(trainingResult?.person_accuracy, trainingResult?.person_test_accuracy, trainingResult?.test_person_accuracy));
  const canTrain = $derived(requiredMissingClasses.length === 0 && unlabeledPeople.length === 0 && (!needsPerson || hasPerson) && !trainingRunning && captureState !== 'capturing');

  function matrixFrom(value: unknown, size: number): number[][] | null {
    return size > 0 && Array.isArray(value) && value.length === size && value.every((row) => Array.isArray(row) && row.length === size)
      ? value as number[][]
      : null;
  }

  function numberFrom(...values: unknown[]): number | null {
    const value = values.find((candidate) => typeof candidate === 'number');
    return typeof value === 'number' ? value : null;
  }

  function stringsFrom(value: unknown): string[] {
    return Array.isArray(value) ? value.map(String) : [];
  }

  function parseClips(value: unknown): ClipRow[] {
    if (!Array.isArray(value)) return [];
    return value.map((row) => {
      const record = row as Record<string, unknown>;
      const v = (record.value ?? {}) as Record<string, unknown>;
      const tag = (v.training ?? null) as Record<string, unknown> | null;
      const manifest = (v.manifest ?? {}) as Record<string, unknown>;
      const seat = seatClasses.find((name) => name === tag?.seat) ?? null;
      return {
        id: Number(record.id),
        name: String(v.name ?? ''),
        status: String(v.status ?? 'unknown'),
        error: String(v.error ?? ''),
        durationS: Number(v.duration_s ?? manifest.duration_s ?? 0),
        seat,
        person: seat === 'Empty' ? '' : String(tag?.person ?? ''),
        exclude: tag?.exclude === true,
        hasCir: Number(manifest.aligned_cir_records ?? 0) > 0,
      };
    });
  }

  async function refreshClips() {
    try {
      clips = parseClips(await api.getClips());
      clipsMessage = '';
    } catch (error) {
      clipsMessage = error instanceof Error ? error.message : 'Clip list unavailable';
    }
  }

  async function capture() {
    if (captureState === 'capturing') return;
    captureState = 'capturing';
    captureProgress = 0;
    captureError = false;
    captureMessage = `Capturing ${captureDurationS} s from trigger…`;
    try {
      const created = await api.saveClip({
        name: captureName.trim(),
        note: captureNote.trim(),
        duration_s: captureDurationS,
        board_positions: boardPositions(),
      }) as Record<string, unknown>;
      const clipId = Number(created.id);
      const started = Date.now();
      const pollMs = Math.max(15_000, captureDurationS * 1000 + 5_000);
      while (Date.now() - started < pollMs) {
        await new Promise((resolve) => setTimeout(resolve, 1_000));
        captureProgress = Math.min(99, ((Date.now() - started) / pollMs) * 100);
        await refreshClips();
        const row = clips.find((clip) => clip.id === clipId);
        if (row?.status === 'complete') {
          captureProgress = 100;
          const person = captureSeat === 'Empty' ? '' : capturePerson.trim();
          if (captureSeat) {
            await tag(clipId, captureSeat, person, false);
            captureMessage = `Clip ${clipId} captured and tagged ${seatDisplay[captureSeat]}${person ? ` · ${person}` : ''}`;
          } else {
            captureMessage = `Clip ${clipId} captured · assign a seat tag to include it in training`;
          }
          captureState = 'idle';
          return;
        }
        if (row?.status === 'failed') throw new Error(row.error || 'Capture failed on the backend');
      }
      throw new Error('Clip finalization timed out');
    } catch (error) {
      captureState = 'idle';
      captureProgress = 0;
      captureError = true;
      captureMessage = error instanceof Error ? error.message : 'Capture failed';
    }
  }

  async function tag(id: number, seat: SeatClass | null, person: string, exclude: boolean) {
    try {
      await api.setClipTraining(id, { seat, person: seat === 'Empty' ? '' : person, exclude });
      await refreshClips();
    } catch (error) {
      clipsMessage = error instanceof Error ? error.message : `Clip ${id} could not be tagged`;
    }
  }

  async function removeClip(id: number) {
    if (!confirm(`Delete protected clip ${id} permanently?`)) return;
    try {
      await api.deleteClip(id);
      await refreshClips();
    } catch (error) {
      clipsMessage = error instanceof Error ? error.message : `Clip ${id} could not be deleted`;
    }
  }

  function seatFrom(value: string): SeatClass | null {
    return seatClasses.find((name) => name === value) ?? null;
  }

  function setCaptureSeat(value: string) {
    captureSeat = seatFrom(value) ?? '';
    if (captureSeat === 'Empty') capturePerson = '';
  }

  async function startTraining() {
    if (!canTrain) return;
    trainMessage = '';
    try {
      const started = await api.startTraining({ variant, epochs, mode, architecture, link_mode: linkMode, taps_left: tapsLeft, taps_right: tapsRight, patience }) as Record<string, unknown>;
      runId = Number(started.run_id ?? 0);
      logLines = [];
      logNext = 0;
      await pollTraining();
    } catch (error) {
      trainMessage = error instanceof Error ? error.message : 'Training could not be started';
    }
  }

  async function pollTraining() {
    clearTimeout(pollTimer);
    if (destroyed) return;
    try {
      const payload = await api.trainingStatus(logNext) as Record<string, unknown>;
      const payloadRunId = Number(payload.run_id ?? 0);
      if (payloadRunId !== runId) {
        // A different run than the one we were following (page reload,
        // second client, remount): restart log streaming from the top.
        runId = payloadRunId;
        logLines = [];
        logNext = 0;
        if (payloadRunId !== 0) {
          pollTimer = setTimeout(() => void pollTraining(), 0);
          return;
        }
      }
      training = payload;
      const lines = Array.isArray(payload.log) ? payload.log as string[] : [];
      if (lines.length) logLines = [...logLines, ...lines];
      if (typeof payload.log_next === 'number') logNext = payload.log_next;
      if (String(payload.status) === 'running') {
        pollTimer = setTimeout(() => void pollTraining(), 1_000);
      }
    } catch (error) {
      if (trainingRunning) {
        pollTimer = setTimeout(() => void pollTraining(), 2_500);
      } else {
        trainMessage = error instanceof Error ? error.message : 'Training status unavailable';
      }
    }
  }

  $effect(() => {
    void logLines.length;
    if (logPanel) logPanel.scrollTop = logPanel.scrollHeight;
  });

  onMount(() => {
    void refreshClips();
    void pollTraining();
    return () => {
      destroyed = true;
      clearTimeout(pollTimer);
    };
  });
</script>

<section class="training-layout">
  <article class="panel capture-panel">
    <header><span>CAPTURE LABELED CLIP</span><b>{captureState === 'capturing' ? `${Math.round(captureProgress)}%` : 'READY'}</b></header>
    <div class="panel-body">
      <label>NAME<input maxlength="120" bind:value={captureName} placeholder="Optional label" /></label>
      <label>NOTE<input maxlength="2000" bind:value={captureNote} placeholder="Optional context" /></label>
      <div class="pair">
        <label>LENGTH<select bind:value={captureDurationS}>{#each [5, 10, 15, 30, 60] as seconds}<option value={seconds}>{seconds} s</option>{/each}</select></label>
        <label>SEAT TAG<select value={captureSeat} onchange={(e) => setCaptureSeat(e.currentTarget.value)}><option value="">— untagged —</option>{#each seatClasses as seat}<option value={seat}>{seatDisplay[seat]}</option>{/each}</select></label>
      </div>
      <label>PERSON<input maxlength="60" bind:value={capturePerson} disabled={captureSeat === 'Empty'} placeholder={captureSeat === 'Empty' ? 'N/A for empty' : 'Who is seated'} /></label>
      <button class="snapshot" onclick={capture} disabled={captureState === 'capturing'}>{captureState === 'capturing' ? `CAPTURING ${Math.round(captureProgress)}%` : `CAPTURE ${captureDurationS} S CLIP`}</button>
      <div class="capture-progress"><i style={`width:${captureProgress}%`}></i></div>
      {#if captureMessage}<p class="note" class:error={captureError}>{captureMessage}</p>{/if}
    </div>
  </article>

  <article class="panel clips-panel">
    <header>
      <span>CAPTURED CLIPS</span>
      <span class="header-side"><b>{trainable.length} IN TRAINING SET</b><button class="text-button" onclick={() => void refreshClips()}>REFRESH</button></span>
    </header>
    <div class="table-scroll">
      <table>
        <thead><tr><th>ID</th><th>NAME</th><th>SEAT TAG</th><th>PERSON</th><th>LEN</th><th>STATUS</th><th></th></tr></thead>
        <tbody>
          {#each clips as clip (clip.id)}
            <tr class:excluded={clip.exclude}>
              <td class="mono">{String(clip.id).padStart(6, '0')}</td>
              <td class="name">{clip.name || '—'}</td>
              <td>
                <select value={clip.seat ?? ''} disabled={clip.status !== 'complete'} onchange={(e) => { const seat = seatFrom(e.currentTarget.value); void tag(clip.id, seat, seat === 'Empty' ? '' : clip.person, clip.exclude); }}>
                  <option value="">—</option>
                  {#each seatClasses as seat}<option value={seat}>{seatDisplay[seat]}</option>{/each}
                </select>
              </td>
              <td><input value={clip.person} maxlength="60" disabled={!clip.seat || clip.seat === 'Empty'} placeholder={clip.seat === 'Empty' ? 'N/A' : '—'} onchange={(e) => void tag(clip.id, clip.seat, e.currentTarget.value.trim(), clip.exclude)} /></td>
              <td class="mono">{clip.durationS ? `${clip.durationS}s` : '—'}</td>
              <td>
                {#if clip.status !== 'complete'}<span class="flag pending">{clip.status.toUpperCase()}</span>
                {:else if !clip.hasCir}<span class="flag warn" title="The clip zip has no aligned-cirs.ndjson, so it cannot be trained on">NO CIR</span>
                {:else if !clip.seat}<span class="flag warn">UNTAGGED</span>
                {:else if clip.exclude}<span class="flag muted">EXCLUDED</span>
                {:else}<span class="flag ok">IN SET</span>{/if}
              </td>
              <td class="row-actions">
                <label class="exclude-toggle" title="Keep the tag but leave this clip out of training"><input type="checkbox" checked={clip.exclude} disabled={!clip.seat} onchange={(e) => void tag(clip.id, clip.seat, clip.person, e.currentTarget.checked)} />EXCL</label>
                <button class="text-button" onclick={() => void removeClip(clip.id)} disabled={clip.status === 'capturing'}>DEL</button>
              </td>
            </tr>
          {/each}
          {#if !clips.length}<tr><td colspan="7" class="empty">NO CLIPS CAPTURED YET</td></tr>{/if}
        </tbody>
      </table>
    </div>
    {#if clipsMessage}<p class="note error foot">{clipsMessage}</p>{/if}
  </article>

  <article class="panel train-panel">
    <header><span>TRAIN CIR CLASSIFIER</span><b class:running={trainingRunning}>{trainingStatus.toUpperCase()}</b></header>
    <div class="panel-body">
      <div class="coverage">
        {#each seatClasses as seat}
          <div class:missing={!trainable.some((clip) => clip.seat === seat)}>
            <span>{seatDisplay[seat]}</span>
            <b>{trainable.filter((clip) => clip.seat === seat).length}</b>
          </div>
        {/each}
      </div>
      <div class="pair">
        <label>VARIANT<select bind:value={variant} disabled={trainingRunning}><option value="raw">raw</option><option value="calibrated">calibrated</option></select></label>
        <label>MODEL MODE<select bind:value={mode} disabled={trainingRunning}><option value="seat">seat</option><option value="person">person</option><option value="separate">separate</option><option value="joint">joint</option></select></label>
      </div>
      <div class="pair">
        <label>ARCHITECTURE<select bind:value={architecture} disabled={trainingRunning}><option value="standard">standard</option><option value="lite">lite</option></select></label>
        <label>LINK MODE<select bind:value={linkMode} disabled={trainingRunning}><option value="canonical">canonical · one per reciprocal link</option><option value="directed">directed</option></select></label>
      </div>
      <div class="pair triple">
        <label>TAPS LEFT<input type="number" min="0" bind:value={tapsLeft} disabled={trainingRunning} /></label>
        <label>TAPS RIGHT<input type="number" min="0" bind:value={tapsRight} disabled={trainingRunning} /></label>
        <label>PATIENCE<input type="number" min="0" bind:value={patience} disabled={trainingRunning} /></label>
      </div>
      <div class="pair single">
        <label>EPOCHS<input type="number" min="1" max="500" bind:value={epochs} disabled={trainingRunning} /></label>
      </div>
      <button class="snapshot" onclick={startTraining} disabled={!canTrain}>{trainingRunning ? 'TRAINING IN PROGRESS…' : 'TRAIN MODEL'}</button>
      {#if requiredMissingClasses.length > 0}
        <p class="note">NEEDS ONE TAGGED CLIP PER REQUIRED CLASS · MISSING: {requiredMissingClasses.map((seat) => seatDisplay[seat]).join(', ')}</p>
      {/if}
      {#if needsPerson && !hasPerson}<p class="note">NEEDS AT LEAST ONE NON-EMPTY PERSON TAG</p>{/if}
      {#if unlabeledPeople.length > 0}<p class="note">EVERY OCCUPIED CLIP NEEDS A PERSON TAG · MISSING: {unlabeledPeople.map((clip) => String(clip.id).padStart(6, '0')).join(', ')}</p>{/if}
      {#if trainMessage}<p class="note error">{trainMessage}</p>{/if}
      {#if trainingStatus === 'failed'}
        <p class="note error">{String(training?.error ?? 'Training failed')}</p>
      {/if}
      {#if trainingResult}
        <dl class="result">
          <dt>SEAT ACCURACY</dt>
          <dd class="accuracy">{seatAccuracy !== null ? `${(seatAccuracy * 100).toFixed(2)}%` : '—'}</dd>
          {#if personAccuracy !== null}<dt>PERSON ACCURACY</dt><dd class="accuracy">{(personAccuracy * 100).toFixed(2)}%</dd>{/if}
          <dt>SAVED MODEL</dt>
          <dd class="path">{String(trainingResult.model_path ?? '—')}</dd>
        </dl>
        {#if seatConfusion}
          <div class="confusion-wrap">
            <p class="note">CONFUSION MATRIX · ROWS TRUE / COLS PREDICTED</p>
            <table class="confusion">
              <thead><tr><th></th>{#each seatClasses as seat}<th>{seatDisplay[seat]}</th>{/each}</tr></thead>
              <tbody>
                {#each seatConfusion as row, i}
                  <tr><th>{seatDisplay[seatClasses[i]]}</th>{#each row as cell, j}<td class:diag={i === j} class:err={i !== j && cell > 0}>{cell}</td>{/each}</tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
        {#if personConfusion}
          <div class="confusion-wrap">
            <p class="note">PERSON CONFUSION MATRIX · ROWS TRUE / COLS PREDICTED</p>
            <table class="confusion">
              <thead><tr><th></th>{#each personClasses as person}<th>{person}</th>{/each}</tr></thead>
              <tbody>{#each personConfusion as row, i}<tr><th>{personClasses[i]}</th>{#each row as cell, j}<td class:diag={i === j} class:err={i !== j && cell > 0}>{cell}</td>{/each}</tr>{/each}</tbody>
            </table>
          </div>
        {/if}
      {/if}
    </div>
  </article>

  <article class="panel log-panel">
    <header><span>TRAINING LOG</span><b>{trainingRunning ? 'STREAMING' : `${logLines.length} LINES`}</b></header>
    <div class="log" bind:this={logPanel}>
      {#each logLines as line, i (i)}<div>{line}</div>{/each}
      {#if !logLines.length}<div class="placeholder">DATASET BUILD AND PER-EPOCH TRAINING OUTPUT STREAMS HERE</div>{/if}
    </div>
  </article>
</section>

<style>
  .training-layout {
    min-height: 0;
    display: grid;
    gap: 7px;
    grid-template-columns: minmax(330px, 400px) minmax(0, 1fr);
    /* Both rows take fixed shares of the viewport so a growing clips table
       can never squeeze the train/log row out; panels scroll internally. */
    grid-template-rows: minmax(0, 1fr) minmax(0, 1fr);
    grid-template-areas: 'capture clips' 'train log';
  }
  .capture-panel { grid-area: capture; }
  .clips-panel { grid-area: clips; }
  .train-panel { grid-area: train; }
  .log-panel { grid-area: log; }
  .panel { display: grid; grid-template-rows: 36px minmax(0, 1fr); }
  .clips-panel { grid-template-rows: 36px minmax(0, 1fr) auto; }

  .panel-body { min-height: 0; overflow-y: auto; padding: 4px 0 10px; }
  .panel-body > label, .pair { display: block; margin: 10px 13px 0; color: #99aaae; font: 9px DM Mono; letter-spacing: .08em; }
  .panel-body input:not([type='checkbox']), .panel-body select { display: block; width: 100%; margin-top: 5px; font: 12px DM Mono; }
  .pair { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 0; }
  .pair.triple { grid-template-columns: repeat(3, 1fr); }
  .pair.single { grid-template-columns: 1fr; }
  .pair label { display: block; margin-top: 10px; color: #99aaae; font: 9px DM Mono; letter-spacing: .08em; }
  .panel-body .snapshot { margin-top: 12px; }
  .note { margin: 8px 13px 0; color: var(--muted); font: 8px DM Mono; letter-spacing: .06em; line-height: 1.5; overflow-wrap: anywhere; }
  .note.error { color: var(--amber); }
  .note.foot { margin: 0; padding: 6px 13px 8px; border-top: 1px solid var(--line); }

  .header-side { display: flex; align-items: center; gap: 12px; }
  .text-button { border: 0; background: none; color: var(--amber); font: 8px DM Mono; letter-spacing: .08em; cursor: pointer; }
  .text-button:disabled { opacity: .4; cursor: not-allowed; }

  .table-scroll { min-height: 0; overflow: auto; }
  table { width: 100%; border-collapse: collapse; font: 9px DM Mono; }
  thead th { position: sticky; top: 0; z-index: 1; background: #101a1e; color: var(--muted); font-weight: 400; letter-spacing: .1em; text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); }
  tbody td { padding: 5px 10px; border-bottom: 1px solid #1c282d; color: #bac8cb; }
  tbody tr.excluded td { opacity: .55; }
  td.mono { color: var(--muted); }
  td.name { max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  tbody select, tbody input:not([type='checkbox']) { width: 100%; min-width: 90px; font: 9px DM Mono; }
  td.empty { padding: 16px 10px; color: var(--muted); letter-spacing: .1em; }
  .flag { display: inline-block; padding: 2px 6px; border: 1px solid #2c3c42; color: var(--muted); font-size: 8px; letter-spacing: .09em; white-space: nowrap; }
  .flag.ok { border-color: #33564f; color: var(--teal); }
  .flag.warn { border-color: #745b32; color: var(--amber); }
  .flag.pending { border-color: #2c3c42; color: #99aaae; }
  .row-actions { white-space: nowrap; }
  .exclude-toggle { display: inline-flex; align-items: center; gap: 4px; margin-right: 10px; color: var(--muted); font-size: 8px; letter-spacing: .08em; }
  .exclude-toggle input { accent-color: var(--amber); }

  .coverage { display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px; margin: 10px 13px 0; }
  .coverage div { display: flex; align-items: center; justify-content: space-between; padding: 7px 9px; border: 1px solid #33564f; background: #0e1a18; font: 8px DM Mono; letter-spacing: .08em; color: #99aaae; }
  .coverage div b { color: var(--teal); font-size: 11px; }
  .coverage div.missing { border-color: #745b32; }
  .coverage div.missing b { color: var(--amber); }
  .train-panel header b.running { color: var(--teal); }

  .result { display: grid; grid-template-columns: auto 1fr; gap: 4px 14px; margin: 12px 13px 0; padding: 8px 10px; border: 1px solid var(--line); font: 9px DM Mono; }
  .result dt { color: var(--muted); letter-spacing: .08em; }
  .result dd { margin: 0; color: #dce6e7; }
  .result dd.accuracy { color: var(--teal); }
  .result dd.path { overflow-wrap: anywhere; color: #bac8cb; }
  .confusion-wrap { margin: 10px 13px 0; }
  table.confusion { margin-top: 6px; font-size: 8px; }
  table.confusion th { position: static; padding: 4px 7px; background: none; border-bottom: 1px solid var(--line); text-align: right; }
  table.confusion thead th { text-align: right; }
  table.confusion tbody th { text-align: left; color: var(--muted); font-weight: 400; letter-spacing: .06em; border-bottom: 1px solid #1c282d; }
  table.confusion td { text-align: right; padding: 4px 7px; }
  table.confusion td.diag { color: var(--teal); }
  table.confusion td.err { color: var(--amber); }

  .log { min-height: 0; overflow-y: auto; padding: 8px 12px; background: #0a1013; font: 9px/1.6 DM Mono; color: #9fb2b6; }
  .log div { white-space: pre-wrap; overflow-wrap: anywhere; }
  .log .placeholder { color: #52636a; letter-spacing: .1em; }

  /* Short viewports: let the whole tab scroll instead of crushing panels. */
  @media (min-width: 901px) and (max-height: 640px) {
    .training-layout { overflow-y: auto; grid-template-rows: minmax(300px, auto) minmax(300px, auto); }
    .table-scroll { max-height: 45vh; }
  }
  @media (max-width: 900px) {
    .training-layout { grid-template-columns: minmax(0, 1fr); grid-template-rows: repeat(3, auto) minmax(240px, 1fr); grid-template-areas: 'capture' 'clips' 'train' 'log'; overflow-y: auto; }
    .table-scroll { max-height: 300px; }
    .pair.triple { grid-template-columns: 1fr 1fr; }
  }
</style>
