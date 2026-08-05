<script lang="ts">
  import { onMount } from 'svelte';
  import type { MapPoint, Vec3 } from './map/map-engine';

  let {
    points,
    nodes,
    valueRange,
    pointSize = 3,
    fixedBounds,
  }: {
    points: MapPoint[];
    nodes: Vec3[];
    valueRange: [number, number];
    pointSize?: number;
    fixedBounds?: { min: Vec3; max: Vec3 } | null;
  } = $props();

  let canvas: HTMLCanvasElement;
  let labels = $state<{ id: number; x: number; y: number; visible: boolean }[]>([]);
  let yaw = 0;
  let pitch = -0.65;
  let distance = 3.2;
  let bounds = $state.raw({
    min: { x: -1, y: -1, z: -1 },
    max: { x: 1, y: 1, z: 1 },
  });
  let gl: WebGL2RenderingContext | null = null;
  let program: WebGLProgram | null = null;
  let buffer: WebGLBuffer | null = null;
  let rebuildRequested = false;
  let requestRender: (() => void) | undefined;

  const color = (hex: string) => [0, 1, 2].map((i) => parseInt(hex.slice(1 + i * 2, 3 + i * 2), 16) / 255);

  function normalizedPosition(point: { x: number; y: number; z: number }): [number, number, number] {
    const b = bounds;
    const spans = [b.max.x - b.min.x, b.max.y - b.min.y, b.max.z - b.min.z];
    const scale = Math.max(...spans) || 1;
    return [
      (point.x - (b.min.x + b.max.x) / 2) * 2 / scale,
      (point.y - (b.min.y + b.max.y) / 2) * 2 / scale,
      (point.z - (b.min.z + b.max.z) / 2) * 2 / scale,
    ];
  }

  function computeBounds() {
    if (fixedBounds) {
      bounds = fixedBounds;
      return;
    }
    let min = { x: Infinity, y: Infinity, z: Infinity };
    let max = { x: -Infinity, y: -Infinity, z: -Infinity };
    for (const point of points) {
      min.x = Math.min(min.x, point.x); max.x = Math.max(max.x, point.x);
      min.y = Math.min(min.y, point.y); max.y = Math.max(max.y, point.y);
      min.z = Math.min(min.z, point.z); max.z = Math.max(max.z, point.z);
    }
    for (const node of nodes) {
      min.x = Math.min(min.x, node.x); max.x = Math.max(max.x, node.x);
      min.y = Math.min(min.y, node.y); max.y = Math.max(max.y, node.y);
      min.z = Math.min(min.z, node.z); max.z = Math.max(max.z, node.z);
    }
    if (!Number.isFinite(min.x)) { min = { x: -1, y: -1, z: -1 }; max = { x: 1, y: 1, z: 1 }; }
    bounds = { min, max };
  }

  function normalized(value: number): number {
    const [low, high] = valueRange;
    if (high <= low) return 0;
    return Math.sqrt(Math.max(0, Math.min(1, (value - low) / (high - low))));
  }

  function rebuildGeometry() {
    if (!gl || !program) return;
    const vertices: number[] = [];
    for (const point of points) {
      vertices.push(...normalizedPosition(point), normalized(point.magnitude), 0);
    }
    for (const node of nodes) {
      vertices.push(...normalizedPosition(node), 1, 1);
    }
    const b = bounds;
    const corners = [
      [b.min.x, b.min.y, b.min.z], [b.max.x, b.min.y, b.min.z], [b.max.x, b.max.y, b.min.z], [b.min.x, b.max.y, b.min.z],
      [b.min.x, b.min.y, b.max.z], [b.max.x, b.min.y, b.max.z], [b.max.x, b.max.y, b.max.z], [b.min.x, b.max.y, b.max.z],
    ].map((point) => normalizedPosition({ x: point[0], y: point[1], z: point[2] }));
    const edges = [[0,1],[1,2],[2,3],[3,0],[4,5],[5,6],[6,7],[7,4],[0,4],[1,5],[2,6],[3,7]];
    for (const [a, c] of edges) {
      vertices.push(...corners[a], 0, 2, ...corners[c], 0, 2);
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(vertices), gl.STATIC_DRAW);
    const stride = 5 * Float32Array.BYTES_PER_ELEMENT;
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 3, gl.FLOAT, false, stride, 0);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 1, gl.FLOAT, false, stride, 3 * 4);
    gl.enableVertexAttribArray(2);
    gl.vertexAttribPointer(2, 1, gl.FLOAT, false, stride, 4 * 4);
  }

  function render() {
    if (!gl || !program) return;
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.floor(canvas.clientWidth * dpr));
    const height = Math.max(1, Math.floor(canvas.clientHeight * dpr));
    if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
    gl.viewport(0, 0, width, height);
    gl.clearColor(0.025, 0.055, 0.068, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.useProgram(program);
    for (const [name, value] of [['uYaw', yaw], ['uPitch', pitch], ['uDistance', distance], ['uAspect', width / height], ['uPointSize', pointSize]] as const) {
      gl.uniform1f(gl.getUniformLocation(program, name), value);
    }
    gl.drawArrays(gl.POINTS, 0, points.length);
    gl.drawArrays(gl.LINES, points.length + nodes.length, 24);
    gl.disable(gl.DEPTH_TEST);
    gl.drawArrays(gl.POINTS, points.length, nodes.length);
    gl.enable(gl.DEPTH_TEST);
    positionNodeLabels(width / dpr, height / dpr);
  }

  function positionNodeLabels(width: number, height: number) {
    const aspect = width / height, f = 1.75;
    const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
    labels = nodes.map((node, id) => {
      const [x, y, z] = normalizedPosition(node);
      const x1 = cy * x + sy * y;
      const y1 = -sy * x + cy * y;
      const y2 = cp * y1 - sp * z;
      const z2 = sp * y1 + cp * z - distance;
      const visible = -z2 > 0.1;
      if (!visible) return { id, x: 0, y: 0, visible: false };
      const ndcX = (x1 * f / aspect) / (-z2);
      const ndcY = (y2 * f) / (-z2);
      return { id, x: (ndcX * 0.5 + 0.5) * width, y: (1 - (ndcY * 0.5 + 0.5)) * height, visible: true };
    });
  }

  function scheduleRebuild() {
    rebuildRequested = true;
    requestRender?.();
  }

  onMount(() => {
    gl = canvas.getContext('webgl2', { antialias: true, alpha: false });
    if (!gl) return;
    const shader = (type: number, source: string) => {
      const value = gl!.createShader(type)!;
      gl!.shaderSource(value, source);
      gl!.compileShader(value);
      if (!gl!.getShaderParameter(value, gl!.COMPILE_STATUS)) throw new Error(gl!.getShaderInfoLog(value) ?? 'shader compile failed');
      return value;
    };
    program = gl.createProgram()!;
    gl.attachShader(program, shader(gl.VERTEX_SHADER, `#version 300 es
      layout(location=0) in vec3 aPosition;
      layout(location=1) in float aIntensity;
      layout(location=2) in float aKind;
      uniform float uYaw;
      uniform float uPitch;
      uniform float uDistance;
      uniform float uAspect;
      uniform float uPointSize;
      out float vIntensity;
      out float vKind;
      void main() {
        float cy = cos(uYaw), sy = sin(uYaw), cp = cos(uPitch), sp = sin(uPitch);
        vec3 rot = vec3(cy*aPosition.x + sy*aPosition.y, -sy*aPosition.x + cy*aPosition.y, aPosition.z);
        vec3 pitched = vec3(rot.x, cp*rot.y - sp*rot.z, sp*rot.y + cp*rot.z);
        pitched.z -= uDistance;
        float near = 0.1, far = 20.0, f = 1.75;
        gl_Position = vec4(pitched.x*f/uAspect, pitched.y*f, ((far+near)/(near-far))*pitched.z + (2.0*far*near)/(near-far), -pitched.z);
        gl_PointSize = aKind > 0.5 ? 15.0 : uPointSize * (1.0 + 1.8*aIntensity);
        vIntensity = aIntensity;
        vKind = aKind;
      }
    `));
    gl.attachShader(program, shader(gl.FRAGMENT_SHADER, `#version 300 es
      precision mediump float;
      in float vIntensity;
      in float vKind;
      out vec4 outColor;
      vec3 color(float t) {
        vec3 a = vec3(0.075,0.25,0.32), b = vec3(0.32,0.9,0.84), c = vec3(1.0,0.74,0.35);
        return t < 0.65 ? mix(a,b,t/0.65) : mix(b,c,(t-0.65)/0.35);
      }
      void main() {
        if (vKind < 0.5) {
          vec2 d = gl_PointCoord - vec2(0.5);
          if (dot(d,d) > 0.25) discard;
          outColor = vec4(color(vIntensity), 1.0);
        } else if (vKind < 1.5) {
          vec2 d = gl_PointCoord - vec2(0.5);
          if (dot(d,d) > 0.25) discard;
          outColor = vec4(0.38,0.91,0.88,1.0);
        } else {
          outColor = vec4(0.28,0.39,0.43,0.7);
        }
      }
    `));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program) ?? 'program link failed');
    buffer = gl.createBuffer();
    gl.enable(gl.DEPTH_TEST);

    computeBounds();
    rebuildRequested = true;

    let raf = 0;
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(() => {
        raf = 0;
        if (rebuildRequested) { rebuildGeometry(); rebuildRequested = false; }
        render();
      });
    };
    requestRender = schedule;
    schedule();
    const resize = new ResizeObserver(schedule);
    resize.observe(canvas);

    let dragging = false, lastX = 0, lastY = 0;
    const down = (e: PointerEvent) => { dragging = true; lastX = e.clientX; lastY = e.clientY; canvas.setPointerCapture(e.pointerId); };
    const move = (e: PointerEvent) => {
      if (!dragging) return;
      yaw += (e.clientX - lastX) * 0.008;
      pitch = Math.max(-Math.PI / 2, Math.min(Math.PI / 2, pitch + (e.clientY - lastY) * 0.008));
      lastX = e.clientX; lastY = e.clientY;
      schedule();
    };
    const up = () => dragging = false;
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      distance = Math.max(1.8, Math.min(7, distance * Math.exp(e.deltaY * 0.001)));
      schedule();
    };
    canvas.addEventListener('pointerdown', down);
    canvas.addEventListener('pointermove', move);
    canvas.addEventListener('pointerup', up);
    canvas.addEventListener('pointercancel', up);
    canvas.addEventListener('wheel', wheel, { passive: false });
    return () => {
      requestRender = undefined;
      cancelAnimationFrame(raf);
      resize.disconnect();
      gl!.deleteBuffer(buffer);
      gl!.deleteProgram(program);
    };
  });

  $effect(() => {
    void points; void nodes; void valueRange; void pointSize; void fixedBounds;
    computeBounds();
    scheduleRebuild();
  });

  function resetView() { yaw = 0; pitch = -0.65; distance = 3.2; requestRender?.(); }
  function topView() { yaw = 0; pitch = -Math.PI / 2; distance = 3.2; requestRender?.(); }
