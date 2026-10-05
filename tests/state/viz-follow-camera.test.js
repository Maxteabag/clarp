import {describe,it,expect} from 'vitest';
import {FollowCamera,cameraForBounds,easeCamera} from '../../static/lib/viz-follow-camera.js';
const meta={bounds:{x:0,y:0,w:3000,h:2000},hits:[{id:'file',x:100,y:100,w:30,h:30}],agents:[{id:'a',x:140,y:140}]};
const event=(ts=10000)=>({ts,agent_id:'a',world_target:'file'});
describe('activity camera',()=>{
 it('is opt-in and ignores old or future activity',()=>{
  const f=new FollowCamera();expect(f.update({events:[event()],meta,playhead:10000,now:0})).toBeNull();
  f.enabled=true;expect(f.update({events:[event(50000)],meta,playhead:10000,now:0})).toEqual(meta.bounds);
  expect(f.update({events:[event()],meta,playhead:50000,now:0})).toEqual(meta.bounds);
 });
 it('follows fresh activity then widens even under continuous traffic',()=>{
  const f=new FollowCamera();f.enabled=true;
  expect(f.update({events:[event()],meta,playhead:10000,now:0}).w).toBeLessThan(meta.bounds.w);
  f.update({events:[event(14000)],meta,playhead:14000,now:4000});
  expect(f.update({events:[event(17000)],meta,playhead:17000,now:7000})).toEqual(meta.bounds);
  expect(f.update({events:[event(20000)],meta,playhead:20000,now:10000})).toEqual(meta.bounds);
  expect(f.update({events:[event(23000)],meta,playhead:23000,now:13000}).w).toBeLessThan(meta.bounds.w);
 });
 it('does not refocus on identical polling or future completion timestamps',()=>{
  const f=new FollowCamera();f.enabled=true;const events=[{...event(),finished_at:40000}];
  f.update({events,meta,playhead:10000,now:0});
  expect(f.update({events,meta,playhead:25000,now:15000})).toEqual(meta.bounds);
  expect(f.update({events,meta,playhead:40000,now:30000}).w).toBeLessThan(meta.bounds.w);
 });
 it('includes simultaneous distant activity and tolerates missing positions',()=>{
  const f=new FollowCamera();f.enabled=true;
  const m={...meta,hits:[...meta.hits,{id:'far',x:1800,y:100,w:100,h:100}]};
  const b=f.update({events:[event(),{...event(),world_target:'far'}],meta:m,playhead:10000,now:0});
  expect(b.x+b.w).toBeGreaterThanOrEqual(1900);
  f.reset();expect(f.update({events:[event()],meta:{bounds:meta.bounds},playhead:10000,now:0})).toEqual(meta.bounds);
 });
 it('fits inside controls, caps zoom and eases without overshoot',()=>{
  const t=cameraForBounds(meta.bounds,{width:390,top:180,bottom:700});
  expect(t.x).toBeGreaterThanOrEqual(0);expect(t.y).toBeGreaterThanOrEqual(180);
  expect(t.y+2000*t.k).toBeLessThanOrEqual(700);
  expect(cameraForBounds({x:0,y:0,w:1,h:1},{width:1600,top:100,bottom:900}).k).toBe(1.25);
  const c={x:0,y:0,k:.1},next=easeCamera(c,t,16);
  expect(next.k).toBeGreaterThan(c.k);expect(next.k).toBeLessThan(t.k);
  expect(easeCamera(c,t,16,true)).toEqual(t);
 });
});
