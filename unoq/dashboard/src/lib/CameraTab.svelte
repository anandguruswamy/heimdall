<script lang="ts">
  import { onMount } from 'svelte';
  import type { HeimdallApi } from './api';
  import { seatClasses, type CameraSessionSummary, type CameraStatus, type SeatClass } from './types';

  let { api, boardPositions }: { api: HeimdallApi; boardPositions: () => unknown } = $props();

  const seatDisplay: Record<SeatClass, string> = {
    FrontLeft: 'FRONT LEFT',
    FrontRight: 'FRONT RIGHT',
    BackRight: 'REAR RIGHT',
    BackLeft: 'REAR LEFT',
    Empty: 'EMPTY',
  };
  const calibrationTargets: SeatClass[] = ['Empty', 'FrontLeft', 'FrontRight', 'BackLeft', 'BackRight'];

  type ClipRow = {
    id: number;
    status: string;
    error: string;
    seat: SeatClass | null;
    person: string;
  };

  let clips = $state<ClipRow[]>([]);
  let cameraStatus = $state<CameraStatus | null>(null);
  let cameraSessions = $state<CameraSessionSummary[]>([]);
  let cameraPerson = $state('');
  let cameraMessage = $state('');
  let cameraError = $state(false);
  let cameraBusy = $state(false);
  let previewUrl = $state('');
  let previewFailed = $state(false);
  let sessionsOpen = $state(false);
  let calibrationState = $state<'idle' | 'transition' | 'prompting' | 'capturing' | 'complete' | 'error'>('idle');
  let calibrationComplete = $state(false);
  let collectionMode = $state<'calibration' | 'continuous'>('calibration');
  let calibrationIndex = $state(0);
  let promptTarget = $state<SeatClass | null>(null);
  let countdown = $state<number | null>(null);
  let captureState = $state<'idle' | 'capturing'>('idle');
  let captureProgress = $state(0);

  let cameraPollTimer: ReturnType<typeof setTimeout> | undefined;
  let previewTimer: ReturnType<typeof setInterval> | undefined;
  let captureWaitTimer: ReturnType<typeof setTimeout> | undefined;
  let midpointTimer: ReturnType<typeof setTimeout> | undefined;
  let countdownTimer: ReturnType<typeof setTimeout> | undefined;
  let transitionTimer: ReturnType<typeof setTimeout> | undefined;
  let destroyed = false;

  const activeCameraSession = $derived(cameraStatus?.state === 'recording' ? cameraStatus.session : null);
  const cameraReady = $derived(cameraStatus?.enabled === true && cameraStatus.state !== 'disabled' && cameraStatus.state !== 'error');
  const guidedBusy = $derived(calibrationState === 'capturing' || cameraBusy || captureState === 'capturing');
  const backendCaptureActive = $derived(clips.some((clip) => clip.status === 'capturing'));

  const cue = $derived.by(() => {
    if (calibrationState === 'transition') return 'MOVE AROUND';
    if (calibrationState === 'idle') return activeCameraSession ? 'READY' : 'START A CAMERA SESSION';
    if (calibrationState === 'prompting') return promptTarget ? seatDisplay[promptTarget] : '—';
    if (calibrationState === 'capturing') return countdown === null ? 'RECORDING' : String(countdown);
    if (calibrationState === 'complete') return calibrationComplete ? 'CALIBRATION COMPLETE' : 'SAMPLE COMPLETE';
    return 'HALTED';
  });
  const cueSub = $derived.by(() => {
    if (calibrationState === 'transition') return 'TRANSITION IS NOT CAPTURED';
    if (calibrationState === 'idle') return activeCameraSession ? 'START THE 5-TARGET CALIBRATION WHEN SETTLED' : 'ENTER PARTICIPANT NAME AND START';
    if (calibrationState === 'prompting') return 'MOVE TO THE NAMED TARGET AND CONFIRM ONLY WHEN STABLE';
    if (calibrationState === 'capturing') return countdown === null ? 'HOLD POSITION · 10 S CLIP' : 'HOLD STILL';
    if (calibrationState === 'complete') return calibrationComplete ? 'CAMERA SESSION REMAINS RECORDING FOR CONTINUOUS COLLECTION' : 'CHOOSE A RANDOM NEXT TARGET WHEN READY';
    return 'RESOLVE THE ERROR THEN REJECT / RE-PROMPT OR RESTART';
  });

  function parseClips(value: unknown): ClipRow[] {
    if (!Array.isArray(value)) return [];
    return value.map((row) => {
      const record = row as Record<string, unknown>;
      const v = (record.value ?? {}) as Record<string, unknown>;
      const tag = (v.training ?? null) as Record<string, unknown> | null;
      const seat = seatClasses.find((name) => name === tag?.seat) ?? null;
      return {
        id: Number(record.id),
        status: String(v.status ?? 'unknown'),
        error: String(v.error ?? ''),
        seat,
        person: seat === 'Empty' ? '' : String(tag?.person ?? ''),
      };
    });
  }

  async function refreshClips() {
    try {
      clips = parseClips(await api.getClips());
    } catch { /* capture polling will surface backend errors */ }
  }

  function wait(ms: number): Promise<void> {
    return new Promise((resolve) => { captureWaitTimer = setTimeout(resolve, ms); });
  }

  async function captureClip(name: string, note: string, durationS: number, seat: SeatClass | '', person: string): Promise<number> {
    if (captureState === 'capturing' || backendCaptureActive) throw new Error('Another UWB clip capture is active');
    captureState = 'capturing';
    captureProgress = 0;
    cameraError = false;
    cameraMessage = `Capturing ${durationS} s from trigger...`;
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
          const taggedPerson = seat === 'Empty' ? '' : person.trim();
          if (seat) await api.setClipTraining(clipId, { seat, person: taggedPerson, exclude: false });
          captureState = 'idle';
          return clipId;
        }
        if (row?.status === 'failed') throw new Error(row.error || 'Capture failed on the backend');
      }
      throw new Error('Clip finalization timed out');
    } catch (error) {
      captureState = 'idle';
      captureProgress = 0;
      cameraError = true;
      cameraMessage = error instanceof Error ? error.message : 'Capture failed';
      throw error;
    }
  }

  function parseCameraSessions(value: CameraSessionSummary[] | { sessions: CameraSessionSummary[] }): CameraSessionSummary[] {
    return Array.isArray(value) ? value : Array.isArray(value.sessions) ? value.sessions : [];
  }

  async function refreshCameraSessions() {
    try {
      cameraSessions = parseCameraSessions(await api.cameraSessions());
    } catch (error) {
      cameraError = true;
      cameraMessage = error instanceof Error ? error.message : 'Camera session list unavailable';
    }
  }

  async function pollCameraStatus() {
    clearTimeout(cameraPollTimer);
    if (destroyed) return;
    try {
      cameraStatus = await api.cameraStatus();
      if (!cameraBusy && cameraStatus.error) {
        cameraError = true;
        cameraMessage = cameraStatus.error;
      }
    } catch (error) {
      cameraStatus = null;
      if (!cameraBusy) {
        cameraError = true;
        cameraMessage = error instanceof Error ? error.message : 'Camera status unavailable';
      }
    } finally {
      if (!destroyed) cameraPollTimer = setTimeout(() => void pollCameraStatus(), 1_000);
    }
  }

  function refreshPreview() {
    if (cameraStatus?.state === 'recording' && cameraStatus.preview_available) {
      previewFailed = false;
      previewUrl = api.cameraPreviewUrl(Date.now());
    } else {
      previewUrl = '';
    }
  }

  async function startCameraSession() {
    const person = cameraPerson.trim();
    if (!person || !cameraReady) return;
    cameraBusy = true;
    cameraError = false;
    cameraMessage = '';
    try {
      await api.startCameraSession(person);
      await pollCameraStatus();
      await refreshCameraSessions();
      cameraMessage = 'Camera session recording.';
    } catch (error) {
      cameraError = true;
      cameraMessage = error instanceof Error ? error.message : 'Camera session could not be started';
    } finally {
      cameraBusy = false;
    }
  }

  async function stopCameraSession() {
    if (!activeCameraSession || guidedBusy) return;
    cameraBusy = true;
    cameraError = false;
    try {
      await api.stopCameraSession(activeCameraSession.id);
      if ('speechSynthesis' in window) window.speechSynthesis.cancel();
      calibrationState = calibrationComplete ? 'complete' : 'idle';
      promptTarget = null;
      countdown = null;
      await pollCameraStatus();
      await refreshCameraSessions();
      cameraMessage = 'Recording stopped. Video remains retained until manual deletion.';
    } catch (error) {
      cameraError = true;
      cameraMessage = error instanceof Error ? error.message : 'Camera session could not be stopped';
    } finally {
      cameraBusy = false;
    }
  }

  async function deleteCameraSession(session: CameraSessionSummary) {
    if (activeCameraSession?.id === session.id || !confirm(`Delete camera session ${session.id} and its retained video permanently?`)) return;
    cameraBusy = true;
    cameraError = false;
    try {
      await api.deleteCameraSession(session.id);
      await refreshCameraSessions();
      cameraMessage = `Camera session ${session.id} deleted.`;
    } catch (error) {
      cameraError = true;
      cameraMessage = error instanceof Error ? error.message : 'Camera session could not be deleted';
    } finally {
      cameraBusy = false;
    }
  }

  function speakPrompt(target: SeatClass) {
    if (!('speechSynthesis' in window)) return;
    window.speechSynthesis.cancel();
    const spokenSeat = target === 'BackLeft' ? 'rear left' : target === 'BackRight' ? 'rear right' : seatDisplay[target].toLowerCase();
    window.speechSynthesis.speak(new SpeechSynthesisUtterance(`${collectionMode === 'calibration' ? 'Calibration target' : 'Next target'}: ${spokenSeat}. Confirm when stable.`));
  }

  function startCalibration() {
    if (!activeCameraSession || guidedBusy) return;
    calibrationComplete = false;
    collectionMode = 'calibration';
    calibrationIndex = 0;
    promptTarget = calibrationTargets[0];
    calibrationState = 'prompting';
    cameraError = false;
    cameraMessage = 'Follow the on-screen and spoken target prompts.';
    speakPrompt(promptTarget);
  }

  function awaitCountdown(): Promise<void> {
    return new Promise((resolve) => {
      countdown = 3;
      const tick = () => {
        if (countdown === null || countdown <= 1) {
          countdown = null;
          resolve();
          return;
        }
        countdown -= 1;
        countdownTimer = setTimeout(tick, 1_000);
      };
      countdownTimer = setTimeout(tick, 1_000);
    });
  }

  async function confirmStable() {
    const session = activeCameraSession;
    const target = promptTarget;
    if (!session || !target || guidedBusy || calibrationState !== 'prompting') return;
    calibrationState = 'capturing';
    cameraError = false;
    cameraMessage = `Stable ${seatDisplay[target]}: countdown, then synchronized 10 s UWB clip.`;
    const clipPerson = target === 'Empty' ? '' : session.person;
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const clipName = `camera-${session.id}-${target}-${stamp}`;
    let midpointEvent: Promise<void> | undefined;
    let midpointError: unknown;
    try {
      await awaitCountdown();
      await api.cameraSessionEvent(session.id, { kind: 'stable_start', seat: target, note: 'Operator confirmed stable' });
      const midpointKind = collectionMode === 'calibration' ? 'calibration_sample' : 'collection_sample';
      midpointEvent = new Promise((resolve) => {
        midpointTimer = setTimeout(() => {
          void api.cameraSessionEvent(session.id, { kind: midpointKind, seat: target, note: 'UWB clip midpoint' })
            .catch((error) => { midpointError = error; })
            .finally(resolve);
        }, 5_000);
      });
      await captureClip(clipName, `Camera-guided ${seatDisplay[target]} stable sample; session ${session.id}`, 10, target, clipPerson);
      await midpointEvent;
      if (midpointError) throw midpointError;
      await api.cameraSessionEvent(session.id, { kind: 'stable_end', seat: target, note: 'Synchronized UWB clip complete' });

      if (collectionMode === 'calibration' && calibrationIndex < calibrationTargets.length - 1) {
        calibrationIndex += 1;
        const next = calibrationTargets[calibrationIndex];
        calibrationState = 'transition';
        cameraMessage = 'Sample saved. Move around; transition is not captured.';
        if ('speechSynthesis' in window) window.speechSynthesis.cancel();
        transitionTimer = setTimeout(() => {
          promptTarget = next;
          calibrationState = 'prompting';
          cameraMessage = `Transition to ${seatDisplay[next]}; confirm when stable.`;
          speakPrompt(next);
        }, 3_000);
      } else if (collectionMode === 'calibration') {
        calibrationComplete = true;
        calibrationState = 'complete';
        cameraMessage = 'All five named targets captured. Video remains recording until manually stopped.';
        if ('speechSynthesis' in window) {
          window.speechSynthesis.cancel();
          window.speechSynthesis.speak(new SpeechSynthesisUtterance('Calibration complete.'));
        }
      } else {
        calibrationState = 'complete';
        cameraMessage = 'Continuous sample saved. Transition was not captured.';
      }
    } catch (error) {
      clearTimeout(midpointTimer);
      clearTimeout(countdownTimer);
      calibrationState = 'error';
      cameraError = true;
      cameraMessage = error instanceof Error ? error.message : 'Guided sample failed';
      if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    }
  }

  async function rejectAndReprompt() {
    const session = activeCameraSession;
    const target = promptTarget;
    if (!session || !target || guidedBusy) return;
    cameraBusy = true;
    cameraError = false;
    try {
      await api.cameraSessionEvent(session.id, { kind: 'rejected', seat: target, note: 'Operator rejected stability; no UWB clip captured' });
      calibrationState = 'prompting';
      cameraMessage = `${seatDisplay[target]} rejected. Repeating the same named target; no transition captured.`;
      speakPrompt(target);
    } catch (error) {
      calibrationState = 'error';
      cameraError = true;
      cameraMessage = error instanceof Error ? error.message : 'Rejection event failed';
    } finally {
      cameraBusy = false;
    }
  }

  function randomNextTarget() {
    if (!activeCameraSession || !calibrationComplete || guidedBusy) return;
    const choices = calibrationTargets.filter((target) => target !== promptTarget);
    promptTarget = choices[Math.floor(Math.random() * choices.length)] ?? calibrationTargets[0];
    collectionMode = 'continuous';
    calibrationState = 'prompting';
    cameraError = false;
    cameraMessage = 'Move to the named target and confirm when stable.';
    speakPrompt(promptTarget);
  }

  onMount(() => {
    void pollCameraStatus();
    void refreshCameraSessions();
    void refreshClips();
    previewTimer = setInterval(refreshPreview, 500);
    return () => {
      destroyed = true;
      clearTimeout(cameraPollTimer);
      clearInterval(previewTimer);
      clearTimeout(captureWaitTimer);
      clearTimeout(midpointTimer);
      clearTimeout(countdownTimer);
      clearTimeout(transitionTimer);
      if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    };
  });
