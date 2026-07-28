import type { PositionRange } from './types';

export type Vec3 = { x:number; y:number; z:number };
export type RangeSource = 'raw'|'smoothed'|'ultra';
export type PositionEdge = { a:number; b:number; measured:number; fitted:number; residual:number };
export type PositionSolution = {
  positions:Vec3[];
  edges:PositionEdge[];
  rmse:number;
  coverage:number;
  confidence:number;
  status:string;
  rank:number;
  degreesOfFreedom:number;
  iterations:number;
  converged:boolean;
};

type Range = { a:number; b:number; distance:number };
type Parameter = { node:number; axis:keyof Vec3 };

const distance=(a:Vec3,b:Vec3)=>Math.hypot(a.x-b.x,a.y-b.y,a.z-b.z);
const clonePositions=(positions:Vec3[])=>positions.map((point)=>({...point}));
const pair=(ranges:Range[],a:number,b:number)=>ranges.find((edge)=>edge.a===Math.min(a,b)&&edge.b===Math.max(a,b))?.distance;

export function selectedRanges(values:Iterable<PositionRange>,source:RangeSource):Range[] {
  return Array.from(values).flatMap((value)=>{
    const measurement=source==='raw'?value.raw:source==='smoothed'?value.smoothed:value.ultra;
    return measurement!==undefined&&Number.isFinite(measurement)&&measurement>0&&!(source==='raw'&&value.outlier)?[{a:value.a,b:value.b,distance:measurement}]:[];
  });
}

export class PositionSolver {
  private key='';
  private previous:Vec3[]|undefined;

  solve(nodeCount:number,ranges:Range[],anchors:[number,number,number],above:number):PositionSolution {
    const key=`${nodeCount}:${anchors.join(',')}:${above}`;
    const solution=solvePositions(nodeCount,ranges,anchors,above,key===this.key?this.previous:undefined);
    this.key=key;
    if(solution.converged&&solution.rank===solution.degreesOfFreedom&&solution.positions.every((point)=>Object.values(point).every(Number.isFinite))) this.previous=clonePositions(solution.positions);
    return solution;
  }
}

export function solvePositions(nodeCount:number,ranges:Range[],anchors:[number,number,number],above:number,previous?:Vec3[]):PositionSolution {
  const [origin,xNode,xyNode]=anchors;
  const validFrame=nodeCount>=4&&[origin,xNode,xyNode,above].every((node)=>Number.isInteger(node)&&node>=0&&node<nodeCount)&&new Set([origin,xNode,xyNode,above]).size===4;
  const usable=ranges.filter((edge)=>edge.a>=0&&edge.a<nodeCount&&edge.b>=0&&edge.b<nodeCount&&edge.a!==edge.b&&Number.isFinite(edge.distance)&&edge.distance>0);
  const possible=nodeCount*(nodeCount-1)/2,coverage=usable.length/Math.max(1,possible);
  if(!validFrame){
    return {positions:Array.from({length:nodeCount},()=>({x:0,y:0,z:0})),edges:[],rmse:0,coverage,confidence:0,status:'NEED 4 NODES',rank:0,degreesOfFreedom:Math.max(0,3*nodeCount-6),iterations:0,converged:false};
  }

  const parameters=parameterLayout(nodeCount,origin,xNode,xyNode);
  const scale=usable.length?median(usable.map((edge)=>edge.distance)):1;
  const initial=previous?.length===nodeCount?clonePositions(previous):classicalMds(nodeCount,usable,anchors,above)??fallbackPositions(nodeCount,usable,anchors,above,scale);
  let values=pack(initial,parameters),lambda=1e-3,iterations=0,converged=false;
  let current=evaluate(values,nodeCount,parameters,usable);

  for(iterations=0;iterations<60;iterations++){
    const {normal,gradient}=normalEquations(current.jacobian,current.residuals);
    const gradientMax=Math.max(0,...gradient.map(Math.abs));
    if(gradientMax<=Math.max(1e-11,scale*1e-10)){converged=true;break;}
    for(let index=0;index<normal.length;index++) normal[index][index]+=lambda*Math.max(1,normal[index][index]);
    const step=solveLinear(normal,gradient.map((value)=>-value));
    if(!step){lambda*=10;continue;}
    const candidateValues=values.map((value,index)=>value+step[index]);
    const candidate=evaluate(candidateValues,nodeCount,parameters,usable);
    if(candidate.cost<current.cost){
      const improvement=current.cost-candidate.cost;
      values=candidateValues;current=candidate;lambda=Math.max(1e-12,lambda/3);
      const stepMax=Math.max(0,...step.map(Math.abs));
      if(stepMax<=Math.max(1e-10,scale*1e-9)||improvement<=Math.max(1e-18,current.cost*1e-12)){converged=true;iterations++;break;}
    }else lambda=Math.min(1e12,lambda*10);
  }

  const positions=orient(current.positions,xNode,xyNode,above);
  const edges=usable.map((edge)=>{const fitted=distance(positions[edge.a],positions[edge.b]);return{...edge,measured:edge.distance,fitted,residual:fitted-edge.distance};});
  const rmse=edges.length?Math.sqrt(edges.reduce((sum,edge)=>sum+edge.residual*edge.residual,0)/edges.length):0;
  const finalEvaluation=evaluate(pack(positions,parameters),nodeCount,parameters,usable);
  const rank=matrixRank(finalEvaluation.jacobian),degreesOfFreedom=parameters.length,observable=rank===degreesOfFreedom;
  const residualFactor=Math.exp(-rmse/Math.max(.005,scale*.02));
  const confidence=coverage*(degreesOfFreedom?rank/degreesOfFreedom:0)*residualFactor;
  const status=!observable?'UNDERDETERMINED':!converged?'NOT CONVERGED':rmse>Math.max(.05,scale*.05)?'DEGRADED':'SOLVED';
  return {positions,edges,rmse,coverage,confidence,status,rank,degreesOfFreedom,iterations,converged};
}

