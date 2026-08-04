<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import BoardScene from './BoardScene.svelte';
  import type { HeimdallApi } from './api';
  import type { LiveStore } from './live';
  import type { PositionRange } from './types';
  import { PositionSolver, selectedRanges, type RangeSource } from './positions';
  let { live, api, nodeCount, revision }: { live:LiveStore; api:HeimdallApi; nodeCount:number; revision:number }=$props();
  const initialFreeze=untrack(()=>live.boardFreeze);
  let frozen=$state(Boolean(initialFreeze)),freeze=$state.raw(initialFreeze),freezeBusy=$state(false),freezeError=$state(''),source:RangeSource=$state(initialFreeze?.source ?? 'smoothed'),edgeMode:'residual'|'neutral'|'hidden'=$state('residual'),origin=$state(initialFreeze?.origin ?? 0),xAxis=$state(initialFreeze?.xAxis ?? 1),xyPlane=$state(initialFreeze?.xyPlane ?? 2),above=$state(initialFreeze?.above ?? 3);
  const positionSolver=new PositionSolver();
  const liveRanges=$derived.by(()=>{void revision;return Array.from(live.positionRanges.values()).map((item)=>({...item,window:item.window.slice()}));});
  const effectiveNodeCount=$derived(freeze?.nodeCount ?? nodeCount);
  const nodes=$derived(Array.from({length:effectiveNodeCount},(_,i)=>i));
  const rangeState=$derived(freeze?.ranges ?? liveRanges);
  const ranges=$derived(selectedRanges(rangeState,source));
  const newestRangeEvent=$derived(Math.max(0,...rangeState.map((item)=>item.eventS)));
  const solution=$derived(freeze?.solution ?? positionSolver.solve(effectiveNodeCount,ranges,[origin,xAxis,xyPlane],above));
  const age=(a:number,b:number)=>{const value=rangeState.find((item)=>item.a===Math.min(a,b)&&item.b===Math.max(a,b));return value?Math.max(0,newestRangeEvent-value.eventS):undefined};
  const geometryDocument=$derived.by(()=>({schema:'heimdall-geometry/1',units:'m',revision:`dashboard-${frozen?'frozen':'live'}-${Math.round(newestRangeEvent*1000)}`,frame:{name:'dashboard-range-derived',origin:`N${origin} antenna phase centre`,axes:`+X toward N${xAxis}, +Y side selected by N${xyPlane}, +Z side selected by N${above}`},provenance:{source:'UNO Q Board Positions',range_source:source,configuration_revision:freeze?.configurationRevision ?? revision,newest_event_s:newestRangeEvent,fit_rmse_m:solution.rmse,fit_rank:solution.rank,fit_degrees_of_freedom:solution.degreesOfFreedom,fit_iterations:solution.iterations,fit_converged:solution.converged,pairs:solution.edges.map((edge)=>({a:edge.a,b:edge.b,distance_m:edge.measured,age_s:age(edge.a,edge.b)})),calibration_status:'antenna-delay-not-independently-verified'},nodes:solution.positions.map((point,node_id)=>({node_id,position_m:[point.x,point.y,point.z]}))}));
  async function toggleFreeze(){
    freezeBusy=true;freezeError='';
    try {
      if(frozen){await api.unfreezeBoard();live.unfreezeBoard();freeze=null;frozen=false;return}
      await api.freezeBoard();
      live.freezeBoard(liveRanges,{source,origin,xAxis,xyPlane,above,nodeCount,configurationRevision:revision,solution});
      freeze=live.boardFreeze;frozen=true;
    } catch { freezeError='BACKEND FREEZE FAILED'; }
    finally { freezeBusy=false; }
  }
  onMount(()=>{
    const reconcile=async()=>{
      try {
        const status=await api.boardStatus() as Record<string,unknown>,backendFrozen=Boolean(status.board_frozen);
        if(frozen&&!backendFrozen){live.unfreezeBoard();freeze=null;frozen=false;freezeError='BACKEND FREEZE LOST - FREEZE AGAIN';}
        else if(!frozen&&backendFrozen){freezeError='BACKEND REFERENCE IS FROZEN';}
      } catch {}
    };
    void reconcile();const timer=setInterval(reconcile,2000);return()=>clearInterval(timer);
  });
  const pct=(value:number)=>`${Math.round(value*100)}%`;
  function exportGeometry(){const blob=new Blob([`${JSON.stringify(geometryDocument,null,2)}\n`],{type:'application/json'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`heimdall-geometry-${geometryDocument.revision}.json`;link.click();URL.revokeObjectURL(url)}
</script>
<section class="positions-layout" data-geometry={JSON.stringify(geometryDocument)}>
  <div class="position-controls panel">
    <div class="control-row"><button class="capture" onclick={toggleFreeze} disabled={freezeBusy}>{freezeBusy?'WORKING':frozen ? 'UNFREEZE' : 'FREEZE'}</button></div>{#if freezeError}<p class="freeze-error">{freezeError}</p>{/if}<button class="export" onclick={exportGeometry}>EXPORT GEOMETRY</button>    <label>RANGE SOURCE<select bind:value={source} disabled={frozen}><option value="raw">DS-TWR raw</option><option value="smoothed">DS-TWR smoothed</option><option value="ultra">DS-TWR ultra smoothed</option></select></label>
    <fieldset><legend>COORDINATE FRAME</legend><label>ORIGIN<select bind:value={origin} disabled={frozen}>{#each nodes as node}<option value={node} disabled={node===xAxis||node===xyPlane||node===above}>N{node}</option>{/each}</select></label><label>+X REFERENCE<select bind:value={xAxis} disabled={frozen}>{#each nodes as node}<option value={node} disabled={node===origin||node===xyPlane||node===above}>N{node}</option>{/each}</select></label><label>+Y SIDE REFERENCE<select bind:value={xyPlane} disabled={frozen}>{#each nodes as node}<option value={node} disabled={node===origin||node===xAxis||node===above}>N{node}</option>{/each}</select></label><label>ABOVE PLANE (+Z)<select bind:value={above} disabled={frozen}>{#each nodes as node}<option value={node} disabled={node===origin||node===xAxis||node===xyPlane}>N{node}</option>{/each}</select></label></fieldset>
    <fieldset><legend>EDGES</legend><div class="segmented edge-mode"><button class:active={edgeMode==='residual'} onclick={()=>edgeMode='residual'}>RESIDUAL</button><button class:active={edgeMode==='neutral'} onclick={()=>edgeMode='neutral'}>NEUTRAL</button><button class:active={edgeMode==='hidden'} onclick={()=>edgeMode='hidden'}>HIDDEN</button></div></fieldset>
  </div>
  <article class="scene-panel panel"><header><span>RECONSTRUCTED BOARD GEOMETRY</span><b>{frozen ? 'FROZEN' : 'LIVE'} · {source.replace('ultra','ultra smooth').toUpperCase()}</b></header><div><BoardScene positions={solution.positions} edges={solution.edges} {edgeMode} /></div></article>
    <aside class="diagnostics panel"><header><span>FIT DIAGNOSTICS</span><b class:warn={solution.status!=='SOLVED'}>{solution.status}</b></header><dl><dt>RMSE</dt><dd>{(solution.rmse*100).toFixed(2)} cm</dd><dt>PAIR COVERAGE</dt><dd>{solution.edges.length}/{effectiveNodeCount*(effectiveNodeCount-1)/2} · {pct(solution.coverage)}</dd><dt>JACOBIAN RANK</dt><dd>{solution.rank}/{solution.degreesOfFreedom}</dd><dt>OPTIMIZER</dt><dd>{solution.converged?'CONVERGED':'INCOMPLETE'} · {solution.iterations} iter</dd><dt>CONFIDENCE</dt><dd>{pct(solution.confidence)}</dd><dt>FRAME</dt><dd>N{origin} origin · N{xAxis} +X<br>N{xyPlane} +Y · N{above} +Z</dd></dl><div class="position-list">{#each solution.positions as point,node}<div><b>N{node}</b><span>{point.x.toFixed(3)} / {point.y.toFixed(3)} / {point.z.toFixed(3)} m</span></div>{/each}</div><div class="edge-list">{#each solution.edges.slice().sort((a,b)=>Math.abs(b.residual)-Math.abs(a.residual)) as edge}<div><span>N{edge.a}↔N{edge.b}<small>{age(edge.a,edge.b)?.toFixed(0)??'—'}s</small></span><b>{edge.measured.toFixed(3)} m</b><i class:bad={Math.abs(edge.residual)>.1}>{edge.residual>=0?'+':''}{(edge.residual*100).toFixed(1)} cm</i></div>{/each}</div></aside>
</section>
<style>
  .positions-layout{display:grid;grid-template-columns:230px minmax(0,1fr) 250px;gap:7px;min-height:0}.panel{min-height:0}.position-controls{padding:10px;overflow-y:auto}  .control-row{display:flex;gap:6px;margin-bottom:10px}.segmented{display:flex;border:1px solid #304147;padding:2px}.segmented button,.capture{border:0;background:transparent;color:#718188;padding:8px 9px;font:9px DM Mono,monospace}.segmented button.active,.capture{background:#45e0c1;color:#07110f}.capture{flex:1;width:100%;margin:0}.position-controls>label,.position-controls fieldset label{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:9px 0;color:#91a1a6;font:8px DM Mono,monospace}.position-controls select{width:120px;padding:5px}.position-controls fieldset{margin:12px 0;padding:8px;border:1px solid #26373d}.position-controls legend{color:#45e0c1;padding:0 4px;font:8px DM Mono,monospace}.edge-mode button{padding:5px}.scene-panel{display:grid;grid-template-rows:36px minmax(0,1fr)}.scene-panel>header,.diagnostics>header{height:36px;padding:0 10px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #233138;color:#91a1a6;font:9px DM Mono,monospace}.scene-panel>header b,.diagnostics>header b{color:#45e0c1;font-weight:400}.scene-panel>div{min-height:0}.diagnostics{overflow:hidden}.diagnostics dl{display:grid;grid-template-columns:1fr auto;gap:8px;margin:0;padding:12px;border-bottom:1px solid #233138;font:9px DM Mono,monospace}.diagnostics dt{color:#718188}.diagnostics dd{margin:0;text-align:right}.warn{color:#f4bd62!important}.edge-list{height:calc(100% - 150px);overflow:auto;padding:4px 10px}.edge-list div{display:grid;grid-template-columns:1fr auto auto;gap:8px;padding:7px 0;border-bottom:1px solid #1b282d;font:8px DM Mono,monospace}.edge-list span{color:#a9b8bb}.edge-list b{font-weight:400}.edge-list i{color:#45e0c1;font-style:normal}.edge-list i.bad{color:#f4bd62}@media(max-width:900px){.positions-layout{grid-template-columns:110px minmax(0,1fr)}.position-controls{padding:6px}.position-controls label{display:block!important}.position-controls select{display:block;width:100%;margin-top:3px}.control-row{display:block}.capture{width:100%;margin-top:4px}.diagnostics{display:none}.edge-mode{display:block}.edge-mode button{width:100%}}
  @media(min-width:901px){.diagnostics{display:grid;grid-template-rows:36px auto minmax(0,1fr)}.edge-list{height:auto;min-height:0}}
  .edge-list small{margin-left:5px;color:#61757b;font:7px DM Mono,monospace}
  .export{width:100%;margin-bottom:8px;border:1px solid #385056;background:#0b1215;color:#9fb0b4;padding:6px;font:8px DM Mono,monospace}.position-list{padding:6px 10px;border-bottom:1px solid #233138}.position-list div{display:flex;justify-content:space-between;padding:3px 0;font:8px DM Mono,monospace}.position-list b{color:#45e0c1}.position-list span{color:#9fb0b4}
  .freeze-error{margin:0 0 8px;color:#f4bd62;font:8px DM Mono,monospace}
</style>
