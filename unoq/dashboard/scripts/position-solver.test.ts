import assert from 'node:assert/strict';
import test from 'node:test';

import { projectBoardPoint, solvePositions, type Vec3 } from '../src/lib/positions.ts';

const truth:Vec3[]=[
  {x:0,y:0,z:0},
  {x:3,y:0,z:0},
  {x:1,y:2,z:0},
  {x:.5,y:.5,z:2},
  {x:2,y:1,z:1},
];

const distance=(a:Vec3,b:Vec3)=>Math.hypot(a.x-b.x,a.y-b.y,a.z-b.z);
const ranges=(scale=1)=>truth.flatMap((a,i)=>truth.slice(i+1).map((b,offset)=>({a:i,b:i+offset+1,distance:distance(a,b)*scale})));

test('recovers exact geometry at different physical scales',()=>{
  for(const scale of [.1,1,10]){
    const solution=solvePositions(5,ranges(scale),[0,1,2],3);
    assert.equal(solution.status,'SOLVED');
    assert.equal(solution.rank,9);
    assert.equal(solution.converged,true);
    assert.ok(solution.rmse<scale*1e-8,`scale ${scale} RMSE ${solution.rmse}`);
    assert.ok(solution.positions[1].x>0);
    assert.ok(solution.positions[2].y>0);
    assert.ok(solution.positions[3].z>0);
  }
});

test('fits noisy complete geometry without losing observability',()=>{
  const noisy=ranges().map((edge,index)=>({...edge,distance:edge.distance+(index%2?-.004:.006)}));
  const solution=solvePositions(5,noisy,[0,1,2],3);
  assert.equal(solution.rank,9);
  assert.equal(solution.converged,true);
  assert.ok(solution.rmse<.01,`RMSE ${solution.rmse}`);
});

test('solves a locally observable matrix with one missing edge',()=>{
  const incomplete=ranges().filter((edge)=>!(edge.a===3&&edge.b===4));
  const solution=solvePositions(5,incomplete,[0,1,2],3);
  assert.equal(solution.rank,9);
  assert.equal(solution.converged,true);
  assert.ok(solution.rmse<1e-7,`RMSE ${solution.rmse}`);
});

test('fits the live review snapshot to millimetre residuals',()=>{
  const snapshot=[2.515,1.608,1.353,2.453,1.688,1.963,2.463,.986,3.073,3.119];
  let index=0;
  const measurements=truth.flatMap((_,a)=>truth.slice(a+1).map((__,offset)=>({a,b:a+offset+1,distance:snapshot[index++]})));
  const solution=solvePositions(5,measurements,[0,1,2],3);
  assert.equal(solution.status,'SOLVED');
  assert.ok(solution.rmse<.01,`RMSE ${solution.rmse}`);
});

test('reports an invalid frame below four nodes without extending positions',()=>{
  const solution=solvePositions(3,[],[0,1,2],3);
  assert.equal(solution.status,'NEED 4 NODES');
  assert.equal(solution.positions.length,3);
});

test('canonical dashboard cameras render +X right and +Y up',()=>{
  const center={x:0,y:0,z:0};
  for(const pitch of [-Math.PI/2,-.65]){
    const x=projectBoardPoint({x:1,y:0,z:0},center,1,0,pitch,1);
    const y=projectBoardPoint({x:0,y:1,z:0},center,1,0,pitch,1);
    assert.ok(x.nx>0&&Math.abs(x.ny)<1e-12);
    assert.ok(Math.abs(y.nx)<1e-12&&y.ny>0);
  }
});