function parameterLayout(nodeCount:number,origin:number,xNode:number,xyNode:number):Parameter[]{
  const output:Parameter[]=[];
  for(let node=0;node<nodeCount;node++){
    if(node===origin) continue;
    if(node===xNode) output.push({node,axis:'x'});
    else if(node===xyNode) output.push({node,axis:'x'},{node,axis:'y'});
    else output.push({node,axis:'x'},{node,axis:'y'},{node,axis:'z'});
  }
  return output;
}

function pack(positions:Vec3[],parameters:Parameter[]):number[]{
  return parameters.map((parameter)=>positions[parameter.node][parameter.axis]);
}

function unpack(values:number[],nodeCount:number,parameters:Parameter[]):Vec3[]{
  const positions=Array.from({length:nodeCount},()=>({x:0,y:0,z:0}));
  parameters.forEach((parameter,index)=>positions[parameter.node][parameter.axis]=values[index]);
  return positions;
}

function evaluate(values:number[],nodeCount:number,parameters:Parameter[],ranges:Range[]){
  const positions=unpack(values,nodeCount,parameters),residuals:number[]=[],jacobian:number[][]=[];
  for(const edge of ranges){
    const a=positions[edge.a],b=positions[edge.b],delta={x:a.x-b.x,y:a.y-b.y,z:a.z-b.z},fitted=Math.max(1e-12,Math.hypot(delta.x,delta.y,delta.z));
    residuals.push(fitted-edge.distance);
    jacobian.push(parameters.map((parameter)=>parameter.node===edge.a?delta[parameter.axis]/fitted:parameter.node===edge.b?-delta[parameter.axis]/fitted:0));
  }
  return {positions,residuals,jacobian,cost:residuals.reduce((sum,value)=>sum+value*value,0)*.5};
}

function normalEquations(jacobian:number[][],residuals:number[]){
  const columns=jacobian[0]?.length??0,normal=Array.from({length:columns},()=>Array(columns).fill(0)),gradient=Array(columns).fill(0);
  for(let row=0;row<jacobian.length;row++) for(let a=0;a<columns;a++){
    gradient[a]+=jacobian[row][a]*residuals[row];
    for(let b=0;b<=a;b++) normal[a][b]+=jacobian[row][a]*jacobian[row][b];
  }
  for(let a=0;a<columns;a++) for(let b=0;b<a;b++) normal[b][a]=normal[a][b];
  return {normal,gradient};
}

