import {it,expect} from 'vitest';
import {activityHeat,HEAT_WINDOW,densityGrid,heatColor,HEAT_SIGMA} from '../../static/lib/viz-heatmap.js';
const hits=[{id:'file',x:0,y:0,w:40,h:40},{id:'other',x:1000,y:0,w:40,h:40}];
const event=(id,ts=1000)=>({id,ts,agent_id:'a',world_target:'file'});
it('gets warmer with repeated events and cools without renormalizing',()=>{
 const single=activityHeat([event('a')],hits,1000),many=activityHeat([event('a'),event('b')],hits,1000);
 expect(many.spots[0].weight).toBeGreaterThan(single.spots[0].weight);
 expect(activityHeat([event('a')],hits,181000).spots[0].weight).toBeLessThan(single.spots[0].weight);
 expect(activityHeat([event('a')],hits,1000+HEAT_WINDOW).spots).toEqual([]);
});
const grid=spots=>densityGrid(spots,{x:0,y:0,k:1},300,240);
const at=(g,x,y)=>g.density[Math.round(y/g.cell+g.pad)*g.cols+Math.round(x/g.cell+g.pad)];
it('matches the Gaussian profile and adds overlapping density before coloring',()=>{
 const a=grid([{x:120,y:120,weight:1}]),b=grid([{x:120,y:120,weight:2}]);
 expect(at(a,120,120)).toBeCloseTo(1,5);
 expect(at(a,120+HEAT_SIGMA,120)).toBeCloseTo(Math.exp(-.5),5);
 for(let i=0;i<a.density.length;i++)expect(b.density[i]).toBeCloseTo(2*a.density[i],5);
 expect(heatColor(at(b,120,120))).not.toEqual(heatColor(at(a,120,120)));
});
it('does not introduce the old 80-unit bin boundary color discontinuity',()=>{
 const a=grid([{x:38,y:120,weight:1},{x:39,y:120,weight:1}]);
 const b=grid([{x:39,y:120,weight:1},{x:41,y:120,weight:1}]);
 const peak=g=>Math.max(...g.density);
 expect(Math.abs(peak(a)-peak(b))/peak(a)).toBeLessThan(.02);
});
it('uses the same density and color domain across pixel ratios and pans',()=>{
 const points=[{x:120,y:120,weight:3}];const a=grid(points);
 const retina=densityGrid(points,{x:0,y:0,k:2},600,480,2);
 expect(retina.density).toEqual(a.density);
 const panned=densityGrid(points,{x:24,y:0,k:1},300,240);
 expect(at(panned,144,120)).toBeCloseTo(at(a,120,120),5);
 const zoomed=densityGrid(points,{x:0,y:0,k:2},600,480);
 expect(at(zoomed,240,240)).toBeCloseTo(at(a,120,120),5);
});
it('keeps contributions at viewport edges and retains more than 128 locations',()=>{
 const edge=grid([{x:-6,y:120,weight:1}]);expect(at(edge,0,120)).toBeCloseTo(Math.exp(-.5*(6/HEAT_SIGMA)**2),5);
 const hs=Array.from({length:160},(_,i)=>({id:String(i),x:i*100,y:0,w:10,h:10}));
 const es=hs.map(h=>({...event(h.id),world_target:h.id}));
 expect(activityHeat(es,hs,1000).spots).toHaveLength(160);
 const huge=densityGrid([],{x:0,y:0,k:1},8000,5000);expect(huge.density.length).toBeLessThan(90000);
});
it('color varies continuously on a fixed scale and fades to transparent',()=>{
 expect(heatColor(0)[3]).toBe(0);expect(heatColor(2)[3]).toBeGreaterThan(heatColor(1)[3]);
 const below=heatColor(3.99),above=heatColor(4.01);
 for(let i=0;i<4;i++)expect(Math.abs(below[i]-above[i])).toBeLessThan(3);
 expect(heatColor(100)).toEqual(heatColor(8));
});
it('deduplicates records and excludes future replay events',()=>{
 const e=event('same');expect(activityHeat([e,e,event('future',2000)],hits,1000).located).toBe(1);
 expect(activityHeat([e,e],hits,1000)).toEqual(activityHeat([e],hits,1000));
});
it('does not invent positions for missing targets or use current agent locations',()=>{
 const e={...event('a'),world_target:'unknown'};
 const result=activityHeat([e],[...hits,{id:'agent:a',x:100,y:100,w:10,h:10}],1000);
 expect(result.spots).toEqual([]);expect(result.unlocated).toBe(1);
 expect(activityHeat([{...e,workspace_target:'file'}],hits,1000).located).toBe(1);
});
it('splits one multi-target event without multiplying its total weight',()=>{
 const result=activityHeat([{...event('a'),world_targets:['file','other','file']}],hits,1000);
 expect(result.spots).toHaveLength(2);expect(result.spots.reduce((n,s)=>n+s.weight,0)).toBeCloseTo(1);
});
