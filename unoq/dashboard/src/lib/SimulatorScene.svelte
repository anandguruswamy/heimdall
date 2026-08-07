<script lang="ts">
  import { onMount } from 'svelte';
  import * as THREE from 'three';
  import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
  import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
  import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
  import type { HeimdallApi } from './api';
  import { LiveSeatFeed, MockSeatFeed, classSeatIds, type LiveInfo, type SeatFeed } from './simulator-feed';
  import { seatIds, type SeatId, type SeatState } from './types';

  let { feed, live, api }: { feed: SeatFeed; live: LiveSeatFeed; api: HeimdallApi } = $props();
  const mock = $derived(feed instanceof MockSeatFeed ? feed : null);

  // Car frame: nose points -Z, +Y up, driver (front_left) at -X.
  const seatDefs: { id: SeatId; short: string; x: number; z: number }[] = [
    { id: 'front_left', short: 'FL', x: -0.42, z: -0.25 },
    { id: 'front_right', short: 'FR', x: 0.42, z: -0.25 },
    { id: 'rear_left', short: 'RL', x: -0.42, z: 0.68 },
    { id: 'rear_right', short: 'RR', x: 0.42, z: 0.68 }
  ];

  let seatState = $state<SeatState | null>(null);
  let autoMode = $state(false);
  let bodySource = $state<'fallback' | 'gltf'>('fallback');
  // Camera auto-rotation; when off, the idle-resume timer stays silenced too.
  let autoRotateEnabled = $state(true);
  let container: HTMLDivElement;
  let canvas: HTMLCanvasElement;
  const labelEls: Partial<Record<SeatId, HTMLSpanElement>> = {};
  let setSeats: ((seats: Record<SeatId, boolean>) => void) | undefined;
  let resubscribeFeed: (() => void) | undefined;
  let applyAutoRotate: ((enabled: boolean) => void) | undefined;

  function toggleAutoRotate() {
    autoRotateEnabled = !autoRotateEnabled;
    applyAutoRotate?.(autoRotateEnabled);
  }

  // Live model inference: predictions arrive through the LiveSeatFeed while
  // active; the mock feed and its manual controls are overridden.
  let liveActive = $state(false);
  let liveStatus = $state<'off' | 'starting' | 'running' | 'error'>('off');
  let liveError = $state('');
  let liveNote = $state('');
  let liveModel = $state('');
  let liveRate = $state(0);
  let liveInfo = $state<LiveInfo>({ latest: null, raw: null, stablePrediction: null, stable: null, stableClass: null, person: null, uncertain: false, bits: null, mode: 'single' });
  let models = $state<{ name: string; modifiedMs: number; mode: string }[]>([]);
  let selectedModel = $state('');
  let smoothingWindow = $state(5);
  let liveMode = $state('');
  let statusTimer: ReturnType<typeof setTimeout> | undefined;
  const classIndexForSeat: Record<SeatId, number> = { front_left: 0, front_right: 1, rear_right: 2, rear_left: 3 };
  // Bit order FrontLeft, FrontRight, BackRight, BackLeft displayed as FL/FR/RR/RL.
  const bitShorts = ['FL', 'FR', 'RR', 'RL'] as const;
  const multilabelLive = $derived(liveMode === 'multilabel' || liveInfo.mode === 'multilabel');
  const multiOccupied = $derived(multilabelLive && liveInfo.bits
    ? (liveInfo.bits.occupied.map((on, index) => (on ? bitShorts[index] : null)).filter(Boolean) as string[])
    : null);

  const occupiedCount = $derived(seatState ? seatIds.filter((id) => seatState!.seats[id]).length : 0);
  const occupiedShort = $derived(seatState ? seatDefs.filter((def) => seatState!.seats[def.id]).map((def) => def.short).join('+') || 'NONE' : '—');
  const lastUpdate = $derived(seatState ? new Date(seatState.timestamp).toISOString().slice(11, 23) : '—');

  function toggleSeat(id: SeatId) {
    if (liveActive || !mock || !seatState) return;
    mock.setSeat(id, !seatState.seats[id]);
    autoMode = mock.auto;
  }

  function toggleAuto() {
    if (liveActive || !mock) return;
    mock.setAuto(!mock.auto);
    autoMode = mock.auto;
  }

  async function refreshModels() {
    try {
      const list = await api.inferenceModels();
      models = Array.isArray(list)
        ? (list as Record<string, unknown>[])
            .map((row) => ({ name: String(row.name ?? ''), modifiedMs: Number(row.modified_ms ?? 0), mode: String(row.mode ?? '') }))
            .filter((row) => row.name)
        : [];
    } catch {
      models = [];
    }
    if (!selectedModel || !models.some((model) => model.name === selectedModel)) selectedModel = models[0]?.name ?? '';
  }

  function stopStatusPolling() {
    clearTimeout(statusTimer);
    statusTimer = undefined;
  }

  function leaveLive(status: 'off' | 'error', message = '') {
    stopStatusPolling();
    liveActive = false;
    liveStatus = status;
    liveError = status === 'error' ? message || 'Live inference failed' : '';
    liveNote = status === 'off' ? message : '';
    liveMode = '';
    live.reset();
    resubscribeFeed?.();
    autoMode = mock?.auto ?? false;
  }

  async function pollStatus() {
    stopStatusPolling();
    if (!liveActive) return;
    try {
      const status = await api.inferenceStatus() as Record<string, unknown>;
      liveModel = String(status.model ?? '');
      liveRate = Number(status.rate_hz ?? 0) || 0;
      const features = status.features as Record<string, unknown> | undefined;
      smoothingWindow = Number(features?.smoothing_window ?? smoothingWindow) || smoothingWindow;
      liveMode = String(features?.mode ?? liveMode);
      const phase = String(status.status ?? '');
      if (phase === 'running' || phase === 'starting') {
        liveStatus = phase;
      } else if (phase === 'error') {
        leaveLive('error', String(status.error ?? ''));
        return;
      } else {
        // Stopped elsewhere (another client or the idle auto-stop).
        leaveLive('off', String(status.stop_reason ?? 'Live inference stopped'));
        return;
      }
    } catch { /* transient status failure; keep polling */ }
    statusTimer = setTimeout(() => void pollStatus(), 2000);
  }

  function adoptRunning(status: Record<string, unknown>) {
    liveActive = true;
    liveStatus = String(status.status) === 'running' ? 'running' : 'starting';
    liveModel = String(status.model ?? '');
    const features = status.features as Record<string, unknown> | undefined;
    smoothingWindow = Number(features?.smoothing_window ?? smoothingWindow) || smoothingWindow;
    liveMode = String(features?.mode ?? '');
    liveError = '';
    liveNote = '';
    resubscribeFeed?.();
    void pollStatus();
  }

  async function toggleLive() {
    if (liveStatus === 'starting') return;
    if (liveActive) {
      leaveLive('off');
      try { await api.stopInference(); } catch { /* already stopped on the backend */ }
      return;
    }
    liveError = '';
    liveNote = '';
    await refreshModels();
    if (!selectedModel) {
      liveStatus = 'error';
      liveError = 'No .pt checkpoints found in the models directory';
      return;
    }
    liveStatus = 'starting';
    try {
      await api.startInference(selectedModel, smoothingWindow);
      liveActive = true;
      liveModel = selectedModel;
      live.reset();
      resubscribeFeed?.();
      void pollStatus();
    } catch (error) {
      liveStatus = 'error';
      liveError = error instanceof Error ? error.message : 'Live inference could not be started';
    }
  }

  // Swap point for a real car model: drop a license-safe (CC0 / CC-BY) Tesla
  // Model Y GLB at public/models/tesla-model-y.glb and record its author and
  // license in unoq/dashboard/README.md. When the file is absent or fails to
  // parse, the stylized primitive body below renders instead.
  const MODEL_URL = '/models/tesla-model-y.glb';
  const MODEL_YAW = Math.PI; // glTF assets face +Z; this scene's car frame points the nose at -Z.
  const CAR_LENGTH = 4.6;
  const CUTAWAY_Y = 1.15; // GLB geometry above this height is clipped so the cabin stays visible.

  onMount(() => {
    autoMode = mock?.auto ?? false;
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.localClippingEnabled = true;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0b1115);
    scene.fog = new THREE.Fog(0x0b1115, 10, 18);

    const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 60);
    camera.position.set(-2.9, 2.8, -3.7);

    const controls = new OrbitControls(camera, canvas);
    controls.target.set(0, 0.55, 0.15);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.autoRotate = autoRotateEnabled;
    controls.autoRotateSpeed = 0.5;
    controls.minDistance = 2.6;
    controls.maxDistance = 9;
    controls.minPolarAngle = 0.12;
    controls.maxPolarAngle = 1.38;
    let resumeTimer: ReturnType<typeof setTimeout> | undefined;
    controls.addEventListener('start', () => { controls.autoRotate = false; clearTimeout(resumeTimer); });
    controls.addEventListener('end', () => { clearTimeout(resumeTimer); resumeTimer = setTimeout(() => { controls.autoRotate = autoRotateEnabled; }, 6000); });
    applyAutoRotate = (enabled) => { clearTimeout(resumeTimer); controls.autoRotate = enabled; };

    scene.add(new THREE.HemisphereLight(0x9fb8c4, 0x0a0e12, 0.55));
    const key = new THREE.DirectionalLight(0xeef4ff, 2.4);
    key.position.set(4, 6.5, -3);
    key.castShadow = true;
    key.shadow.mapSize.set(2048, 2048);
    key.shadow.camera.left = key.shadow.camera.bottom = -4.5;
    key.shadow.camera.right = key.shadow.camera.top = 4.5;
    key.shadow.bias = -0.0004;
    scene.add(key);
    const rim = new THREE.DirectionalLight(0x45e0c1, 0.5);
    rim.position.set(-3.5, 2.4, 4);
    scene.add(rim);
    const fill = new THREE.DirectionalLight(0x8fa3b0, 0.35);
    fill.position.set(2.5, 1.6, 3.5);
    scene.add(fill);

    const ground = new THREE.Mesh(new THREE.CircleGeometry(6, 48), new THREE.MeshStandardMaterial({ color: 0x0d1418, roughness: 0.95 }));
    ground.rotation.x = -Math.PI / 2;
    ground.receiveShadow = true;
    scene.add(ground);
    const halo = new THREE.Mesh(new THREE.RingGeometry(2.95, 3.0, 64), new THREE.MeshBasicMaterial({ color: 0x1d3a37, transparent: true, opacity: 0.9 }));
    halo.rotation.x = -Math.PI / 2;
    halo.position.y = 0.004;
    scene.add(halo);

    const paint = new THREE.MeshStandardMaterial({ color: 0x6f7d88, metalness: 0.85, roughness: 0.38 });
    const trim = new THREE.MeshStandardMaterial({ color: 0x14181c, metalness: 0.4, roughness: 0.6 });
    const glass = new THREE.MeshPhysicalMaterial({ color: 0x2b444c, metalness: 0, roughness: 0.08, transparent: true, opacity: 0.16, side: THREE.DoubleSide, depthWrite: false });
    const cabin = new THREE.MeshStandardMaterial({ color: 0x1a2025, roughness: 0.9 });
    const tire = new THREE.MeshStandardMaterial({ color: 0x101317, roughness: 0.95 });
    const rimMetal = new THREE.MeshStandardMaterial({ color: 0x39434c, metalness: 0.8, roughness: 0.3 });

    const box = (material: THREE.Material, w: number, h: number, d: number, x: number, y: number, z: number, radius = 0.06, shadow = true) => {
      const mesh = new THREE.Mesh(new RoundedBoxGeometry(w, h, d, 3, Math.min(radius, w / 2, h / 2, d / 2)), material);
      mesh.position.set(x, y, z);
      mesh.castShadow = shadow;
      return mesh;
    };

    function buildFallbackBody(): THREE.Group {
      const body = new THREE.Group();
      body.add(box(trim, 1.78, 0.28, 4.35, 0, 0.38, 0, 0.08));
      body.add(box(paint, 1.86, 0.5, 4.55, 0, 0.72, 0, 0.16));
      const hood = box(paint, 1.68, 0.24, 1.15, 0, 0.94, -1.68, 0.1);
      hood.rotation.x = 0.13;
      body.add(hood);
      const hatch = box(paint, 1.68, 0.22, 1.2, 0, 0.97, 1.62, 0.1);
      hatch.rotation.x = -0.2;
      body.add(hatch);
      const canopy = box(glass, 1.62, 0.62, 2.95, 0, 1.28, 0.15, 0.24, false);
      body.add(canopy);
      for (const side of [-1, 1]) {
        body.add(box(trim, 0.05, 0.5, 0.07, side * 0.8, 1.22, 0.22, 0.02));
        body.add(box(trim, 0.16, 0.05, 0.24, side * 0.99, 1.02, -0.78, 0.02));
        for (const end of [-1, 1]) {
          const wheel = new THREE.Mesh(new THREE.CylinderGeometry(0.37, 0.37, 0.26, 28), tire);
          wheel.rotation.z = Math.PI / 2;
          wheel.position.set(side * 0.82, 0.37, end * 1.45);
          wheel.castShadow = true;
          body.add(wheel);
          const hub = new THREE.Mesh(new THREE.CylinderGeometry(0.2, 0.2, 0.27, 20), rimMetal);
          hub.rotation.z = Math.PI / 2;
          hub.position.copy(wheel.position);
          body.add(hub);
        }
      }
      const headBar = new THREE.Mesh(new THREE.BoxGeometry(1.5, 0.05, 0.04), new THREE.MeshStandardMaterial({ color: 0x30393f, emissive: 0xcfe8ff, emissiveIntensity: 1.2 }));
      headBar.position.set(0, 0.9, -2.29);
      body.add(headBar);
      const tailBar = new THREE.Mesh(new THREE.BoxGeometry(1.62, 0.05, 0.04), new THREE.MeshStandardMaterial({ color: 0x30393f, emissive: 0xff5544, emissiveIntensity: 0.8 }));
      tailBar.position.set(0, 0.96, 2.29);
      body.add(tailBar);
      return body;
    }

    function buildInterior(): THREE.Group {
      const interior = new THREE.Group();
      interior.add(box(cabin, 1.6, 0.06, 3.05, 0, 0.5, 0.2, 0.02, false));
      interior.add(box(cabin, 1.6, 0.32, 0.42, 0, 0.9, -1.05, 0.08));
      const screen = new THREE.Mesh(new THREE.BoxGeometry(0.56, 0.34, 0.03), new THREE.MeshStandardMaterial({ color: 0x0a1412, emissive: 0x2ea18a, emissiveIntensity: 1 }));
      screen.position.set(0, 1.04, -0.83);
      screen.rotation.x = -0.12;
      interior.add(screen);
      interior.add(box(cabin, 0.32, 0.2, 1.05, 0, 0.62, 0.05, 0.06));
      const wheelRing = new THREE.Mesh(new THREE.TorusGeometry(0.18, 0.025, 12, 32), cabin);
      wheelRing.position.set(-0.42, 0.98, -0.76);
      wheelRing.rotation.x = -1.2;
      interior.add(wheelRing);
      return interior;
    }

    const easeOutBack = (k: number) => 1 + 2.70158 * Math.pow(k - 1, 3) + 1.70158 * Math.pow(k - 1, 2);

    type SeatRuntime = {
      occupant: THREE.Group;
      seatMaterial: THREE.MeshStandardMaterial;
      occupantMaterials: THREE.MeshStandardMaterial[];
      labelAnchor: THREE.Vector3;
      presence: number;
      target: number;
      highlight: number;
    };
    const runtimes = new Map<SeatId, SeatRuntime>();

    const capsule = (material: THREE.Material, r: number, len: number, x: number, y: number, z: number, rx = 0) => {
      const mesh = new THREE.Mesh(new THREE.CapsuleGeometry(r, len, 4, 12), material);
      mesh.position.set(x, y, z);
      mesh.rotation.x = rx;
      mesh.castShadow = true;
      return mesh;
    };

    function buildOccupant(): { group: THREE.Group; materials: THREE.MeshStandardMaterial[] } {
      const material = new THREE.MeshStandardMaterial({ color: 0xb9c6cc, roughness: 0.55, emissive: 0x45e0c1, emissiveIntensity: 0.06 });
      const group = new THREE.Group();
      group.add(capsule(material, 0.16, 0.4, 0, 0.42, 0.06, 0.12));
      const head = new THREE.Mesh(new THREE.SphereGeometry(0.12, 20, 16), material);
      head.position.set(0, 0.82, 0);
      head.castShadow = true;
      group.add(head);
      for (const side of [-1, 1]) {
        group.add(capsule(material, 0.085, 0.28, side * 0.13, 0.1, -0.2, Math.PI / 2));
        group.add(capsule(material, 0.07, 0.3, side * 0.13, -0.14, -0.36));
        group.add(capsule(material, 0.055, 0.3, side * 0.26, 0.38, -0.08, 0.7));
      }
      return { group, materials: [material] };
    }

    const interiorGroup = new THREE.Group();
    const furniture = buildInterior();
    interiorGroup.add(furniture);
    for (const def of seatDefs) {
      const seatMaterial = new THREE.MeshStandardMaterial({ color: 0x232a30, roughness: 0.9, emissive: 0x45e0c1, emissiveIntensity: 0 });
      const seat = new THREE.Group();
      seat.position.set(def.x, 0, def.z);
      seat.add(box(seatMaterial, 0.55, 0.13, 0.52, 0, 0.6, 0, 0.05));
      const backrest = box(seatMaterial, 0.55, 0.6, 0.13, 0, 0.92, 0.28, 0.05);
      backrest.rotation.x = 0.16;
      seat.add(backrest);
      seat.add(box(seatMaterial, 0.3, 0.16, 0.1, 0, 1.3, 0.36, 0.04));
      const { group: occupant, materials } = buildOccupant();
      occupant.position.set(0, 0.66, -0.02);
      occupant.visible = false;
      seat.add(occupant);
      interiorGroup.add(seat);
      runtimes.set(def.id, { occupant, seatMaterial, occupantMaterials: materials, labelAnchor: new THREE.Vector3(def.x, 1.72, def.z), presence: 0, target: 0, highlight: 0 });
    }

    let fallbackBody: THREE.Group | null = buildFallbackBody();
    const carGroup = new THREE.Group();
    carGroup.add(fallbackBody, interiorGroup);
    scene.add(carGroup);

    let disposed = false;
    void new GLTFLoader().loadAsync(MODEL_URL).then((gltf) => {
      if (disposed) return;
      const model = gltf.scene;
      model.rotation.y = MODEL_YAW;
      const bounds = new THREE.Box3().setFromObject(model);
      const size = bounds.getSize(new THREE.Vector3());
      const scale = CAR_LENGTH / Math.max(size.x, size.z, 0.01);
      model.scale.setScalar(scale);
      bounds.setFromObject(model);
      const center = bounds.getCenter(new THREE.Vector3());
      model.position.set(-center.x, -bounds.min.y, -center.z);
      const cutaway = new THREE.Plane(new THREE.Vector3(0, -1, 0), CUTAWAY_Y);
      model.traverse((child) => {
        if (child instanceof THREE.Mesh) {
          child.castShadow = true;
          child.receiveShadow = true;
          for (const material of Array.isArray(child.material) ? child.material : [child.material]) material.clippingPlanes = [cutaway];
        }
      });
      if (fallbackBody) { carGroup.remove(fallbackBody); disposeObject(fallbackBody); fallbackBody = null; }
      furniture.visible = false;
      carGroup.add(model);
      bodySource = 'gltf';
    }).catch(() => { /* fallback body stays */ });

    setSeats = (seats) => {
      for (const def of seatDefs) {
        const runtime = runtimes.get(def.id)!;
        const target = seats[def.id] ? 1 : 0;
        if (target === runtime.target) continue;
        runtime.target = target;
        runtime.highlight = 1;
        if (target === 1) runtime.occupant.visible = true;
      }
    };

    const projected = new THREE.Vector3();
    const clock = new THREE.Clock();
    let raf = 0;
    const step = () => {
      raf = requestAnimationFrame(step);
      const dt = Math.min(clock.getDelta(), 0.1);
      controls.update();
      for (const runtime of runtimes.values()) {
        if (runtime.presence !== runtime.target) {
          runtime.presence = runtime.target === 1 ? Math.min(1, runtime.presence + dt / 0.35) : Math.max(0, runtime.presence - dt / 0.35);
          const k = runtime.presence;
          runtime.occupant.scale.setScalar(Math.max(0.01, 0.4 + 0.6 * easeOutBack(k)));
          for (const material of runtime.occupantMaterials) { material.transparent = true; material.opacity = k; }
          if (k >= 1) { for (const material of runtime.occupantMaterials) { material.transparent = false; material.opacity = 1; } }
          else if (k <= 0) runtime.occupant.visible = false;
        }
        if (runtime.highlight > 0) {
          runtime.highlight = Math.max(0, runtime.highlight - dt / 0.9);
          runtime.seatMaterial.emissiveIntensity = runtime.highlight * 0.85;
        }
      }
      const width = container.clientWidth, height = container.clientHeight;
      for (const def of seatDefs) {
        const label = labelEls[def.id];
        if (!label) continue;
        projected.copy(runtimes.get(def.id)!.labelAnchor).applyMatrix4(carGroup.matrixWorld).project(camera);
        const visible = Boolean(label.textContent?.trim()) && projected.z < 1 && Math.abs(projected.x) < 1.1 && Math.abs(projected.y) < 1.1;
        label.style.display = visible ? '' : 'none';
        if (visible) { label.style.left = `${(projected.x * 0.5 + 0.5) * width}px`; label.style.top = `${(0.5 - projected.y * 0.5) * height}px`; }
      }
      renderer.render(scene, camera);
    };

    const start = () => { if (!raf) { clock.getDelta(); raf = requestAnimationFrame(step); } };
    const stop = () => { cancelAnimationFrame(raf); raf = 0; };
    let unsubscribe: (() => void) | undefined;
    // The active source is decided at (re)subscribe time so the scene code
    // itself stays agnostic of mock-versus-live mode.
    const currentFeed = () => (liveActive ? live : feed);
    const subscribeFeed = () => { unsubscribe ??= currentFeed().subscribe((state) => { seatState = state; setSeats?.(state.seats); }); };
    const pauseFeed = () => { unsubscribe?.(); unsubscribe = undefined; };
    resubscribeFeed = () => { pauseFeed(); subscribeFeed(); };
    const offInfo = live.onInfo((info) => { liveInfo = info; });
    const onVisibility = () => { if (document.hidden) { stop(); pauseFeed(); } else { subscribeFeed(); start(); } };
    document.addEventListener('visibilitychange', onVisibility);

    const resize = new ResizeObserver(() => {
      const width = Math.max(1, container.clientWidth), height = Math.max(1, container.clientHeight);
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    });
    resize.observe(container);

    function disposeObject(root: THREE.Object3D) {
      root.traverse((child) => {
        if (child instanceof THREE.Mesh) {
          child.geometry.dispose();
          for (const material of Array.isArray(child.material) ? child.material : [child.material]) {
            for (const value of Object.values(material)) if (value instanceof THREE.Texture) value.dispose();
            material.dispose();
          }
        }
      });
    }

    subscribeFeed();
    start();
    void refreshModels();
    // Adopt a run that is already active on the backend (page reload or a
    // quick tab switch while the idle grace period was still counting down).
    void api.inferenceStatus().then((status) => {
      const phase = String((status as Record<string, unknown>).status ?? '');
      if (!disposed && (phase === 'running' || phase === 'starting')) adoptRunning(status as Record<string, unknown>);
    }).catch(() => { /* backend offline; stay in mock mode */ });

    return () => {
      disposed = true;
      pauseFeed();
      stop();
      stopStatusPolling();
      offInfo();
      clearTimeout(resumeTimer);
      document.removeEventListener('visibilitychange', onVisibility);
      resize.disconnect();
      controls.dispose();
      disposeObject(scene);
      ground.geometry.dispose();
      key.shadow.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      setSeats = undefined;
      resubscribeFeed = undefined;
      applyAutoRotate = undefined;
    };
  });
