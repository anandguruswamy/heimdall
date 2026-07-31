import { spawn } from 'node:child_process';
import { rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const edge = process.env.EDGE_PATH ?? 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const url = process.argv[2] ?? 'http://127.0.0.1:8080';
const durationMs = Number(process.argv[3] ?? 15_000);
const port = 9334;
const profile = join(tmpdir(), `heimdall-latency-${process.pid}`);
const browser = spawn(edge, ['--headless', '--hide-scrollbars', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, '--window-size=1440,1000', url], { stdio: 'ignore' });
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
    this.socket = new WebSocket(endpoint); this.next = 1; this.pending = new Map(); this.events = [];
    this.socket.onmessage = ({ data }) => { const message=JSON.parse(data);if(!message.id){this.events.push(message);return;}const pending=this.pending.get(message.id);if(!pending)return;this.pending.delete(message.id);message.error?pending.reject(new Error(message.error.message)):pending.resolve(message.result); };
  }
  async open() { if(this.socket.readyState===WebSocket.OPEN)return;await new Promise((resolve,reject)=>{this.socket.onopen=resolve;this.socket.onerror=reject;}); }
  call(method,params={}) { const id=this.next++;return new Promise((resolve,reject)=>{this.pending.set(id,{resolve,reject});this.socket.send(JSON.stringify({id,method,params}));}); }
  async evaluate(expression) { const result=await this.call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(result.exceptionDetails)throw new Error(result.exceptionDetails.text);return result.result.value; }
}

try {
  const page = await target(), cdp = new Cdp(page.webSocketDebuggerUrl); await cdp.open();
  await cdp.call('Runtime.enable'); await cdp.call('Performance.enable'); await cdp.call('Network.enable'); await sleep(2_000);
  await cdp.evaluate(`(() => { const button=[...document.querySelectorAll('.tabs button')].find((item)=>item.textContent.trim().endsWith('Live Distance'));button?.click();window.__heimdallPerf={frames:[],longTasks:[],rounds:[],started:performance.now()};let last;const frame=(now)=>{if(last)window.__heimdallPerf.frames.push({at:now,duration:now-last});last=now;requestAnimationFrame(frame)};requestAnimationFrame(frame);new PerformanceObserver((list)=>window.__heimdallPerf.longTasks.push(...list.getEntries().map((item)=>({at:item.startTime,duration:item.duration})))).observe({type:'longtask',buffered:true});let previous='';setInterval(()=>{const value=document.querySelector('.header-metrics div:first-child b')?.textContent??'';if(value!==previous){window.__heimdallPerf.rounds.push({at:performance.now(),value});previous=value}},10);return Boolean(button);})()`);
  await sleep(1_000);
  await cdp.evaluate(`(() => { const p=window.__heimdallPerf;p.frames=[];p.longTasks=[];p.rounds=[];p.started=performance.now(); })()`);
  const initial = await cdp.call('Performance.getMetrics'); await sleep(durationMs); const final = await cdp.call('Performance.getMetrics');
  const browserMetrics = await cdp.evaluate(`(() => { const p=window.__heimdallPerf,values=p.frames.map((item)=>item.duration),sorted=values.slice().sort((a,b)=>a-b),q=(n)=>sorted[Math.min(sorted.length-1,Math.floor(sorted.length*n))]??0,memory=performance.memory,frameStalls=p.frames.filter((item)=>item.duration>40).map((item)=>({at_ms:item.at-p.started,duration_ms:item.duration})),roundGaps=p.rounds.slice(1).map((item,index)=>({at_ms:item.at-p.started,duration_ms:item.at-p.rounds[index].at})).filter((item)=>item.duration_ms>80);return{duration_ms:performance.now()-p.started,frames:p.frames.length,frame_p50_ms:q(.5),frame_p95_ms:q(.95),frame_p99_ms:q(.99),frame_max_ms:Math.max(0,...values),frames_over_50_ms:values.filter((v)=>v>50).length,frames_over_100_ms:values.filter((v)=>v>100).length,frame_stalls:frameStalls,long_tasks:p.longTasks.length,long_task_total_ms:p.longTasks.reduce((sum,item)=>sum+item.duration,0),long_task_max_ms:Math.max(0,...p.longTasks.map((item)=>item.duration)),long_task_details:p.longTasks.map((item)=>({at_ms:item.at-p.started,duration_ms:item.duration})),round_updates:p.rounds.length,round_gap_max_ms:Math.max(0,...roundGaps.map((item)=>item.duration_ms)),round_gaps:roundGaps,heap_used_bytes:memory?.usedJSHeapSize??null,heap_total_bytes:memory?.totalJSHeapSize??null};})()`);
  const metrics = (result) => Object.fromEntries(result.metrics.map((item) => [item.name,item.value]));
  const before=metrics(initial),after=metrics(final);
  const socketFrames=cdp.events.filter((event)=>event.method==='Network.webSocketFrameReceived').map((event)=>event.params.timestamp*1000);
  const socketGaps=socketFrames.slice(1).map((at,index)=>({at_ms:at-socketFrames[0],duration_ms:at-socketFrames[index]})).filter((item)=>item.duration_ms>40);
  console.log(JSON.stringify({...browserMetrics,websocket_frames:socketFrames.length,websocket_gap_max_ms:Math.max(0,...socketGaps.map((item)=>item.duration_ms)),websocket_gaps:socketGaps,task_time_delta_ms:(after.TaskDuration-before.TaskDuration)*1000,script_time_delta_ms:(after.ScriptDuration-before.ScriptDuration)*1000,layout_time_delta_ms:(after.LayoutDuration-before.LayoutDuration)*1000},null,2));
  cdp.socket.close();
} finally {
  browser.kill(); await sleep(500); try { rmSync(profile,{recursive:true,force:true}); } catch {}
}