</script>

<div class="scene">
  <canvas bind:this={canvas} aria-label="Interactive 3D radar point cloud"></canvas>
  <div class="labels">
    {#each labels as item}
      {#if item.visible}<span style={`left:${item.x}px;top:${item.y}px`}>N{item.id}</span>{/if}
    {/each}
    <span class="axis axis-x">X</span>
    <span class="axis axis-y">Y</span>
    <span class="axis axis-z">Z</span>
  </div>
  <div class="hint">DRAG ORBIT · WHEEL ZOOM</div>
  <div class="view-buttons">
    <button onclick={topView}>TOP +Z</button>
    <button onclick={resetView}>RESET 3D</button>
  </div>
</div>

<style>
  .scene { position: relative; width: 100%; height: 100%; min-height: 0; overflow: hidden; background: #0b1115; }
  canvas { display: block; width: 100%; height: 100%; cursor: grab; touch-action: none; }
  .labels { position: absolute; inset: 0; pointer-events: none; }
  .labels span { position: absolute; transform: translate(-50%, -150%); padding: 2px 4px; background: #071014c9; color: #dbe5e7; font: 9px DM Mono, monospace; border: 1px solid #31524f; }
  .labels span.axis { transform: translate(-50%, -50%); font-weight: 700; border-radius: 3px; }
  .labels span.axis-x { color: #f4bd62; border-color: #6b5732; left: 12px; top: 12px; }
  .labels span.axis-y { color: #45e0c1; border-color: #2c6a5c; left: 34px; top: 12px; }
  .labels span.axis-z { color: #b995ff; border-color: #6c5a96; left: 56px; top: 12px; }
  .hint { position: absolute; left: 8px; bottom: 7px; color: #61757b; font: 8px DM Mono, monospace; }
  .view-buttons { position: absolute; right: 8px; bottom: 7px; display: flex; gap: 5px; }
  .view-buttons button { border: 1px solid #385056; background: #0b1215; color: #9fb0b4; padding: 5px 7px; font: 8px DM Mono, monospace; cursor: pointer; }
</style>
