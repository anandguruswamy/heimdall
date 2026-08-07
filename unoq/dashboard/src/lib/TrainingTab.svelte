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
    seats: SeatClass[];
    person: string;
    exclude: boolean;
    hasCir: boolean;
  };

  // Occupied seats in multilabel bit order (Empty is never a bit).
  const occupiedSeats = seatClasses.slice(0, 4) as SeatClass[];
  const seatShort: Record<SeatClass, string> = { FrontLeft: 'FL', FrontRight: 'FR', BackRight: 'RR', BackLeft: 'RL', Empty: 'E' };

  let clips = $state<ClipRow[]>([]);
  let selectedIds = $state<number[]>([]);
  let clipsMessage = $state('');

  const KIND_STORAGE_KEY = 'heimdall.training.classifierKind';
  function storedKind(): 'single' | 'multilabel' {
    try {
      return localStorage.getItem(KIND_STORAGE_KEY) === 'multilabel' ? 'multilabel' : 'single';
    } catch {
      return 'single';
    }
  }
  let classifierKind = $state<'single' | 'multilabel'>(storedKind());
  const multiKind = $derived(classifierKind === 'multilabel');
  $effect(() => {
    try {
      localStorage.setItem(KIND_STORAGE_KEY, classifierKind);
    } catch { /* storage blocked; the selector still works for this session */ }
  });

  let captureName = $state('');
  let captureNote = $state('');
  let captureDurationS = $state(10);
  let captureSeat = $state<SeatClass | ''>('');
  let capturePerson = $state('');
  // Multi-person capture tag: selected occupied seats, or the explicit Empty.
  let captureSeats = $state<SeatClass[]>([]);
  let captureEmpty = $state(false);
  let captureState = $state<'idle' | 'capturing'>('idle');
  let captureProgress = $state(0);
  let captureMessage = $state('');
  let captureError = $state(false);

  let variant = $state<TrainingVariant>('calibrated');
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
  let captureWaitTimer: ReturnType<typeof setTimeout> | undefined;
  let destroyed = false;

  const selectable = $derived(clips.filter(canSelectClip));
  const backendCaptureActive = $derived(clips.some((clip) => clip.status === 'capturing'));
  const selectedClips = $derived(clips.filter((clip) => selectedIds.includes(clip.id)));
  const selectedTrainable = $derived(selectable.filter((clip) => selectedIds.includes(clip.id)));
  const trainingStatus = $derived(String(training?.status ?? 'idle'));
  const trainingRunning = $derived(trainingStatus === 'running');
  const trainingResult = $derived((training?.result ?? null) as Record<string, unknown> | null);
  const seatConfusion = $derived(matrixFrom(trainingResult?.seat_confusion ?? trainingResult?.seat_confusion_matrix ?? trainingResult?.confusion, seatClasses.length));
  const personClasses = $derived(stringsFrom(trainingResult?.person_names ?? trainingResult?.person_classes ?? trainingResult?.person_labels));
  const personConfusion = $derived(matrixFrom(trainingResult?.person_confusion ?? trainingResult?.person_confusion_matrix, personClasses.length));
  const seatAccuracy = $derived(numberFrom(trainingResult?.seat_accuracy, trainingResult?.seat_test_accuracy, trainingResult?.test_seat_accuracy, trainingResult?.test_accuracy));
  const personAccuracy = $derived(numberFrom(trainingResult?.person_accuracy, trainingResult?.person_test_accuracy, trainingResult?.test_person_accuracy));
  // Multilabel results replace the confusion matrix with per-seat metrics.
  const multilabelResult = $derived(trainingResult?.model_mode === 'multilabel' ? trainingResult : null);
  const perSeatMetrics = $derived.by(() => {
    const perSeat = multilabelResult?.per_seat as Record<string, Record<string, unknown>> | undefined;
    if (!perSeat) return null;
    const rows = occupiedSeats
      .filter((seat) => typeof perSeat[seat] === 'object' && perSeat[seat] !== null)
      .map((seat) => ({ seat, ...perSeat[seat] } as { seat: SeatClass } & Record<string, unknown>));
    return rows.length === occupiedSeats.length ? rows : null;
  });
  const subsetAccuracy = $derived(numberFrom(multilabelResult?.subset_accuracy));
  const meanBitAccuracy = $derived(numberFrom(multilabelResult?.mean_bit_accuracy));
  // Person labels embedding a seat class name (or the TwoPeople marker) would
  // corrupt the multilabel folder-name encoding; the backend rejects them at
  // run start and the UI warns earlier.
  function personCorrupts(person: string): boolean {
    const sanitized = person.replace(/[^0-9A-Za-z]/g, '');
    return [...occupiedSeats, 'Empty', 'TwoPeople'].some((name) => sanitized.includes(name));
  }
  const selectedMultiTagged = $derived(selectedClips.filter((clip) => clip.seats.length >= 2));
  const selectedCorruptPersons = $derived(multiKind ? selectedClips.filter((clip) => personCorrupts(clip.person)) : []);
  const modeMismatch = $derived.by(() => {
    if (!multiKind && selectedMultiTagged.length) {
      return `${selectedMultiTagged.length} SELECTED CLIP${selectedMultiTagged.length === 1 ? ' HAS' : 'S HAVE'} MULTI-SEAT TAGS · SWITCH TO MULTI-PERSON MODE OR RETAG`;
    }
    if (multiKind && selectedCorruptPersons.length) {
      return `PERSON LABELS ON CLIP${selectedCorruptPersons.length === 1 ? '' : 'S'} ${selectedCorruptPersons.map((clip) => String(clip.id).padStart(6, '0')).join(', ')} CONTAIN A SEAT CLASS NAME · RENAME BEFORE MULTI-PERSON TRAINING`;
    }
    return '';
  });
  const canTrain = $derived(selectedIds.length > 0 && !trainingRunning && captureState !== 'capturing' && !modeMismatch);

  function canSelectClip(clip: ClipRow): boolean {
    return clip.status !== 'capturing';
  }

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
      const taggedSeats = Array.isArray(tag?.seats)
        ? occupiedSeats.filter((name) => (tag.seats as unknown[]).includes(name))
        : seat && seat !== 'Empty' ? [seat] : [];
      return {
        id: Number(record.id),
        name: String(v.name ?? ''),
        status: String(v.status ?? 'unknown'),
        error: String(v.error ?? ''),
        durationS: Number(v.duration_s ?? manifest.duration_s ?? 0),
        seat,
        seats: taggedSeats,
        person: seat === 'Empty' ? '' : String(tag?.person ?? ''),
        exclude: tag?.exclude === true,
        hasCir: Number(manifest.aligned_cir_records ?? 0) > 0,
      };
    });
  }

  async function refreshClips() {
    try {
      clips = parseClips(await api.getClips());
      const selectableIds = new Set(clips.filter(canSelectClip).map((clip) => clip.id));
      selectedIds = selectedIds.filter((id) => selectableIds.has(id));
      clipsMessage = '';
    } catch (error) {
      clipsMessage = error instanceof Error ? error.message : 'Clip list unavailable';
    }
  }

  function wait(ms: number): Promise<void> {
    return new Promise((resolve) => { captureWaitTimer = setTimeout(resolve, ms); });
  }

  async function captureClip(name: string, note: string, durationS: number, seat: SeatClass | '', person: string): Promise<number> {
    if (captureState === 'capturing' || backendCaptureActive) throw new Error('Another UWB clip capture is active');
    captureState = 'capturing';
    captureProgress = 0;
    captureError = false;
    captureMessage = `Capturing ${durationS} s from trigger...`;
    try {
      const created = await api.saveClip({
        name,
        note,
        duration_s: durationS,
        board_positions: boardPositions(),
      }) as Record<string, unknown>;
      const clipId = Number(created.id);
      const started = Date.now();
      const pollMs = Math.max(15_000, durationS * 1000 + 5_000);
      while (Date.now() - started < pollMs) {
        await wait(1_000);
        if (destroyed) throw new Error('Capture cancelled');
        captureProgress = Math.min(99, ((Date.now() - started) / pollMs) * 100);
        await refreshClips();
        const row = clips.find((clip) => clip.id === clipId);
        if (row?.status === 'complete') {
          captureProgress = 100;
          if (multiKind) {
            if (captureEmpty) {
              await tag(clipId, 'Empty', '', false);
              captureMessage = `Clip ${clipId} captured and tagged EMPTY`;
            } else if (captureSeats.length) {
              const person = captureSeats.length === 1 ? capturePerson.trim() : '';
              await tagSeats(clipId, captureSeats, person, false);
              captureMessage = `Clip ${clipId} captured and tagged ${captureSeats.map((seat) => seatDisplay[seat]).join(' + ')}${person ? ` · ${person}` : ''}`;
            } else {
              captureMessage = `Clip ${clipId} captured · assign seat tags to include it in training`;
            }
          } else {
            const taggedPerson = seat === 'Empty' ? '' : person.trim();
            if (seat) {
              await api.setClipTraining(clipId, { seat, person: taggedPerson, exclude: false });
              await refreshClips();
              captureMessage = `Clip ${clipId} captured and tagged ${seatDisplay[seat]}${taggedPerson ? ` / ${taggedPerson}` : ''}`;
            } else {
              captureMessage = `Clip ${clipId} captured / assign a seat tag to include it in training`;
            }
          }
          captureState = 'idle';
          return clipId;
        }
        if (row?.status === 'failed') throw new Error(row.error || 'Capture failed on the backend');
      }
      throw new Error('Clip finalization timed out');
    } catch (error) {
      captureState = 'idle';
      captureProgress = 0;
      captureError = true;
      captureMessage = error instanceof Error ? error.message : 'Capture failed';
      throw error;
    }
  }

  async function capture() {
    try {
      await captureClip(captureName.trim(), captureNote.trim(), captureDurationS, captureSeat, capturePerson);
    } catch { /* captureClip reports manual capture errors in the panel */ }
  }

  async function tag(id: number, seat: SeatClass | null, person: string, exclude: boolean) {
    try {
      await api.setClipTraining(id, { seat, person: seat === 'Empty' ? '' : person, exclude });
      await refreshClips();
    } catch (error) {
      clipsMessage = error instanceof Error ? error.message : `Clip ${id} could not be tagged`;
    }
  }

  // Multi-person tags: one seat keeps the canonical single shape, two or more
  // send the seats array (person is unavailable there), none removes the tag.
  async function tagSeats(id: number, seats: SeatClass[], person: string, exclude: boolean) {
    try {
      if (!seats.length) await api.setClipTraining(id, { seat: null });
      else if (seats.length === 1) await api.setClipTraining(id, { seat: seats[0], person, exclude });
      else await api.setClipTraining(id, { seat: seats[0], seats, person: '', exclude });
      await refreshClips();
    } catch (error) {
      clipsMessage = error instanceof Error ? error.message : `Clip ${id} could not be tagged`;
    }
  }

  function toggleCaptureSeat(seat: SeatClass) {
    captureEmpty = false;
    captureSeats = captureSeats.includes(seat)
      ? captureSeats.filter((name) => name !== seat)
      : occupiedSeats.filter((name) => name === seat || captureSeats.includes(name));
    if (captureSeats.length !== 1) capturePerson = '';
  }

  function toggleCaptureEmpty() {
    captureEmpty = !captureEmpty;
    if (captureEmpty) {
      captureSeats = [];
      capturePerson = '';
    }
  }

  function toggleRowSeat(clip: ClipRow, seat: SeatClass) {
    const seats = clip.seats.includes(seat)
      ? clip.seats.filter((name) => name !== seat)
      : occupiedSeats.filter((name) => name === seat || clip.seats.includes(name));
    void tagSeats(clip.id, seats, seats.length === 1 ? clip.person : '', clip.exclude);
  }

  function setSelected(id: number, selected: boolean) {
    selectedIds = selected
      ? [...new Set([...selectedIds, id])]
      : selectedIds.filter((selectedId) => selectedId !== id);
  }

  function selectAll() {
    selectedIds = selectable.map((clip) => clip.id);
  }

  function unselectAll() {
    selectedIds = [];
  }

  async function deleteSelected() {
    if (!selectedIds.length || !confirm(`Delete ${selectedIds.length} selected clip${selectedIds.length === 1 ? '' : 's'} permanently?`)) return;
    try {
      await Promise.all(selectedIds.map((id) => api.deleteClip(id)));
      selectedIds = [];
      await refreshClips();
    } catch (error) {
      clipsMessage = error instanceof Error ? error.message : 'Selected clips could not be deleted';
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
      const started = await api.startTraining({ clip_ids: [...selectedIds], variant, epochs, mode: multiKind ? 'multilabel' : mode, architecture, link_mode: linkMode, taps_left: tapsLeft, taps_right: tapsRight, patience }) as Record<string, unknown>;
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
      clearTimeout(captureWaitTimer);
    };
  });
