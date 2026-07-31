<script lang="ts">
  import { onMount } from 'svelte';
  import type { PlotFrame } from './types';

  let { frame, revision = 0, label = 'Signal plot', animate = true, webgl = false, axes = 'none' }: { frame: () => PlotFrame; revision?: number; label?: string; animate?: boolean; webgl?: boolean; axes?: 'none'|'bounds'|'full' } = $props();
  let canvas: HTMLCanvasElement;
  let current = $state<PlotFrame>({});

  const ticks = (min: number, max: number) => Array.from({ length: 5 }, (_, index) => max - index * (max - min) / 4);
  const xTicks = (value: PlotFrame) => { const count=Math.max(2,...(value.series ?? []).map((series)=>series.data.length)); return Array.from({length:5},(_,index)=>Math.round(index*(count-1)/4)); };
  const tickLabel = (value: number) => Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(1);

  onMount(() => {
    const gl = webgl ? canvas.getContext('webgl2', { alpha: false, antialias: true }) : null;
    const ctx = gl ? null : canvas.getContext('2d');
    let raf = 0;
    let last = 0;
    let program: WebGLProgram | null = null;
    let buffer: WebGLBuffer | null = null;
    let position = -1;
    let color = -1;
    let pointSize: WebGLUniformLocation | null = null;
    let lastFrame: PlotFrame | null = null;
    let cssWidth = canvas.clientWidth;
    let cssHeight = canvas.clientHeight;
    let axisSignature = '';
    let sizeDirty = true;
    const resize = new ResizeObserver(([entry]) => {
      cssWidth = entry.contentRect.width;
      cssHeight = entry.contentRect.height;
      sizeDirty = true;
    });
    resize.observe(canvas);
    const contextLost = (event: Event) => { event.preventDefault(); lastFrame = null; };
    const contextRestored = () => location.reload();
    if (gl) {
      canvas.addEventListener('webglcontextlost', contextLost);
      canvas.addEventListener('webglcontextrestored', contextRestored);
    }

    if (gl) {
      const compile = (type: number, source: string) => {
        const shader = gl.createShader(type)!;
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        return shader;
      };
      program = gl.createProgram()!;
      gl.attachShader(program, compile(gl.VERTEX_SHADER, `#version 300 es\nin vec2 p;in vec3 c;out vec3 vc;uniform float size;void main(){gl_Position=vec4(p,0.,1.);gl_PointSize=size;vc=c;}`));
      gl.attachShader(program, compile(gl.FRAGMENT_SHADER, `#version 300 es\nprecision mediump float;in vec3 vc;out vec4 o;void main(){o=vec4(vc,1.);}`));
      gl.linkProgram(program);
      position = gl.getAttribLocation(program, 'p');
      color = gl.getAttribLocation(program, 'c');
      pointSize = gl.getUniformLocation(program, 'size');
      buffer = gl.createBuffer();
    }

    const size = () => {
      if (!sizeDirty) return false;
      sizeDirty = false;
      const dpr = Math.min(devicePixelRatio, 2);
      const w = Math.max(1, Math.floor(cssWidth * dpr));
      const h = Math.max(1, Math.floor(cssHeight * dpr));
      if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; return true; }
      return false;
    };
    const rgb = (hex: string) => [1, 1, 1].map((_, i) => parseInt(hex.slice(1 + i * 2, 3 + i * 2), 16) / 255);
    const heatColor = (value: number): [number, number, number] => { const v=Math.max(0,Math.min(1,value)); return [Math.min(255, v * 420), Math.min(255, Math.max(0, v - 0.18) * 360), Math.min(255, (1 - v) * 115 + v * 50)]; };

    const draw2d = (f: PlotFrame) => {
      if (!ctx) return;
      const { width: w, height: h } = canvas;
      ctx.fillStyle = '#0b1115'; ctx.fillRect(0, 0, w, h);
      ctx.strokeStyle = '#1d2a30'; ctx.lineWidth = 1;
      for (let i = 1; i < 5; i++) { ctx.beginPath(); ctx.moveTo(0, h * i / 5); ctx.lineTo(w, h * i / 5); ctx.stroke(); }
      if (f.heatmap && f.heatWidth && f.heatHeight) {
        const image = ctx.createImageData(f.heatWidth, f.heatHeight);
        f.heatmap.forEach((value, i) => { const normalized=(value-(f.min??0))/Math.max(Number.EPSILON,(f.max??1)-(f.min??0)); const c = heatColor(normalized); image.data.set([c[0], c[1], c[2], 255], i * 4); });
        const temp = document.createElement('canvas'); temp.width = f.heatWidth; temp.height = f.heatHeight;
        temp.getContext('2d')!.putImageData(image, 0, 0); ctx.imageSmoothingEnabled = false; ctx.drawImage(temp, 0, 0, w, h);
      }
      for (const s of f.series ?? []) {
        ctx.strokeStyle = s.color; ctx.fillStyle = s.color; ctx.lineWidth = (s.width ?? 1) * devicePixelRatio;
        if (s.points) {
          const size = 5 * devicePixelRatio;
          for (let i = 0; i < s.data.length; i++) { const v = s.data[i]; if (!Number.isFinite(v)) continue; const x = (s.data.length < 2 ? 0.5 : i / (s.data.length - 1)) * w; const y = h - (v - (f.min ?? 0)) / Math.max(Number.EPSILON,(f.max ?? 1) - (f.min ?? 0)) * h; ctx.fillRect(x - size / 2, y - size / 2, size, size); }
        } else {
          ctx.beginPath();
          let drawing = false;
          s.data.forEach((v, i) => { if (!Number.isFinite(v)) { drawing=false; return; } const x = i / Math.max(1,s.data.length - 1) * w; const y = h - (v - (f.min ?? 0)) / Math.max(Number.EPSILON,(f.max ?? 1) - (f.min ?? 0)) * h; if (drawing) ctx.lineTo(x, y); else ctx.moveTo(x, y); drawing=true; });
          ctx.stroke();
        }
      }
      for (const marker of f.markers ?? []) { ctx.strokeStyle=marker.color; ctx.setLineDash([4*devicePixelRatio,3*devicePixelRatio]); ctx.beginPath(); ctx.moveTo(marker.at*w,0); ctx.lineTo(marker.at*w,h); ctx.stroke(); ctx.setLineDash([]); }
    };
    const drawGl = (f: PlotFrame) => {
      if (!gl || !program || !buffer) return;
      gl.viewport(0, 0, canvas.width, canvas.height); gl.clearColor(0.043, 0.067, 0.082, 1); gl.clear(gl.COLOR_BUFFER_BIT);
      gl.useProgram(program); gl.bindBuffer(gl.ARRAY_BUFFER, buffer); gl.enableVertexAttribArray(position); gl.enableVertexAttribArray(color); gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 20, 0); gl.vertexAttribPointer(color, 3, gl.FLOAT, false, 20, 8);
      if (f.heatmap && f.heatWidth && f.heatHeight) {
        const vertices = new Float32Array(f.heatmap.length * 5);
        for (let i = 0; i < f.heatmap.length; i++) { const x=i%f.heatWidth,y=Math.floor(i/f.heatWidth),normalized=(f.heatmap[i]-(f.min??0))/Math.max(Number.EPSILON,(f.max??1)-(f.min??0)),c=heatColor(normalized); vertices.set([(x+.5)/f.heatWidth*2-1,1-(y+.5)/f.heatHeight*2,c[0]/255,c[1]/255,c[2]/255],i*5); }
        gl.bufferData(gl.ARRAY_BUFFER,vertices,gl.STREAM_DRAW); gl.uniform1f(pointSize, Math.max(canvas.width/f.heatWidth,canvas.height/f.heatHeight)+1); gl.drawArrays(gl.POINTS,0,f.heatmap.length);
      }
      for (const s of f.series ?? []) {
        const c=rgb(s.color),vertex=(value:number,index:number)=>[index/Math.max(1,s.data.length-1)*2-1,(value-(f.min??0))/Math.max(Number.EPSILON,(f.max??1)-(f.min??0))*2-1,...c];
        gl.uniform1f(pointSize, s.points ? 8*devicePixelRatio : 4*devicePixelRatio);
        if (s.points) {
          const vertices=Array.from(s.data).flatMap((value,index)=>Number.isFinite(value)?vertex(value,index):[]);
          gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(vertices),gl.STREAM_DRAW);gl.drawArrays(gl.POINTS,0,vertices.length/5);
        } else {
          let vertices:number[]=[];
          const flush=()=>{if(vertices.length>=10){gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(vertices),gl.STREAM_DRAW);gl.drawArrays(gl.LINE_STRIP,0,vertices.length/5)}vertices=[]};
          s.data.forEach((value,index)=>{if(Number.isFinite(value))vertices.push(...vertex(value,index));else flush()});flush();
        }
      }
      for (const marker of f.markers ?? []) { const x=marker.at*2-1,c=rgb(marker.color),v=new Float32Array([x,-1,...c,x,1,...c]); gl.bufferData(gl.ARRAY_BUFFER,v,gl.STREAM_DRAW); gl.uniform1f(pointSize,1); gl.drawArrays(gl.LINES,0,2); }
    };
    const loop = (now: number) => {
      if (now - last >= 33 || !animate) {
        const resized = size();
         void revision;
         const value = frame();
         const nextAxisSignature = axes === 'none' ? '' : `${value.min}|${value.max}|${value.xLabel}|${value.yLabel}|${Math.max(0,...(value.series ?? []).map((series)=>series.data.length))}`;
         if (nextAxisSignature !== axisSignature) { axisSignature = nextAxisSignature; current = value; }
        if (!document.hidden && (resized || value !== lastFrame)) {
          gl ? drawGl(value) : draw2d(value);
          lastFrame = value;
        }
        last = now;
      }
      if (animate) raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => { cancelAnimationFrame(raf); resize.disconnect(); canvas.removeEventListener('webglcontextlost',contextLost); canvas.removeEventListener('webglcontextrestored',contextRestored); if (gl) { if (buffer) gl.deleteBuffer(buffer); if (program) gl.deleteProgram(program); } };
  });