</script>

<section class="camera-layout">
  <header class="camera-topbar">
    <div class="identity">
      <strong>BRIO CAMERA</strong>
      <span class:ready={cameraReady} class:fault={cameraStatus?.state === 'error'}>{cameraStatus?.state?.toUpperCase() ?? 'OFFLINE'}</span>
      {#if cameraStatus}
        <small>{cameraStatus.enabled ? `${cameraStatus.width}x${cameraStatus.height} @ ${cameraStatus.fps} FPS` : 'CAMERA DISABLED'}</small>
      {/if}
    </div>
    {#if !activeCameraSession}
      <label class="name-field">PARTICIPANT<input maxlength="60" bind:value={cameraPerson} placeholder="Required participant name" /></label>
      <button class="top-action" onclick={() => void startCameraSession()} disabled={!cameraReady || !cameraPerson.trim() || cameraBusy}>START CAMERA SESSION</button>
    {:else}
      <div class="active-session"><b>RECORDING</b><span>{activeCameraSession.person}</span><small>{activeCameraSession.id}</small></div>
      <button class="top-action stop" onclick={() => void stopCameraSession()} disabled={guidedBusy}>STOP</button>
    {/if}
    <button class="top-action ghost" onclick={() => sessionsOpen = !sessionsOpen}>SESSIONS ({cameraSessions.length})</button>
  </header>

  <div class="camera-stage">
    {#if previewUrl}
      <img class="feed" src={previewUrl} alt="Camera session live feed" onload={() => { previewFailed = false; }} onerror={() => { previewFailed = true; }} />
    {:else}
      <div class="no-feed">NO LIVE FEED{previewFailed ? ' · PREVIEW FRAME UNAVAILABLE' : ''}</div>
    {/if}

    <div class="cue-wrap">
      <div class="cue" class:count={calibrationState === 'capturing' && countdown !== null} class:big={calibrationState === 'transition'}>{cue}</div>
      <div class="cue-sub">{cueSub}</div>
    </div>

    {#if calibrationState === 'capturing' && captureState === 'capturing' && countdown === null}
      <div class="capture-progress"><i style={`width:${captureProgress}%`}></i></div>
    {/if}

    <div class="stage-controls">
      {#if activeCameraSession}
        {#if calibrationState === 'idle'}
          <button class="stage-action confirm" onclick={startCalibration} disabled={guidedBusy}>START 5-TARGET CALIBRATION</button>
        {:else if calibrationState === 'prompting' || calibrationState === 'error'}
          <button class="stage-action confirm" onclick={() => void confirmStable()} disabled={guidedBusy || calibrationState === 'error'}>CONFIRM STABLE / CAPTURE 10 S</button>
          <button class="stage-action reject" onclick={() => void rejectAndReprompt()} disabled={guidedBusy}>REJECT / RE-PROMPT</button>
          {#if calibrationState === 'error'}<button class="stage-action" onclick={startCalibration} disabled={guidedBusy}>RESTART CALIBRATION</button>{/if}
        {:else if calibrationState === 'capturing'}
          <button class="stage-action" disabled>{countdown === null ? 'CAPTURING STABLE SAMPLE...' : 'COUNTDOWN...'}</button>
        {:else if calibrationState === 'complete'}
          {#if calibrationComplete}
            <button class="stage-action confirm" onclick={randomNextTarget} disabled={guidedBusy}>RANDOM NEXT TARGET</button>
          {/if}
          <button class="stage-action" onclick={startCalibration} disabled={guidedBusy}>RESTART CALIBRATION</button>
        {/if}
      {/if}
    </div>

    {#if cameraMessage}<div class="stage-note" class:error={cameraError}>{cameraMessage}</div>{/if}

    {#if sessionsOpen}
      <aside class="sessions-panel">
        <header><span>RETAINED CAMERA SESSIONS</span><b>{cameraSessions.length}</b></header>
        <div>
          {#each cameraSessions as session (session.id)}
            <div class="session-row">
              <span><b>{session.person || 'UNKNOWN'}</b><small>{session.id}</small></span>
              {#if session.status !== 'recording'}<a href={api.cameraVideoUrl(session.id)} target="_blank" rel="noreferrer">VIDEO</a>{/if}
              <button class="text-button" onclick={() => void deleteCameraSession(session)} disabled={cameraBusy || activeCameraSession?.id === session.id}>DELETE</button>
            </div>
          {/each}
          {#if !cameraSessions.length}<p class="empty">NO RETAINED SESSIONS</p>{/if}
        </div>
      </aside>
    {/if}
  </div>
</section>

<style>
  .camera-layout {
    height: 100%;
    min-height: 0;
    display: grid;
    grid-template-rows: minmax(46px, 10%) minmax(0, 1fr);
    grid-template-areas: 'topbar' 'stage';
    gap: 7px;
  }
  .camera-topbar { grid-area: topbar; }
  .camera-stage { grid-area: stage; }

  .camera-topbar {
    display: grid;
    grid-template-columns: auto minmax(120px, 1fr) auto auto;
    align-items: center;
    gap: 10px;
    padding: 0 12px;
    border: 1px solid var(--line);
    background: #0b1215;
    overflow: hidden;
  }
  .identity { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
  .identity strong { color: #dce6e7; font: 9px DM Mono; letter-spacing: .1em; }
  .identity span { color: var(--muted); font: 8px DM Mono; letter-spacing: .08em; }
  .identity span.ready { color: var(--teal); }
  .identity span.fault { color: var(--amber); }
  .identity small { color: var(--muted); font: 7px DM Mono; letter-spacing: .05em; }
  .name-field { display: grid; grid-template-columns: auto 1fr; align-items: center; gap: 8px; color: #99aaae; font: 8px DM Mono; letter-spacing: .08em; }
  .name-field input { min-width: 0; padding: 6px 8px; border: 1px solid #2c3d44; background: #0d1418; color: #dce6e7; font: 12px DM Mono; }
  .top-action { padding: 7px 12px; border: 1px solid #745b32; background: #201a10; color: var(--amber); font: 8px DM Mono; letter-spacing: .08em; cursor: pointer; }
  .top-action:disabled { opacity: .42; cursor: not-allowed; }
  .top-action.stop { border-color: #573f38; background: #1b1412; color: #c99a82; }
  .top-action.ghost { border-color: var(--line); background: transparent; color: #aebfc2; }
  .active-session { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
  .active-session b { color: var(--teal); font: 8px DM Mono; letter-spacing: .1em; }
  .active-session span { color: #dce6e7; font: 10px DM Mono; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .active-session small { color: var(--muted); font: 7px DM Mono; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .camera-stage {
    position: relative;
    min-height: 0;
    border: 1px solid var(--line);
    background: #05090b;
    overflow: hidden;
  }
  .feed { display: block; width: 100%; height: 100%; object-fit: cover; }
  .no-feed { position: absolute; inset: 0; display: grid; place-items: center; color: var(--muted); font: 9px DM Mono; letter-spacing: .1em; }

  .cue-wrap {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    pointer-events: none;
    text-align: center;
  }
  .cue {
    padding: 4px 18px;
    background: #000000aa;
    border: 1px solid #0000;
    color: #ffffff;
    font: 700 clamp(40px, 8vw, 96px) DM Mono;
    letter-spacing: .12em;
    text-shadow: 0 2px 22px #000;
  }
  .cue.count { color: var(--amber); border-color: #745b3266; }
  .cue.big { color: var(--teal); }
  .cue-sub {
    margin-top: 10px;
    padding: 4px 10px;
    background: #00000088;
    color: #d8e2e4;
    font: 10px DM Mono;
    letter-spacing: .14em;
  }

  .capture-progress { position: absolute; left: 0; right: 0; top: 0; height: 5px; background: #1d2a2f; }
  .capture-progress i { display: block; height: 100%; background: var(--amber); transition: width .1s linear; }

  .stage-controls {
    position: absolute;
    left: 50%;
    bottom: 18px;
    transform: translateX(-50%);
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    justify-content: center;
  }
  .stage-action {
    padding: 10px 16px;
    border: 1px solid #3d615b;
    background: #0e1a18ee;
    color: var(--teal);
    font: 9px DM Mono;
    letter-spacing: .1em;
    cursor: pointer;
    box-shadow: 0 4px 18px #000a;
  }
  .stage-action:disabled { opacity: .42; cursor: not-allowed; }
  .stage-action.confirm { border-color: #33564f; }
  .stage-action.reject { border-color: #745b32; color: var(--amber); background: #1b1710ee; }

  .stage-note {
    position: absolute;
    left: 50%;
    bottom: 66px;
    transform: translateX(-50%);
    max-width: 90%;
    padding: 5px 12px;
    background: #000a;
    color: var(--muted);
    font: 8px DM Mono;
    letter-spacing: .06em;
    text-align: center;
    overflow-wrap: anywhere;
  }
  .stage-note.error { color: var(--amber); }

  .sessions-panel {
    position: absolute;
    top: 10px;
    right: 10px;
    width: min(360px, 92%);
    max-height: 72%;
    display: grid;
    grid-template-rows: 32px minmax(0, 1fr);
    border: 1px solid #33464d;
    background: #0b1215f2;
    backdrop-filter: blur(3px);
    box-shadow: 0 18px 60px #000;
  }
  .sessions-panel header { display: flex; align-items: center; justify-content: space-between; padding: 0 12px; border-bottom: 1px solid var(--line); }
  .sessions-panel header span { color: #99aaae; font: 8px DM Mono; letter-spacing: .08em; }
  .sessions-panel header b { color: var(--teal); font: 10px DM Mono; }
  .sessions-panel > div { min-height: 0; overflow-y: auto; padding: 2px 12px; }
  .session-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 7px; padding: 8px 0; border-top: 1px solid #1c282d; }
  .session-row:first-child { border-top: 0; }
  .session-row span { min-width: 0; }
  .session-row b, .session-row small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .session-row b { color: #bac8cb; font-weight: 400; font: 10px DM Mono; }
  .session-row small { margin-top: 2px; color: var(--muted); font: 8px DM Mono; }
  .session-row a { color: var(--teal); text-decoration: none; font: 8px DM Mono; letter-spacing: .08em; }
  .text-button { border: 0; background: none; color: var(--amber); font: 8px DM Mono; letter-spacing: .08em; cursor: pointer; }
  .text-button:disabled { opacity: .4; cursor: not-allowed; }
  .sessions-panel .empty { margin: 14px 0; color: var(--muted); font: 8px DM Mono; letter-spacing: .1em; }

  @media (max-width: 900px) {
    .camera-topbar { grid-template-columns: auto minmax(90px, 1fr) auto; grid-template-rows: auto auto; }
    .identity small, .active-session small { display: none; }
    .top-action.ghost { grid-column: 1 / -1; justify-self: end; }
  }
</style>
