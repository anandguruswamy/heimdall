import type { Link, PlotFrame, Tab } from './types';

export function linksFor(count: number): Link[] {
  const links: Link[] = [];
  for (let from = 0; from < count; from++) {
    for (let to = 0; to < count; to++) {
      if (from !== to) links.push({ from, to, id: `${from}>${to}` });
    }
  }
  return links;
}

function signal(length: number, phase: number, style: 'line' | 'cir' | 'fft' | 'cfo'): Float32Array {
  const out = new Float32Array(length);
  for (let i = 0; i < length; i++) {
    const x = i / (length - 1);
    if (style === 'cir') {
      const peak = Math.exp(-Math.pow((x - 0.32 - Math.sin(phase) * 0.025) * 26, 2));
      const echo = 0.56 * Math.exp(-Math.pow((x - 0.52) * 18, 2));
      out[i] = 0.03 * Math.sin(i * 1.7 + phase) + peak + echo;
    } else if (style === 'fft') {
      out[i] = 0.08 + 0.7 * Math.exp(-Math.pow((x - 0.44) * 23, 2)) + 0.17 * Math.sin(i * 0.22 + phase);
    } else if (style === 'cfo') {
      out[i] = 0.5 + 0.22 * Math.sin(i * 0.07 + phase) + 0.08 * Math.sin(i * 0.31);
    } else {
      out[i] = 0.48 + 0.16 * Math.sin(i * 0.055 + phase) + 0.035 * Math.sin(i * 0.62 + phase);
    }
  }
  return out;
}

function smooth(data: Float32Array, factor = 0.08): Float32Array {
  const out = new Float32Array(data.length);
  out[0] = data[0];
  for (let i = 1; i < data.length; i++) out[i] = out[i - 1] + factor * (data[i] - out[i - 1]);
  return out;
}

function heat(width: number, height: number, phase: number): Float32Array {
  const out = new Float32Array(width * height);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const ridge = 0.6 * Math.exp(-Math.pow((x / width - 0.32 - 0.08 * Math.sin(y * 0.12 + phase)) * 18, 2));
      const echo = 0.3 * Math.exp(-Math.pow((x / width - 0.63) * 12, 2));
      out[y * width + x] = Math.max(0, ridge + echo + 0.08 * Math.sin(x * 0.3 + y * 0.2));
    }
  }
  return out;
}

export function demoFrame(tab: Tab, link: Link, time: number, phaseMode = false): PlotFrame {
  const p = time / 900 + link.from * 0.7 + link.to * 0.31;
  if (tab === 'CIR Waterfall' || tab === 'Slow-Time FFT') {
    return { heatmap: heat(96, 48, p), heatWidth: 96, heatHeight: 48, min: 0, max: 1 };
  }
  if (tab === 'Live CIR') {
    const raw = signal(64, p, 'cir');
    const curve = signal(1024, p, 'cir');
    return { series: [{ data: curve, color: '#45e0c1' }, { data: raw, color: '#f4bd62', points: true }], min: -0.1, max: 1.15 };
  }
  if (tab === 'Fast-Time FFT') {
    const fft = signal(256, p, 'fft');
    const phase = new Float32Array(256);
    for (let i = 0; i < phase.length; i++) phase[i] = 0.5 + 0.42 * Math.sin(i * 0.12 + p);
    return { series: [{ data: phaseMode ? phase : fft, color: phaseMode ? '#b995ff' : '#45e0c1' }], min: 0, max: 1 };
  }
  if (tab === 'CFO') {
    const raw = signal(180, p, 'cfo');
    return { series: [{ data: raw, color: '#f4bd62' }, { data: smooth(raw, 0.04), color: '#45e0c1', width: 2 }], min: 0.1, max: 0.9 };
  }
  const ss = signal(180, p, 'line');
  const ds = signal(180, p + 0.35, 'line');
  return { series: [{ data: ss, color: '#f4bd62' }, { data: smooth(ss), color: '#45e0c1', width: 2 }, { data: ds, color: '#b995ff' }], min: 0.2, max: 0.8 };
}