function solveLinear(matrix:number[][],rhs:number[]):number[]|undefined{
  const n=rhs.length,a=matrix.map((row,index)=>[...row,rhs[index]]);
  for(let column=0;column<n;column++){
    let pivot=column;
    for(let row=column+1;row<n;row++) if(Math.abs(a[row][column])>Math.abs(a[pivot][column])) pivot=row;
    if(Math.abs(a[pivot][column])<1e-14) return undefined;
    [a[column],a[pivot]]=[a[pivot],a[column]];
    const divisor=a[column][column];for(let value=column;value<=n;value++)a[column][value]/=divisor;
    for(let row=0;row<n;row++) if(row!==column){const factor=a[row][column];for(let value=column;value<=n;value++)a[row][value]-=factor*a[column][value];}
  }
  return a.map((row)=>row[n]);
}

function matrixRank(input:number[][]):number{
  if(!input.length||!input[0].length) return 0;
  const matrix=input.map((row)=>row.slice()),rows=matrix.length,columns=matrix[0].length,maxValue=Math.max(...matrix.flat().map(Math.abs),1),tolerance=maxValue*Math.max(rows,columns)*1e-10;
  let rank=0;
  for(let column=0;column<columns&&rank<rows;column++){
    let pivot=rank;for(let row=rank+1;row<rows;row++)if(Math.abs(matrix[row][column])>Math.abs(matrix[pivot][column]))pivot=row;
    if(Math.abs(matrix[pivot][column])<=tolerance)continue;
    [matrix[rank],matrix[pivot]]=[matrix[pivot],matrix[rank]];
    for(let row=rank+1;row<rows;row++){const factor=matrix[row][column]/matrix[rank][column];for(let value=column;value<columns;value++)matrix[row][value]-=factor*matrix[rank][value];}
    rank++;
  }
  return rank;
}

function classicalMds(nodeCount:number,ranges:Range[],anchors:[number,number,number],above:number):Vec3[]|undefined{
  if(ranges.length!==nodeCount*(nodeCount-1)/2) return undefined;
  const distances=Array.from({length:nodeCount},()=>Array(nodeCount).fill(0));
  for(const edge of ranges) distances[edge.a][edge.b]=distances[edge.b][edge.a]=edge.distance;
  if(distances.some((row,index)=>row.some((value,column)=>index!==column&&value<=0))) return undefined;
  const rowMeans=distances.map((row)=>row.reduce((sum,value)=>sum+value*value,0)/nodeCount);
  const totalMean=rowMeans.reduce((sum,value)=>sum+value,0)/nodeCount;
  const gram=distances.map((row,index)=>row.map((value,column)=>-.5*(value*value-rowMeans[index]-rowMeans[column]+totalMean)));
  const {values,vectors}=symmetricEigen(gram),order=values.map((value,index)=>({value,index})).sort((a,b)=>b.value-a.value);
  const positions=Array.from({length:nodeCount},()=>({x:0,y:0,z:0}));
  (['x','y','z'] as const).forEach((axis,dimension)=>{const eigen=order[dimension];if(!eigen||eigen.value<=0)return;const magnitude=Math.sqrt(eigen.value);for(let node=0;node<nodeCount;node++)positions[node][axis]=vectors[node][eigen.index]*magnitude;});
  return alignFrame(positions,anchors,above);
}

function symmetricEigen(input:number[][]){
  const n=input.length,matrix=input.map((row)=>row.slice()),vectors:number[][]=Array.from({length:n},(_,row)=>Array.from({length:n},(_,column)=>row===column?1:0));
  for(let iteration=0;iteration<100*n*n;iteration++){
    let p=0,q=1,largest=0;
    for(let row=0;row<n;row++)for(let column=row+1;column<n;column++)if(Math.abs(matrix[row][column])>largest){largest=Math.abs(matrix[row][column]);p=row;q=column;}
    const diagonal=Math.max(1,...matrix.map((row,index)=>Math.abs(row[index])));if(largest<=diagonal*1e-12)break;
    const angle=.5*Math.atan2(2*matrix[p][q],matrix[q][q]-matrix[p][p]),c=Math.cos(angle),s=Math.sin(angle),app=matrix[p][p],aqq=matrix[q][q],apq=matrix[p][q];
    for(let index=0;index<n;index++)if(index!==p&&index!==q){const aip=matrix[index][p],aiq=matrix[index][q];matrix[index][p]=matrix[p][index]=c*aip-s*aiq;matrix[index][q]=matrix[q][index]=s*aip+c*aiq;}
    matrix[p][p]=c*c*app-2*s*c*apq+s*s*aqq;matrix[q][q]=s*s*app+2*s*c*apq+c*c*aqq;matrix[p][q]=matrix[q][p]=0;
    for(let row=0;row<n;row++){const vip=vectors[row][p],viq=vectors[row][q];vectors[row][p]=c*vip-s*viq;vectors[row][q]=s*vip+c*viq;}
  }
  return {values:matrix.map((row,index)=>row[index]),vectors};
}