</script>

<div class="plot" class:bound-axes={axes === 'bounds'} class:full-axes={axes === 'full'}>
  <canvas bind:this={canvas} aria-label={label}></canvas>
  {#if axes === 'bounds' && current.min !== undefined && current.max !== undefined}
    <div class="y-bounds"><span>{tickLabel(current.max)} {current.yLabel ?? ''}</span><span>{tickLabel(current.min)} {current.yLabel ?? ''}</span></div>
  {:else if axes === 'full' && current.min !== undefined && current.max !== undefined}
    <div class="y-ticks">{#each ticks(current.min, current.max) as tick, index}<span style={`top:${index * 25}%`}>{tickLabel(tick)}</span>{/each}</div>
    <div class="x-ticks">{#each xTicks(current) as tick, index}<span style={`left:${index * 25}%`}>{tick}</span>{/each}</div>
  {/if}
  <span class="x-axis">{current.xLabel ?? ''}</span><span class="y-axis">{current.yLabel ?? ''}</span>
</div>

<style>
  .plot { position: relative; width: 100%; height: 100%; min-height: 0; }
  canvas { display: block; width: 100%; height: 100%; min-height: 0; }
  span { position: absolute; pointer-events: none; color: #718188; font: 8px ui-monospace, monospace; text-transform: uppercase; letter-spacing: .06em; text-shadow: 0 1px 2px #000; }
  .x-axis { right: 6px; bottom: 4px; }
  .y-axis { left: 5px; top: 4px; }
  .y-bounds { position: absolute; inset: 4px auto 4px 4px; display: flex; flex-direction: column; justify-content: space-between; pointer-events: none; }
  .y-bounds span { position: static; color: #a3b3b7; background: #071014b8; padding: 1px 3px; text-shadow: none; }
  .bound-axes .y-axis { display: none; }
  .full-axes canvas { width: calc(100% - 62px); height: calc(100% - 38px); margin-left: 56px; }
  .y-ticks { position: absolute; left: 0; top: 0; bottom: 38px; width: 51px; pointer-events: none; }
  .y-ticks span { left: 0; width: 43px; text-align: right; transform: translateY(-50%); color: #a3b3b7; }
  .y-ticks span:first-child { transform: none; }.y-ticks span:last-child { transform: translateY(-100%); }
  .x-ticks { position: absolute; left: 56px; right: 6px; bottom: 19px; height: 12px; pointer-events: none; }
  .x-ticks span { top: 0; transform: translateX(-50%); color: #a3b3b7; }
  .x-ticks span:first-child { transform: none; }.x-ticks span:last-child { transform: translateX(-100%); }
  .full-axes .x-axis { left: 56px; right: 6px; bottom: 2px; text-align: center; }
  .full-axes .y-axis { left: 4px; top: 50%; transform: rotate(-90deg) translateX(-50%); transform-origin: left top; }
</style>
