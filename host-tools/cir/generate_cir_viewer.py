#!/usr/bin/env python3
"""Align captured complex CIR windows and generate a standalone scrubber."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path


def sinc(x: float) -> float:
    if abs(x) < 1.0e-12:
        return 1.0
    return math.sin(math.pi * x) / (math.pi * x)


def bessel_i0(x: float) -> float:
    total = 1.0
    term = 1.0
    for k in range(1, 32):
        term *= (x * x) / (4.0 * k * k)
        total += term
        if abs(term) < 1.0e-14 * abs(total):
            break
    return total


def resample_complex(samples: list[complex], x: float, half_width: int = 10) -> complex:
    """MATLAB-resample-style windowed-sinc interpolation at original index x.

    This is a 16-phase FIR interpolator in the browser (the Python peak search
    uses the same continuous kernel). The Kaiser window limits the finite FIR;
    normalization preserves DC gain and integer samples remain exact.
    """
    beta = 8.6
    denom = bessel_i0(beta)
    start = max(0, math.floor(x) - half_width)
    stop = min(len(samples) - 1, math.floor(x) + half_width)
    value = 0j
    weight_sum = 0.0
    for i in range(start, stop + 1):
        t = x - i
        u = abs(t) / half_width
        if u > 1.0:
            continue
        window = bessel_i0(beta * math.sqrt(max(0.0, 1.0 - u * u))) / denom
        weight = sinc(t) * window
        value += samples[i] * weight
        weight_sum += weight
    return value / weight_sum if abs(weight_sum) > 1.0e-12 else 0j


def peak_position(samples: list[complex]) -> float:
    coarse = max(range(len(samples)), key=lambda i: abs(samples[i]))
    lo = max(0.0, coarse - 2.0)
    hi = min(float(len(samples) - 1), coarse + 2.0)
    best_x = float(coarse)
    best_mag = abs(samples[coarse])
    # Dense search is deterministic and avoids assuming a particular channel shape.
    for step in range(1, 161):
        x = lo + (hi - lo) * step / 160.0
        mag = abs(resample_complex(samples, x))
        if mag > best_mag:
            best_x, best_mag = x, mag
    return best_x


def fft(values: list[complex], inverse: bool = False) -> list[complex]:
    n = len(values)
    if n == 0 or n & (n - 1):
        raise ValueError("FFT length must be a non-zero power of two")
    a, j = list(values), 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    sign = 1.0 if inverse else -1.0
    while length <= n:
        half = length // 2
        twiddle = complex(math.cos(2 * math.pi / length), sign * math.sin(2 * math.pi / length))
        for start in range(0, n, length):
            w = 1 + 0j
            for i in range(start, start + half):
                u, v = a[i], a[i + half] * w
                a[i], a[i + half] = u + v, u - v
                w *= twiddle
        length *= 2
    return [v / n for v in a] if inverse else a


def estimate_lag(reference: list[complex], current: list[complex]) -> float:
    nfft = 256
    ref_fft = fft(reference + [0j] * (nfft - len(reference)))
    cur_fft = fft(current + [0j] * (nfft - len(current)))
    corr = fft([cur_fft[i] * ref_fft[i].conjugate() for i in range(nfft)], inverse=True)
    index = max(range(nfft), key=lambda i: abs(corr[i]))
    lag = float(index if index <= nfft // 2 else index - nfft)
    # Refine the FFT peak on the same 16x FIR interpolator used for display.
    best_lag, best_score = lag, -1.0
    for step in range(-16, 17):
        candidate = lag + step / 16.0
        cross = sum(reference[n].conjugate() * resample_complex(current, n + candidate) for n in range(len(reference)))
        score = abs(cross)
        if score > best_score:
            best_lag, best_score = candidate, score
    return round(best_lag * 16.0) / 16.0


def fractional_shift(samples: list[complex], delay: float) -> list[complex]:
    """Apply y[n] = x[n-delay] via FFT upsampling and a 1/16-sample shift."""
    n = len(samples)
    factor = 16
    spectrum = fft(samples)
    up_n = n * factor
    up_spectrum = [0j] * up_n
    up_spectrum[: n // 2] = spectrum[: n // 2]
    up_spectrum[-n // 2 :] = spectrum[n // 2 :]
    upsampled = [value * factor for value in fft(up_spectrum, inverse=True)]
    integer_shift = round(delay * factor)
    shifted = [0j] * up_n
    for index, value in enumerate(upsampled):
        target = index + integer_shift
        if 0 <= target < up_n:
            shifted[target] = value
    return shifted[::factor][:n]


def phase_offset(reference: list[complex], current: list[complex]) -> float:
    cross = sum(ref.conjugate() * sample * abs(ref) * abs(sample) for ref, sample in zip(reference, current))
    return math.atan2(cross.imag, cross.real)


def read_capture(path: Path) -> list[dict]:
    with path.open(newline="") as stream:
        rows = csv.DictReader(stream)
        i_fields = sorted(
            (name for name in rows.fieldnames or [] if name.startswith("i") and name[1:].isdigit()),
            key=lambda name: int(name[1:]),
        )
        if not i_fields:
            raise ValueError(f"{path}: no i0/i1/... CIR fields found")
        result = []
        for row in rows:
            samples = [complex(float(row[f"i{i}"]), float(row[f"q{i}"])) for i in range(len(i_fields))]
            peak = peak_position(samples)
            peak_amplitude = max(abs(resample_complex(samples, peak)), 1.0e-12)
            marker_db = [20.0 * math.log10(max(abs(sample), 1.0e-12)) for sample in samples]
            result.append(
                {
                    "source": path.name,
                    "seq": int(row.get("seq", len(result))),
                    "rx_ts": int(row.get("rx_ts", 0)),
                    "fp": int(row.get("fp", 0)),
                    "peak_sample": max(range(len(samples)), key=lambda i: abs(samples[i])),
                    "peak_position": peak,
                    "peak_amplitude": peak_amplitude,
                    "markers": [
                        {"index": i, "x": i - peak, "db": marker_db[i]}
                        for i in range(len(samples))
                    ],
                    "samples": [{"i": sample.real, "q": sample.imag} for sample in samples],
                }
            )
    return result


def align_frames(frames: list[dict]) -> list[dict]:
    if not frames:
        return frames
    reference = [complex(s["i"], s["q"]) for s in frames[0]["samples"]]
    reference_peak = peak_position(reference)
    for index, frame in enumerate(frames):
        original = [complex(s["i"], s["q"]) for s in frame["samples"]]
        delay = 0.0 if index == 0 else estimate_lag(reference, original)
        aligned = fractional_shift(original, -delay)
        phase = 0.0 if index == 0 else phase_offset(reference, aligned)
        aligned = [sample * complex(math.cos(-phase), math.sin(-phase)) for sample in aligned]
        frame["time_delay"] = delay
        frame["phase_rad"] = phase
        frame["reference_peak"] = reference_peak
        frame["peak_position"] = peak_position(aligned)
        frame["peak_sample"] = max(range(len(aligned)), key=lambda i: abs(aligned[i]))
        frame["samples"] = [{"i": sample.real, "q": sample.imag} for sample in aligned]
        frame["markers"] = [{"index": i, "x": i - reference_peak, "db": 20.0 * math.log10(max(abs(sample), 1.0e-12))} for i, sample in enumerate(aligned)]
    return frames


def write_aligned_csv(path: Path, frames: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["source", "seq", "rx_ts", "fp", "time_delay_samples", "phase_offset_rad", "peak_sample", "peak_position", "sample", "aligned_x", "i", "q", "magnitude_db"])
        for frame in frames:
            for marker in frame["markers"]:
                writer.writerow(
                    [
                        frame["source"],
                        frame["seq"],
                        frame["rx_ts"],
                        frame["fp"],
                        f'{frame["time_delay"]:.6f}',
                        f'{frame["phase_rad"]:.8f}',
                        frame["peak_sample"],
                        f'{frame["peak_position"]:.6f}',
                        marker["index"],
                        f'{marker["x"]:.6f}',
                        f'{frame["samples"][marker["index"]]["i"]:.6f}',
                        f'{frame["samples"][marker["index"]]["q"]:.6f}',
                        f'{marker["db"]:.4f}',
                    ]
                )


def build_waterfalls(frames: list[dict], reference_peak: float) -> dict:
    x_min, x_max, oversample = -20.0, 50.0, 16
    xs = [x_min + i / oversample for i in range(round((x_max - x_min) * oversample) + 1)]
    waterfall = []
    fft_waterfall = []
    for frame in frames:
        samples = [complex(s["i"], s["q"]) for s in frame["samples"]]
        waterfall.append([20.0 * math.log10(max(abs(resample_complex(samples, x + reference_peak)), 1.0e-12)) for x in xs])
    # Slow-time FFT: one row per aligned tap, transforming across frames.
    fft_n = 1
    while fft_n < len(frames) * 16:
        fft_n *= 2
    for tap in range(len(frames[0]["samples"])):
        slow_time = [complex(frame["samples"][tap]["i"], frame["samples"][tap]["q"]) for frame in frames]
        spectrum = fft(slow_time + [0j] * (fft_n - len(slow_time)))
        fft_waterfall.append([20.0 * math.log10(max(abs(spectrum[(i + fft_n // 2) % fft_n]), 1.0e-12)) for i in range(fft_n)])
    timestamp_deltas = []
    for source in sorted({frame["source"] for frame in frames}):
        source_frames = sorted((frame for frame in frames if frame["source"] == source), key=lambda frame: frame["seq"])
        for previous, current in zip(source_frames, source_frames[1:]):
            delta = (current["rx_ts"] - previous["rx_ts"]) & ((1 << 40) - 1)
            if delta:
                timestamp_deltas.append(delta)
    dtu_per_second = 63_897_600_000.0
    collection_interval_seconds = statistics.median(timestamp_deltas) / dtu_per_second if timestamp_deltas else 1.0
    frame_rate_hz = 1.0 / collection_interval_seconds
    return {"reference_peak": reference_peak, "x_min": x_min, "x_max": x_max, "xs": xs, "frames": frames, "waterfall": waterfall, "fft_waterfall": fft_waterfall, "frame_rate_hz": frame_rate_hz, "collection_interval_seconds": collection_interval_seconds}


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aligned CIR scrubber</title>
<style>
:root { color-scheme: light dark; --bg:#10151b; --fg:#e8eef5; --muted:#9eacba; --grid:#34424f; --line:#55b7ff; --mark:#ffb454; --panel:#18212a; }
* { box-sizing:border-box; }
body { margin:0; padding:14px; background:var(--bg); color:var(--fg); font:15px/1.35 system-ui,sans-serif; }
main { width:min(900px,100%); margin:auto; }
h1 { font-size:1.15rem; font-weight:500; margin:0 0 10px; }
.toolbar { display:grid; grid-template-columns:auto 1fr auto; gap:8px; align-items:center; margin-bottom:8px; }
button { border:1px solid var(--grid); background:var(--panel); color:var(--fg); border-radius:6px; padding:7px 11px; font:inherit; }
button:focus-visible, input:focus-visible { outline:2px solid var(--line); outline-offset:2px; }
input[type=range] { width:100%; accent-color:var(--line); }
#frameLabel { color:var(--muted); white-space:nowrap; text-align:right; }
.meta { display:flex; flex-wrap:wrap; gap:6px 14px; color:var(--muted); font-size:.82rem; margin:2px 0 8px; }
.plot { width:100%; aspect-ratio:1.65; min-height:280px; }
svg { display:block; width:100%; height:100%; overflow:visible; }
.grid { stroke:var(--grid); stroke-width:.8; }
.axis { stroke:var(--muted); stroke-width:1; }
.curve { fill:none; stroke:var(--line); stroke-width:2.2; stroke-linejoin:round; stroke-linecap:round; }
.marker { fill:var(--mark); stroke:var(--bg); stroke-width:1; }
text { fill:var(--muted); font-size:11px; }
.legend { color:var(--muted); font-size:.8rem; margin-top:6px; }
.plot-title { color:var(--muted); font-size:.82rem; margin:12px 0 4px; }
.heat { width:100%; height:auto; display:block; image-rendering:auto; }
@media (max-width:480px) { body { padding:10px; } .toolbar { grid-template-columns:auto 1fr; } #frameLabel { grid-column:1/-1; text-align:left; } .plot { aspect-ratio:.95; min-height:360px; } }
</style>
</head>
<body>
<main>
<h1>Aligned complex CIR magnitude</h1>
<div class="toolbar">
  <button id="prev" type="button" aria-label="Previous CIR">‹</button>
  <input id="scrub" type="range" min="0" max="0" value="0" aria-label="CIR frame">
  <button id="next" type="button" aria-label="Next CIR">›</button>
  <div id="frameLabel">Frame 1/1</div>
</div>
<div class="meta"><span id="source"></span><span id="peak"></span><span id="rx"></span></div>
<div class="plot"><svg id="chart" viewBox="0 0 820 500" role="img" aria-label="Unnormalized CIR magnitude in raw-unit decibels, aligned to interpolated peak at x equals zero"></svg></div>
<div class="plot-title">Interpolated aligned CIR magnitude waterfall</div>
<canvas id="waterfall" class="heat" width="900" height="360" role="img" aria-label="CIR magnitude waterfall over frame and aligned sample index"></canvas>
<div class="plot-title">Across-frame FFT by aligned tap (frame rate <span id="frameRate"></span>)</div>
<canvas id="fftWaterfall" class="heat" width="900" height="360" role="img" aria-label="FFT magnitude across frames for each aligned CIR tap"></canvas>
<div class="legend">Frames are aligned to frame 0 by FFT cross-correlation, FFT fractional delay, and an all-tap common-phase estimate. Waterfalls use raw dB magnitude with no per-frame normalization.</div>
<div class="legend">Orange markers are the captured samples. Blue line is 16× windowed-FIR resampling (MATLAB <code>resample(x,16,1)</code>-style) through those samples. dB is raw accumulator magnitude: 20·log10(|I+jQ|), with no per-frame normalization.</div>
</main>
<script>
const data = __DATA__;
const frames = data.frames;
const scrub = document.getElementById('scrub');
const chart = document.getElementById('chart');
const label = document.getElementById('frameLabel');
const source = document.getElementById('source');
const peak = document.getElementById('peak');
const rx = document.getElementById('rx');
const frameRate = document.getElementById('frameRate');
scrub.max = Math.max(0, frames.length - 1);
function sinc(x) { return Math.abs(x) < 1e-10 ? 1 : Math.sin(Math.PI*x)/(Math.PI*x); }
function besselI0(x) { let total=1, term=1; for(let k=1;k<32;k++){term*=x*x/(4*k*k);total+=term;if(Math.abs(term)<1e-14*Math.abs(total))break;} return total; }
function resampleKernel(frame, x) {
  const halfWidth=10, beta=8.6, denom=besselI0(beta); let iSum=0,qSum=0,wSum=0;
  const lo=Math.max(0,Math.floor(x)-halfWidth), hi=Math.min(frame.samples.length-1,Math.floor(x)+halfWidth);
  for(let n=lo;n<=hi;n++){const t=x-n,u=Math.abs(t)/halfWidth;if(u>1)continue;const w=sinc(t)*besselI0(beta*Math.sqrt(Math.max(0,1-u*u)))/denom;iSum+=frame.samples[n].i*w;qSum+=frame.samples[n].q*w;wSum+=w;}
  return wSum ? Math.hypot(iSum/wSum,qSum/wSum) : 0;
}
function interp(frame, x) {
  return resampleKernel(frame, x);
}
function line(parent, x1,y1,x2,y2, cls) { const e=document.createElementNS('http://www.w3.org/2000/svg','line'); e.setAttribute('x1',x1);e.setAttribute('y1',y1);e.setAttribute('x2',x2);e.setAttribute('y2',y2);e.setAttribute('class',cls);parent.appendChild(e); }
function text(parent, x,y,value,anchor='middle') { const e=document.createElementNS('http://www.w3.org/2000/svg','text');e.setAttribute('x',x);e.setAttribute('y',y);e.setAttribute('text-anchor',anchor);e.textContent=value;parent.appendChild(e); }
function circle(parent,cx,cy,r) { const e=document.createElementNS('http://www.w3.org/2000/svg','circle');e.setAttribute('cx',cx);e.setAttribute('cy',cy);e.setAttribute('r',r);e.setAttribute('class','marker');parent.appendChild(e); }
function path(parent, points) { const e=document.createElementNS('http://www.w3.org/2000/svg','path');e.setAttribute('d',points.map((p,n)=>(n?'L':'M')+p[0].toFixed(2)+' '+p[1].toFixed(2)).join(' '));e.setAttribute('class','curve');parent.appendChild(e); }
function render(n) {
  n=Math.max(0,Math.min(frames.length-1,n)); scrub.value=n;
  const f=frames[n], W=820,H=500, left=54,right=14,top=18,bottom=42, pw=W-left-right,ph=H-top-bottom;
  const x0=-20, x1=50, y0=-10,y1=70;
  const sx=x=>left+(x-x0)/(x1-x0)*pw, sy=y=>top+(y1-y)/(y1-y0)*ph;
  chart.replaceChildren();
  for(let y=y0;y<=y1;y+=10){line(chart,left,sy(y),W-right,sy(y),'grid');text(chart,left-8,sy(y)+4,String(y),'end');}
  const xticks=[Math.ceil(x0/10)*10]; while(xticks[xticks.length-1]<x1) xticks.push(xticks[xticks.length-1]+10);
  xticks.forEach(x=>{line(chart,sx(x),top,sx(x),H-bottom,'grid');text(chart,sx(x),H-bottom+18,String(x));});
  line(chart,left,top,left,H-bottom,'axis');line(chart,left,H-bottom,W-right,H-bottom,'axis');
  text(chart,left-37,top+4,'dB','middle');text(chart,W/2,H-5,'aligned sample index','middle');
  if(x0<=0&&x1>=0) line(chart,sx(0),top,sx(0),H-bottom,'axis');
  const curveXs=[]; const interpPointsPerSample=16; const step=1/interpPointsPerSample;
  for(let x=x0;x<=x1+step/2;x+=step) curveXs.push(x);
  f.markers.forEach(m=>curveXs.push(m.x));
  curveXs.sort((a,b)=>a-b);
  const points=[]; curveXs.forEach(x=>{const db=20*Math.log10(Math.max(interp(f,x+data.reference_peak),1e-12));points.push([sx(x),sy(Math.max(y0,Math.min(y1,db)))]);}); path(chart,points);
  f.markers.forEach(m=>circle(chart,sx(m.x),sy(Math.max(y0,Math.min(y1,m.db))),3.4));
  label.textContent=`Frame ${n+1}/${frames.length}`; source.textContent=`${f.source}, seq ${f.seq}`; peak.textContent=`delay ${f.time_delay.toFixed(3)} samples, phase ${f.phase_rad.toFixed(3)} rad`; rx.textContent=`RX timestamp ${f.rx_ts}`;
  drawHeat('waterfall', data.waterfall, data.x_min, data.x_max, n, 'aligned sample index');
  drawHeat('fftWaterfall', data.fft_waterfall, -data.frame_rate_hz/2, data.frame_rate_hz/2, -1, 'frame frequency (Hz)', 'tap index');
  frameRate.textContent=`${data.frame_rate_hz.toFixed(3)} Hz`;
}
function heatColor(value,lo,hi){const t=Math.max(0,Math.min(1,(value-lo)/(hi-lo)));return [Math.round(255*Math.max(0,(t-.55)*2.22)),Math.round(255*Math.sin(Math.PI*t)),Math.round(255*(1-t)),255];}
function drawHeat(id,matrix,xmin,xmax,row,xlabel,ylabel='frame'){const canvas=document.getElementById(id),ctx=canvas.getContext('2d'),width=720,height=matrix.length,image=ctx.createImageData(width,height);for(let y=0;y<height;y++)for(let x=0;x<width;x++){const col=Math.min(matrix[y].length-1,Math.floor(x/width*matrix[y].length)),c=heatColor(matrix[y][col],-10,70),j=(y*width+x)*4;image.data[j]=c[0];image.data[j+1]=c[1];image.data[j+2]=c[2];image.data[j+3]=255;}const source=document.createElement('canvas');source.width=width;source.height=height;source.getContext('2d').putImageData(image,0,0);ctx.clearRect(0,0,canvas.width,canvas.height);const left=54,right=14,top=8,bottom=30;ctx.drawImage(source,left,top,canvas.width-left-right,canvas.height-top-bottom);ctx.strokeStyle='#9eacba';ctx.strokeRect(left,top,canvas.width-left-right,canvas.height-top-bottom);ctx.fillStyle='#9eacba';ctx.font='12px system-ui';ctx.fillText(String(xmin),left,canvas.height-8);ctx.fillText(String(xmax),canvas.width-right-28,canvas.height-8);ctx.fillText(xlabel,canvas.width/2-45,canvas.height-8);ctx.fillText(ylabel,5,top+12);if(row>=0){ctx.fillText(String(row+1),5,top+(row+.7)/height*(canvas.height-top-bottom));ctx.strokeStyle='#ffb454';ctx.beginPath();const selectedY=top+(row+.5)/height*(canvas.height-top-bottom);ctx.moveTo(left,selectedY);ctx.lineTo(canvas.width-right,selectedY);ctx.stroke();}}
scrub.addEventListener('input',()=>render(Number(scrub.value)));
document.getElementById('prev').addEventListener('click',()=>render(Number(scrub.value)-1));
document.getElementById('next').addEventListener('click',()=>render(Number(scrub.value)+1));
document.addEventListener('keydown',e=>{if(e.key==='ArrowLeft')render(Number(scrub.value)-1);if(e.key==='ArrowRight')render(Number(scrub.value)+1);});
render(0);
</script>
</body>
</html>
'''


def write_html(path: Path, dataset: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HTML.replace("__DATA__", json.dumps(dataset, separators=(",", ":"))), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="one or more CIR capture CSV files")
    parser.add_argument("--output-csv", type=Path, default=Path("fw/captures/cir-aligned.csv"))
    parser.add_argument("--output-html", type=Path, default=Path("fw/captures/cir-viewer.html"))
    args = parser.parse_args()
    frames = align_frames([frame for path in args.inputs for frame in read_capture(path)])
    if not frames:
        raise SystemExit("no CIR rows found")
    dataset = build_waterfalls(frames, frames[0]["reference_peak"])
    write_aligned_csv(args.output_csv, frames)
    write_html(args.output_html, dataset)
    print(f"wrote {len(frames)} frames and {len(dataset['xs'])} interpolated CIR columns to {args.output_csv} and {args.output_html}")


if __name__ == "__main__":
    main()
