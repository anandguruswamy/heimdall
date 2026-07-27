import { spawn } from 'node:child_process';
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const edge = process.env.EDGE_PATH ?? 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const url = process.argv[2] ?? 'http://192.168.8.215:8080';
const output = process.argv[3] ?? join(tmpdir(), 'heimdall-tab-audit');
const captureScreenshots = !process.argv.includes('--no-screenshots');
const port = 9333;
const profile = join(tmpdir(), `heimdall-edge-${process.pid}`);
const tabs = ['Network Health', 'Live Distance', 'Instantaneous CIR', 'CIR Waterfall', 'Slow-Time FFT', 'Fast-Time FFT', 'CFO', 'Distance Calibration'];
mkdirSync(output, { recursive: true });

const browser = spawn(edge, [
  '--headless', '--hide-scrollbars', `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`, '--window-size=1440,1000', url
], { stdio: 'ignore' });

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
async function target() {
  for (let attempt = 0; attempt < 60; attempt++) {
    try {
      const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      const page = pages.find((item) => item.type === 'page');
      if (page) return page;
    } catch {}
    await sleep(250);
  }
  throw new Error('Edge DevTools endpoint did not start');
}

class Cdp {
  constructor(endpoint) {
    this.socket = new WebSocket(endpoint);
    this.next = 1;
    this.pending = new Map();
    this.events = [];
    this.socket.onmessage = ({ data }) => {
      const message = JSON.parse(data);
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        message.error ? pending.reject(new Error(message.error.message)) : pending.resolve(message.result);
      } else {
        this.events.push(message);
      }
    };
  }
  async open() {
    if (this.socket.readyState === WebSocket.OPEN) return;
    await new Promise((resolve, reject) => {
      this.socket.onopen = resolve;
      this.socket.onerror = reject;
    });
  }
  call(method, params = {}) {
    const id = this.next++;
    return new Promise((resolve, reject) => {
      const timer=setTimeout(()=>{ this.pending.delete(id); reject(new Error(`CDP ${method} timed out`)); },20_000);
      this.pending.set(id, { resolve:(value)=>{clearTimeout(timer);resolve(value);}, reject:(error)=>{clearTimeout(timer);reject(error);} });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression) {
    const result = await this.call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    return result.result.value;
  }
}

async function run() {
  const page = await target();
  const cdp = new Cdp(page.webSocketDebuggerUrl);
  await cdp.open();
  await cdp.call('Runtime.enable');
  await cdp.call('Page.enable');
  await sleep(2_000);
  const audits = [];
  for (const viewport of [{ name: 'desktop', width: 1440, height: 1000 }, { name: 'phone', width: 390, height: 844 }]) {
    await cdp.call('Emulation.setDeviceMetricsOverride', { width: viewport.width, height: viewport.height, deviceScaleFactor: 1, mobile: viewport.name === 'phone' });
    await cdp.call('Page.reload', { ignoreCache: true });
    await sleep(2_000);
    for (const tab of tabs) {
      console.error(`Auditing ${viewport.name}: ${tab}`);
      const clicked = await cdp.evaluate(`(() => { const button=[...document.querySelectorAll('.tabs button')].find((item)=>item.textContent.trim().endsWith(${JSON.stringify(tab)})); button?.click(); return Boolean(button); })()`);
      if (!clicked) throw new Error(`Tab not found: ${tab}`);
      await sleep(tab === 'Slow-Time FFT' ? 2_500 : 1_200);
      const state = await cdp.evaluate(`(() => ({
        active: document.querySelector('.mode-head h1')?.textContent,
        status: document.querySelector('.status')?.textContent?.trim(),
        canvases: [...document.querySelectorAll('canvas')].map((canvas)=>({width:canvas.width,height:canvas.height})),
        noData: [...document.querySelectorAll('.link-cell footer span')].filter((item)=>item.textContent.includes('NO DATA')).length,
        links: document.querySelectorAll('.link-cell').length,
        synthetic: document.body.textContent.includes('SYNTHETIC'),
        uncalibrated: document.body.textContent.includes('UNCALIBRATED'),
        bodyWidth: document.body.getBoundingClientRect().width,
        viewportWidth: innerWidth,
        linkLayout: [...document.querySelectorAll('.link-cell')].slice(0,5).map((item)=>{const box=item.getBoundingClientRect();return {label:item.querySelector('header b')?.textContent?.trim(),x:Math.round(box.x),y:Math.round(box.y)};})
      }))()`);
      const slug = tab.toLowerCase().replaceAll(/[^a-z0-9]+/g, '-');
      let screenshotCaptured=!captureScreenshots || tab === 'Distance Calibration' ? null : true;
      try { if (screenshotCaptured) {
        const screenshot = await cdp.call('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
        writeFileSync(join(output, `${viewport.name}-${slug}.png`), Buffer.from(screenshot.data, 'base64'));
      }} catch (error) {
        screenshotCaptured=false;
        console.error(`${viewport.name} ${tab} screenshot failed: ${error.message}`);
      }
      audits.push({ viewport: viewport.name, tab, screenshotCaptured, ...state });
    }
  }
  const exceptions = cdp.events.filter((event) => event.method === 'Runtime.exceptionThrown');
  const failures = audits.filter((item) => {
    const layout=item.viewport==='desktop'&&item.tab==='Live Distance'?item.linkLayout:[];
    const columnMajor=!layout.length||(layout.slice(0,4).every((cell)=>cell.x===layout[0].x)&&layout.slice(1,4).every((cell,index)=>cell.y>layout[index].y)&&layout[4]?.x>layout[0].x&&layout[4]?.y===layout[0].y);
    return item.active !== item.tab || item.status !== 'LIVE' || item.synthetic || !columnMajor || item.canvases.some((canvas) => canvas.width < 1 || canvas.height < 1) || (!['Network Health', 'Distance Calibration'].includes(item.tab) && item.noData > 0);
  });
  const report = { url, output, audits, exceptions: exceptions.length, failures: failures.length, screenshotFailures: audits.filter((item)=>item.screenshotCaptured===false).length };
  console.log(JSON.stringify(report, null, 2));
  if (failures.length || exceptions.length) process.exitCode = 1;
  cdp.socket.close();
}

try {
  await run();
} finally {
  browser.kill();
  await sleep(500);
  try { rmSync(profile, { recursive: true, force: true }); } catch {}
}