function alignFrame(positions:Vec3[],anchors:[number,number,number],above:number):Vec3[]{
  const [origin,xNode,xyNode]=anchors,base=positions[origin],translated=positions.map((point)=>subtract(point,base)),ex=normalize(translated[xNode]);
  const xyProjection=dot(translated[xyNode],ex),ey=normalize(subtract(translated[xyNode],multiply(ex,xyProjection))),ez=cross(ex,ey);
  if(length(ex)<.5||length(ey)<.5||length(ez)<.5)return positions;
  const aligned=translated.map((point)=>({x:dot(point,ex),y:dot(point,ey),z:dot(point,ez)}));
  return orient(aligned,xNode,xyNode,above);
}

function fallbackPositions(nodeCount:number,ranges:Range[],anchors:[number,number,number],above:number,scale:number):Vec3[]{
  const [origin,xNode,xyNode]=anchors,positions=Array.from({length:nodeCount},(_,node)=>({x:(node%3)*scale*.4,y:Math.floor(node/3)*scale*.4,z:node===above?scale*.5:scale*.15}));
  positions[origin]={x:0,y:0,z:0};
  const d01=pair(ranges,origin,xNode)??scale;positions[xNode]={x:d01,y:0,z:0};
  const d02=pair(ranges,origin,xyNode)??scale,d12=pair(ranges,xNode,xyNode)??scale,x2=(d02*d02+d01*d01-d12*d12)/(2*Math.max(d01,1e-9)),y2=Math.sqrt(Math.max(scale*scale*1e-4,d02*d02-x2*x2));positions[xyNode]={x:x2,y:y2,z:0};
  for(let node=0;node<nodeCount;node++)if(!anchors.includes(node)){
    const d0=pair(ranges,origin,node),d1=pair(ranges,xNode,node),d2=pair(ranges,xyNode,node);
    if(d0!==undefined&&d1!==undefined&&d2!==undefined){const x=(d0*d0+d01*d01-d1*d1)/(2*Math.max(d01,1e-9)),y=(d0*d0+x2*x2+y2*y2-d2*d2-2*x*x2)/(2*Math.max(y2,1e-9)),z=Math.sqrt(Math.max(scale*scale*1e-4,d0*d0-x*x-y*y));positions[node]={x,y,z};}
  }
  return positions;
}

function orient(positions:Vec3[],xNode:number,xyNode:number,above:number):Vec3[]{
  const output=clonePositions(positions);
  if(output[xNode].x<0)for(const point of output)point.x=-point.x;
  if(output[xyNode].y<0)for(const point of output)point.y=-point.y;
  if(output[above].z<0)for(const point of output)point.z=-point.z;
  return output;
}

const median=(values:number[])=>{const sorted=values.slice().sort((a,b)=>a-b),middle=Math.floor(sorted.length/2);return sorted.length%2?sorted[middle]:(sorted[middle-1]+sorted[middle])/2;};
const subtract=(a:Vec3,b:Vec3):Vec3=>({x:a.x-b.x,y:a.y-b.y,z:a.z-b.z});
const multiply=(a:Vec3,value:number):Vec3=>({x:a.x*value,y:a.y*value,z:a.z*value});
const dot=(a:Vec3,b:Vec3)=>a.x*b.x+a.y*b.y+a.z*b.z;
const cross=(a:Vec3,b:Vec3):Vec3=>({x:a.y*b.z-a.z*b.y,y:a.z*b.x-a.x*b.z,z:a.x*b.y-a.y*b.x});
const length=(a:Vec3)=>Math.hypot(a.x,a.y,a.z);
const normalize=(a:Vec3):Vec3=>{const magnitude=length(a);return magnitude>1e-12?multiply(a,1/magnitude):{x:0,y:0,z:0};};
