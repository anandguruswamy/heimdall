import type { PositionRange } from './types';

export type Vec3 = { x:number; y:number; z:number };
export type RangeSource = 'raw'|'smoothed'|'ultra';
export type PositionEdge = { a:number; b:number; measured:number; fitted:number; residual:number };
export type PositionSolution = { positions:Vec3[]; edges:PositionEdge[]; rmse:number; coverage:number; confidence:number; status:string };

const distance=(a:Vec3,b:Vec3)=>Math.hypot(a.x-b.x,a.y-b.y,a.z-b.z);
const pair=(ranges:{a:number;b:number;distance:number}[],a:number,b:number)=>ranges.find((edge)=>edge.a===Math.min(a,b)&&edge.b===Math.max(a,b))?.distance;

export function selectedRanges(values:Iterable<PositionRange>,source:RangeSource) {
  return Array.from(values).flatMap((value)=>{
    const distance=source==='raw'?value.raw:source==='smoothed'?value.smoothed:value.ultra;
    return distance !== undefined && Number.isFinite(distance) && distance>0 && !(source==='raw'&&value.outlier) ? [{a:value.a,b:value.b,distance}] : [];
  });
}

export function solvePositions(nodeCount:number,ranges:{a:number;b:number;distance:number}[],anchors:[number,number,number],above:number):PositionSolution {
  const [origin,xNode,xyNode]=anchors,scale=ranges.length?ranges.map((r)=>r.distance).sort((a,b)=>a-b)[Math.floor(ranges.length/2)]:1;
  const positions=Array.from({length:nodeCount},(_,i)=>({x:(i%3)*scale*.35,y:Math.floor(i/3)*scale*.35,z:i===above?scale*.35:scale*.12}));
  positions[origin]={x:0,y:0,z:0};
  const d01=pair(ranges,origin,xNode)??scale; positions[xNode]={x:d01,y:0,z:0};
  const d02=pair(ranges,origin,xyNode)??scale,d12=pair(ranges,xNode,xyNode)??scale,x2=(d02*d02+d01*d01-d12*d12)/(2*Math.max(d01,1e-6)),y2=Math.sqrt(Math.max(1e-4,d02*d02-x2*x2)); positions[xyNode]={x:x2,y:y2,z:0};
  for(let iteration=0;iteration<180;iteration++){
    const gradients=positions.map(()=>({x:0,y:0,z:0}));
    for(const edge of ranges){const a=positions[edge.a],b=positions[edge.b],dx=a.x-b.x,dy=a.y-b.y,dz=a.z-b.z,fit=Math.max(1e-6,Math.hypot(dx,dy,dz)),residual=Math.max(-.25,Math.min(.25,fit-edge.distance)),factor=residual/fit;gradients[edge.a].x+=factor*dx;gradients[edge.a].y+=factor*dy;gradients[edge.a].z+=factor*dz;gradients[edge.b].x-=factor*dx;gradients[edge.b].y-=factor*dy;gradients[edge.b].z-=factor*dz;}
    const step=.045/(1+iteration*.025)/Math.max(1,ranges.length/nodeCount);
    for(let i=0;i<nodeCount;i++){positions[i].x-=step*gradients[i].x;positions[i].y-=step*gradients[i].y;positions[i].z-=step*gradients[i].z;}
    positions[origin]={x:0,y:0,z:0}; positions[xNode].y=0;positions[xNode].z=0;positions[xNode].x=Math.abs(positions[xNode].x);positions[xyNode].z=0;positions[xyNode].y=Math.abs(positions[xyNode].y);positions[above].z=Math.abs(positions[above].z);
  }
  const edges=ranges.map((edge)=>{const fitted=distance(positions[edge.a],positions[edge.b]);return{...edge,measured:edge.distance,fitted,residual:fitted-edge.distance}}),rmse=edges.length?Math.sqrt(edges.reduce((sum,e)=>sum+e.residual*e.residual,0)/edges.length):0,possible=nodeCount*(nodeCount-1)/2,coverage=edges.length/Math.max(1,possible),needed=Math.max(1,3*nodeCount-6),supported=Math.min(1,edges.length/needed),confidence=supported*coverage*Math.exp(-rmse/Math.max(.05,scale*.05));
  return {positions,edges,rmse,coverage,confidence,status:edges.length<needed?'UNDERDETERMINED':confidence<.35?'DEGRADED':'SOLVED'};
}