</script>

<section class="training-layout">
  <div class="panel mode-bar">
    <label>CLASSIFICATION MODE
      <select bind:value={classifierKind} disabled={trainingRunning || captureState === 'capturing'}>
        <option value="single">Single person</option>
        <option value="multilabel">Multi person (multi-label)</option>
      </select>
    </label>
    <p class="note">{multiKind ? 'ONE INDEPENDENT DETECTOR PER SEAT · TAG ANY SEAT COMBINATION INCLUDING EMPTY' : 'ONE OCCUPANT OR EMPTY · FIVE-CLASS CLASSIFIER'}</p>
  </div>
  <article class="panel capture-panel">
    <header><span>CAPTURE LABELED CLIP</span><b>{captureState === 'capturing' ? `${Math.round(captureProgress)}%` : backendCaptureActive ? 'ACTIVE' : 'READY'}</b></header>
    <div class="panel-body">
      <label>NAME<input maxlength="120" bind:value={captureName} placeholder="Optional label" /></label>
      <label>NOTE<input maxlength="2000" bind:value={captureNote} placeholder="Optional context" /></label>
      {#if multiKind}
        <div class="pair single">
          <label>LENGTH<select bind:value={captureDurationS}>{#each [5, 10, 15, 30, 60] as seconds}<option value={seconds}>{seconds} s</option>{/each}</select></label>
        </div>
        <p class="chips-label">SEAT TAGS</p>
        <div class="seat-chips">
          {#each occupiedSeats as seat (seat)}
            <button type="button" class:active={captureSeats.includes(seat)} onclick={() => toggleCaptureSeat(seat)}>{seatDisplay[seat]}</button>
          {/each}
          <button type="button" class="empty-chip" class:active={captureEmpty} onclick={toggleCaptureEmpty}>EMPTY</button>
        </div>
        <label>PERSON<input maxlength="60" bind:value={capturePerson} disabled={captureSeats.length !== 1 || captureEmpty} placeholder={captureEmpty ? 'N/A for empty' : captureSeats.length > 1 ? 'N/A · multiple occupants' : captureSeats.length === 1 ? 'Who is seated' : 'Pick exactly one seat first'} /></label>
        {#if capturePerson && personCorrupts(capturePerson)}<p class="note error">PERSON NAMES MAY NOT CONTAIN A SEAT CLASS NAME OR "TWOPEOPLE" IN MULTI-PERSON MODE</p>{/if}
      {:else}
        <div class="pair">
          <label>LENGTH<select bind:value={captureDurationS}>{#each [5, 10, 15, 30, 60] as seconds}<option value={seconds}>{seconds} s</option>{/each}</select></label>
          <label>SEAT TAG<select value={captureSeat} onchange={(e) => setCaptureSeat(e.currentTarget.value)}><option value="">— untagged —</option>{#each seatClasses as seat}<option value={seat}>{seatDisplay[seat]}</option>{/each}</select></label>
        </div>
        <label>PERSON<input maxlength="60" bind:value={capturePerson} disabled={captureSeat === 'Empty'} placeholder={captureSeat === 'Empty' ? 'N/A for empty' : 'Who is seated'} /></label>
      {/if}
      <button class="snapshot" onclick={capture} disabled={captureState === 'capturing' || backendCaptureActive}>{captureState === 'capturing' ? `CAPTURING ${Math.round(captureProgress)}%` : backendCaptureActive ? 'CAPTURE ALREADY ACTIVE' : `CAPTURE ${captureDurationS} S CLIP`}</button>
      <div class="capture-progress"><i style={`width:${captureProgress}%`}></i></div>
      {#if captureMessage}<p class="note" class:error={captureError}>{captureMessage}</p>{/if}
    </div>
  </article>

  <article class="panel clips-panel">
    <header>
      <span>CAPTURED CLIPS</span>
      <span class="header-side">
        <b>{selectedClips.length} SELECTED</b>
        {#if selectedIds.length > 0}
          <button class="text-button" onclick={selectAll}>SELECT ALL</button>
          <button class="text-button" onclick={unselectAll}>UNSELECT</button>
          <button class="text-button" onclick={() => void deleteSelected()}>DELETE</button>
        {/if}
        <button class="text-button" onclick={() => void refreshClips()}>REFRESH</button>
      </span>
    </header>
    <div class="table-scroll">
      <table>
        <thead><tr><th class="select-col"></th><th>ID</th><th>NAME</th><th>SEAT TAG</th><th>PERSON</th><th>LEN</th></tr></thead>
        <tbody>
          {#each clips as clip (clip.id)}
            <tr>
              <td class="select-col"><input type="checkbox" aria-label={`Select clip ${clip.id}`} checked={selectedIds.includes(clip.id)} disabled={!canSelectClip(clip)} title={canSelectClip(clip) ? 'Select this clip' : 'Wait for capture to complete'} onchange={(e) => setSelected(clip.id, e.currentTarget.checked)} /></td>
              <td class="mono">{String(clip.id).padStart(6, '0')}</td>
              <td class="name">{clip.name || '—'}</td>
              <td>
                {#if multiKind}
                  <div class="row-chips">
                    {#each occupiedSeats as seat, index (seat)}
                      <button type="button" class:active={clip.seats.includes(seat)} disabled={clip.status !== 'complete'} title={seatDisplay[seat]} onclick={() => toggleRowSeat(clip, seat)}>{['FL', 'FR', 'RR', 'RL'][index]}</button>
                    {/each}
                    <button type="button" class="empty-chip" class:active={clip.seat === 'Empty'} disabled={clip.status !== 'complete'} title="Empty cabin" onclick={() => void tag(clip.id, clip.seat === 'Empty' ? null : 'Empty', '', clip.exclude)}>E</button>
                  </div>
                {:else}
                  <select value={clip.seats.length >= 2 ? '__multi__' : clip.seat ?? ''} disabled={clip.status !== 'complete'} onchange={(e) => { const seat = seatFrom(e.currentTarget.value); void tag(clip.id, seat, seat === 'Empty' ? '' : clip.person, clip.exclude); }}>
                    <option value="">—</option>
                    {#if clip.seats.length >= 2}<option value="__multi__">{clip.seats.map((seat) => seatShort[seat]).join('+')} · MULTI</option>{/if}
                    {#each seatClasses as seat}<option value={seat}>{seatDisplay[seat]}</option>{/each}
                  </select>
                {/if}
              </td>
              <td><input value={clip.person} maxlength="60" disabled={clip.seats.length >= 2 || (multiKind ? clip.seats.length !== 1 : (!clip.seat || clip.seat === 'Empty'))} placeholder={clip.seat === 'Empty' ? 'N/A' : clip.seats.length >= 2 ? 'N/A · multiple' : '—'} onchange={(e) => void tag(clip.id, clip.seat, e.currentTarget.value.trim(), clip.exclude)} /></td>
              <td class="mono">{clip.durationS ? `${clip.durationS}s` : '—'}</td>
            </tr>
          {/each}
          {#if !clips.length}<tr><td colspan="6" class="empty">NO CLIPS CAPTURED YET</td></tr>{/if}
        </tbody>
      </table>
    </div>
    {#if clipsMessage}<p class="note error foot">{clipsMessage}</p>{/if}
  </article>

  <article class="panel train-panel">
    <header><span>TRAIN CIR CLASSIFIER</span><b class:running={trainingRunning}>{trainingStatus.toUpperCase()}</b></header>
    <div class="panel-body">
      <div class="coverage">
        {#if multiKind}
          {#each occupiedSeats as seat}
            <div class:missing={!selectedTrainable.some((clip) => clip.seats.includes(seat))}>
              <span>{seatDisplay[seat]}</span>
              <b>{selectedTrainable.filter((clip) => clip.seats.includes(seat)).length}</b>
            </div>
          {/each}
          <div class:missing={!selectedTrainable.some((clip) => clip.seat === 'Empty')}>
            <span>EMPTY</span>
            <b>{selectedTrainable.filter((clip) => clip.seat === 'Empty').length}</b>
          </div>
        {:else}
          {#each seatClasses as seat}
            <div class:missing={!selectedTrainable.some((clip) => clip.seat === seat)}>
              <span>{seatDisplay[seat]}</span>
              <b>{selectedTrainable.filter((clip) => clip.seat === seat).length}</b>
            </div>
          {/each}
        {/if}
      </div>
      {#if multiKind}
        <div class="pair triple">
          <label>VARIANT<select bind:value={variant} disabled={trainingRunning}><option value="raw">raw</option><option value="calibrated">calibrated</option></select></label>
          <label>PATIENCE<input type="number" min="0" bind:value={patience} disabled={trainingRunning} /></label>
          <label>EPOCHS<input type="number" min="1" max="500" bind:value={epochs} disabled={trainingRunning} /></label>
        </div>
        <p class="note">MULTI-LABEL · FIXED 20-LINK / 64-TAP FULL-WINDOW GEOMETRY · ONE INDEPENDENT DETECTOR PER SEAT</p>
      {:else}
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
      {/if}
      <button class="snapshot" onclick={startTraining} disabled={!canTrain}>{trainingRunning ? 'TRAINING IN PROGRESS…' : 'TRAIN MODEL'}</button>
      {#if selectedIds.length === 0}<p class="note">SELECT AT LEAST ONE CAPTURED CLIP TO TRAIN</p>{/if}
      {#if modeMismatch}<p class="note error">{modeMismatch}</p>{/if}
      {#if trainMessage}<p class="note error">{trainMessage}</p>{/if}
      {#if trainingStatus === 'failed'}
        <p class="note error">{String(training?.error ?? 'Training failed')}</p>
      {/if}
      {#if multilabelResult}
        <dl class="result">
          <dt>SUBSET ACCURACY</dt>
          <dd class="accuracy">{subsetAccuracy !== null ? `${(subsetAccuracy * 100).toFixed(2)}% · ALL FOUR SEATS CORRECT` : '—'}</dd>
          <dt>MEAN BIT ACCURACY</dt>
          <dd class="accuracy">{meanBitAccuracy !== null ? `${(meanBitAccuracy * 100).toFixed(2)}%` : '—'}</dd>
          <dt>SAVED MODEL</dt>
          <dd class="path">{String(multilabelResult.model_path ?? '—')}</dd>
        </dl>
        {#if perSeatMetrics}
          <div class="confusion-wrap">
            <p class="note">PER-SEAT METRICS · INDEPENDENT DETECTORS</p>
            <table class="confusion">
              <thead><tr><th></th><th>PRECISION</th><th>RECALL</th><th>F1</th><th>ACC</th><th>SUPPORT</th></tr></thead>
              <tbody>
                {#each perSeatMetrics as row (row.seat)}
                  <tr>
                    <th>{seatDisplay[row.seat]}</th>
                    <td>{(Number(row.precision) * 100).toFixed(2)}%</td>
                    <td>{(Number(row.recall) * 100).toFixed(2)}%</td>
                    <td>{(Number(row.f1) * 100).toFixed(2)}%</td>
                    <td>{(Number(row.accuracy) * 100).toFixed(2)}%</td>
                    <td>{Number(row.support)}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      {:else if trainingResult}
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
    /* Both content rows take fixed shares of the viewport so a growing clips
       table can never squeeze the train/log row out; panels scroll internally. */
    grid-template-rows: auto minmax(0, 1fr) minmax(0, 1fr);
    grid-template-areas: 'modebar modebar' 'capture clips' 'train log';
  }
  .mode-bar { grid-area: modebar; display: flex; align-items: center; gap: 14px; padding: 8px 13px; }
  .mode-bar label { display: flex; align-items: center; gap: 10px; color: #99aaae; font: 9px DM Mono; letter-spacing: .08em; white-space: nowrap; }
  .mode-bar select { font: 11px DM Mono; }
  .mode-bar .note { margin: 0; }
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

  .header-side { display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
  .text-button { border: 0; background: none; color: var(--amber); font: 8px DM Mono; letter-spacing: .08em; cursor: pointer; }
  .text-button:disabled { opacity: .4; cursor: not-allowed; }

  .table-scroll { min-height: 0; overflow: auto; }
  table { width: 100%; border-collapse: collapse; font: 9px DM Mono; }
  thead th { position: sticky; top: 0; z-index: 1; background: #101a1e; color: var(--muted); font-weight: 400; letter-spacing: .1em; text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); }
  tbody td { padding: 5px 10px; border-bottom: 1px solid #1c282d; color: #bac8cb; }
  th.select-col, td.select-col { width: 26px; padding-left: 10px; padding-right: 2px; text-align: center; }
  td.select-col input { margin: 0; accent-color: var(--amber); cursor: pointer; }
  td.select-col input:disabled { cursor: not-allowed; }
  td.mono { color: var(--muted); }
  td.name { max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  tbody select, tbody input:not([type='checkbox']) { width: 100%; min-width: 90px; font: 9px DM Mono; }
  td.empty { padding: 16px 10px; color: var(--muted); letter-spacing: .1em; }

  .chips-label { margin: 10px 13px 0; color: #99aaae; font: 9px DM Mono; letter-spacing: .08em; }
  .seat-chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 13px 0; }
  .seat-chips button { border: 1px solid #26373d; background: #0b1215; color: #718188; padding: 7px 9px; font: 9px DM Mono; letter-spacing: .06em; cursor: pointer; }
  .seat-chips button.active { border-color: var(--teal); background: #0d211e; color: var(--teal); }
  .seat-chips button.empty-chip.active { border-color: var(--amber); background: #211a0d; color: var(--amber); }
  .row-chips { display: flex; gap: 3px; }
  .row-chips button { border: 1px solid #26373d; background: #0b1215; color: #718188; min-width: 22px; padding: 3px 0; font: 8px DM Mono; cursor: pointer; }
  .row-chips button.active { border-color: var(--teal); background: #0d211e; color: var(--teal); }
  .row-chips button.empty-chip.active { border-color: var(--amber); background: #211a0d; color: var(--amber); }
  .row-chips button:disabled { opacity: .4; cursor: not-allowed; }
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
    .training-layout { overflow-y: auto; grid-template-rows: auto minmax(300px, auto) minmax(300px, auto); }
    .table-scroll { max-height: 45vh; }
  }
  @media (max-width: 900px) {
    .training-layout { grid-template-columns: minmax(0, 1fr); grid-template-rows: repeat(4, auto) minmax(240px, 1fr); grid-template-areas: 'modebar' 'capture' 'clips' 'train' 'log'; overflow-y: auto; }
    .table-scroll { max-height: 300px; }
    .pair.triple { grid-template-columns: 1fr 1fr; }
    .mode-bar { flex-wrap: wrap; }
  }
</style>