</script>

<section class="sim-layout">
  <div class="sim-controls panel">
    <header><span>PRESENCE CONTROLS</span><b>{liveActive ? 'LIVE MODEL' : 'MOCK FEED'}</b></header>
    <div class="controls-body">
      <button class="auto live-toggle" class:running={liveActive} onclick={() => void toggleLive()} disabled={liveStatus === 'starting'}>
        {liveStatus === 'starting' ? 'LIVE INFERENCE · STARTING…' : liveActive ? 'LIVE INFERENCE · STOP' : 'LIVE INFERENCE · START'}
      </button>
      <label class="model-pick">MODEL
        <select bind:value={selectedModel} disabled={liveActive || liveStatus === 'starting' || !models.length}>
          {#if !models.length}<option value="">NO CHECKPOINTS FOUND</option>{/if}
          {#each models as model (model.name)}<option value={model.name}>{model.name}{model.mode ? ` · ${model.mode.toUpperCase()}` : ''}</option>{/each}
        </select>
      </label>
      <label class="model-pick smoothing-window">COMMON MAJORITY WINDOW <output>{smoothingWindow} snapshot{smoothingWindow === 1 ? '' : 's'}</output>
        <input type="range" min="1" max="300" step="1" bind:value={smoothingWindow} disabled={liveActive || liveStatus === 'starting'} aria-label="Common person and position majority window" />
      </label>
      {#if liveActive}
        <dl class="status live-readout">
          <dt>STATE</dt><dd class:uncertain={liveInfo.uncertain}>{liveStatus === 'running' ? (liveInfo.uncertain ? 'RUNNING · UNCERTAIN' : 'RUNNING') : 'STARTING'}</dd>
          <dt>MODE</dt><dd>{multilabelLive ? 'MULTI-PERSON' : 'SINGLE-PERSON'}</dd>
          <dt>MODEL</dt><dd class="wrap">{liveModel || selectedModel}</dd>
          <dt>RATE</dt><dd>{liveRate.toFixed(1)} Hz</dd>
          <dt>PREDICTED</dt><dd>{multiOccupied ? (multiOccupied.length ? `${multiOccupied.join('+')} · ${multiOccupied.length}/4` : 'EMPTY CABIN') : liveInfo.latest?.seat.replace(/(?!^)([A-Z])/g, ' $1').toUpperCase() ?? (liveInfo.person ? 'PERSON ONLY' : '—')}</dd>
          {#if liveInfo.person}<dt>PERSON</dt><dd>{liveInfo.person}</dd>{/if}
        </dl>
        {#if multilabelLive && liveInfo.bits}
          {@const bits = liveInfo.bits}
          <div class="confidence">
            {#each seatDefs as def (def.id)}
              {@const index = classIndexForSeat[def.id]}
              {@const prob = bits.probs[index] ?? 0}
              <div class="bar" class:top={bits.occupied[index]} class:uncertain={bits.uncertainBits[index]}>
                <span>{def.short}</span>
                <i><b style={`width:${Math.min(100, Math.round(prob * 100))}%`}></b></i>
                <small>{Math.round(prob * 100)}%</small>
              </div>
            {/each}
          </div>
          <p class="note">INDEPENDENT PER-SEAT PROBABILITIES · DO NOT SUM TO 100% · FILLED = OVER THRESHOLD ({Math.round(bits.threshold * 100)}%)</p>
        {:else if liveInfo.latest}
          <div class="confidence">
            {#each seatDefs as def (def.id)}
              {@const prob = liveInfo.latest.probs[classIndexForSeat[def.id]] ?? 0}
              <div class="bar" class:top={liveInfo.stable === def.id}>
                <span>{def.short}</span>
                <i><b style={`width:${Math.min(100, Math.round(prob * 100))}%`}></b></i>
                <small>{Math.round(prob * 100)}%</small>
              </div>
            {/each}
          </div>
        {/if}
      {/if}
      {#if liveStatus === 'error'}<p class="note error-note">{liveError}</p>{/if}
      {#if liveNote}<p class="note">{liveNote}</p>{/if}
      {#if multilabelLive}
        <p class="note">LIVE MODEL · MULTI-PERSON · four independent seat detectors; any combination including an empty cabin is possible.</p>
      {:else}
        <p class="note">LIVE MODEL · SINGLE OCCUPANT OR EMPTY · backend stable predictions are used directly; raw-only output falls back to local majority smoothing.</p>
      {/if}

      <div class="test-block" class:overridden={liveActive}>
        <p class="section-label">TEST CONTROLS{liveActive ? ' · OVERRIDDEN BY LIVE' : ''}</p>
        {#if mock}
          <button class="auto" class:active={autoMode && !liveActive} disabled={liveActive} onclick={toggleAuto}>{autoMode ? 'AUTO / RANDOM · ON' : 'AUTO / RANDOM · OFF'}</button>
          <div class="seat-toggles">
            {#each seatDefs as def (def.id)}
              <button class:active={!liveActive && seatState?.seats[def.id]} disabled={liveActive} onclick={() => toggleSeat(def.id)}>
                <span>{def.short}</span>
                <small>{def.id.replace('_', ' ').toUpperCase()}</small>
                <b>{seatState?.seats[def.id] ? 'OCCUPIED' : 'EMPTY'}</b>
              </button>
            {/each}
          </div>
          <p class="note">Toggling a seat switches Auto off. Any combination of seats can be occupied at once.</p>
        {:else}
          <p class="note">External seat feed connected · manual test controls are available with the mock feed only.</p>
        {/if}
      </div>
      <button class="auto" class:active={autoRotateEnabled} onclick={toggleAutoRotate}>{autoRotateEnabled ? 'CAMERA AUTO-ROTATE · ON' : 'CAMERA AUTO-ROTATE · OFF'}</button>
      <dl class="status">
        <dt>LAST UPDATE</dt><dd>{lastUpdate}</dd>
        <dt>OCCUPIED</dt><dd>{occupiedShort} · {occupiedCount}/4</dd>
        <dt>CAR BODY</dt><dd>{bodySource === 'gltf' ? 'GLB MODEL' : 'FALLBACK PRIMITIVES'}</dd>
      </dl>
      <p class="note">A license-safe (CC0/CC-BY) Model Y GLB is attempted from /models/tesla-model-y.glb; its attribution belongs in the dashboard README. Until one is provided, the stylized fallback body renders.</p>
    </div>
  </div>
  <article class="scene-panel panel">
    <header><span>CABIN OCCUPANCY</span><b>{occupiedCount}/4 OCCUPIED</b></header>
    <div class="scene" bind:this={container}>
      <canvas bind:this={canvas} aria-label="3D cabin occupancy view"></canvas>
      <div class="labels">
        {#each seatDefs as def (def.id)}
          <span bind:this={labelEls[def.id]} class:occupied={seatState?.seats[def.id]}>{seatState?.seats[def.id] ? seatState.people?.[def.id] ?? '' : ''}</span>
        {/each}
      </div>
      {#if liveActive && !liveInfo.latest && liveInfo.person}<div class="person-only-label">{liveInfo.person}</div>{/if}
      <div class="hint">DRAG ORBIT · WHEEL ZOOM{autoRotateEnabled ? ' · AUTO-ROTATES WHEN IDLE' : ''}</div>
    </div>
  </article>
</section>

<style>
  .sim-layout{display:grid;grid-template-columns:250px minmax(0,1fr);gap:7px;min-height:0}
  .sim-controls{display:grid;grid-template-rows:36px minmax(0,1fr)}
  .controls-body{padding:10px;overflow-y:auto}
  .auto{width:100%;border:1px solid #304147;background:#0b1215;color:#9fb0b4;padding:8px;font:9px DM Mono,monospace;letter-spacing:.08em}
  .auto.active{background:#45e0c1;border-color:#45e0c1;color:#07110f}
  .seat-toggles{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:10px 0}
  .seat-toggles button{display:grid;gap:3px;border:1px solid #26373d;background:#0b1215;color:#718188;padding:9px 8px;text-align:left;font:9px DM Mono,monospace}
  .seat-toggles button span{color:#dbe5e7;font-size:12px}
  .seat-toggles button small{font-size:7px;letter-spacing:.08em}
  .seat-toggles button b{color:#61757b;font-weight:400;font-size:8px}
  .seat-toggles button.active{border-color:#45e0c1;background:#0d211e}
  .seat-toggles button.active span,.seat-toggles button.active b{color:#45e0c1}
  .status{display:grid;grid-template-columns:auto 1fr;gap:6px 10px;margin:12px 0;padding:10px;border:1px solid #26373d;font:8px DM Mono,monospace}
  .status dt{color:#718188}
  .status dd{margin:0;text-align:right;color:#dbe5e7}
  .note{margin:8px 0 0;color:#61757b;font:8px DM Mono,monospace;line-height:1.5}
  .live-toggle.running{background:#f4bd6222;border-color:#745b32;color:#f4bd62}
  .live-toggle:disabled{opacity:.6;cursor:wait}
  .model-pick{display:block;margin:10px 0 0;color:#99aaae;font:9px DM Mono,monospace;letter-spacing:.08em}
  .model-pick select{display:block;width:100%;margin-top:5px;font:10px DM Mono,monospace}
  .smoothing-window output{float:right;color:#45e0c1}
  .smoothing-window input{display:block;width:100%;margin-top:8px;accent-color:#45e0c1}
  .live-readout dd.uncertain{color:#f4bd62}
  .live-readout dd.wrap{overflow-wrap:anywhere}
  .confidence{display:grid;gap:5px;margin:10px 0}
  .confidence .bar{display:grid;grid-template-columns:22px minmax(0,1fr) 32px;align-items:center;gap:7px;font:8px DM Mono,monospace;color:#718188}
  .confidence .bar i{display:block;height:6px;background:#0b1215;border:1px solid #26373d}
  .confidence .bar b{display:block;height:100%;background:#2c4a45;transition:width .15s linear}
  .confidence .bar small{text-align:right}
  .confidence .bar.top{color:#45e0c1}
  .confidence .bar.top b{background:#45e0c1;box-shadow:0 0 7px #45e0c144}
  .confidence .bar.uncertain{color:#f4bd62}
  .confidence .bar.uncertain b{background:#f4bd62;box-shadow:0 0 7px #f4bd6244}
  .error-note{color:#f4bd62}
  .section-label{margin:14px 0 8px;padding-top:10px;border-top:1px solid #1c282d;color:#718188;font:8px DM Mono,monospace;letter-spacing:.1em}
  .test-block.overridden{opacity:.45}
  .test-block.overridden .section-label{color:#f4bd62;opacity:1}
  .test-block button:disabled{cursor:not-allowed}
  .scene-panel{display:grid;grid-template-rows:36px minmax(0,1fr)}
  .scene{position:relative;min-height:0;overflow:hidden;background:#0b1115}
  canvas{display:block;width:100%;height:100%;cursor:grab;touch-action:none}
  .labels{position:absolute;inset:0;pointer-events:none}
  .labels span{position:absolute;transform:translate(-50%,-100%);padding:2px 5px;background:#071014c9;color:#7d8d93;font:9px DM Mono,monospace;border:1px solid #31434a}
  .labels span.occupied{color:#45e0c1;border-color:#31524f;box-shadow:0 0 9px #45e0c133}
  .person-only-label{position:absolute;left:50%;top:48%;transform:translate(-50%,-50%);padding:7px 12px;border:1px solid #31524f;background:#071014d9;color:#45e0c1;box-shadow:0 0 16px #45e0c133;font:12px DM Mono,monospace;letter-spacing:.06em;pointer-events:none}
  .hint{position:absolute;left:8px;bottom:7px;color:#61757b;font:8px DM Mono,monospace}
  @media(max-width:900px){.sim-layout{grid-template-columns:1fr;grid-template-rows:minmax(0,1.4fr) auto}.sim-layout{grid-template-areas:'scene' 'controls'}.scene-panel{grid-area:scene}.sim-controls{grid-area:controls;max-height:40vh}}
</style>
