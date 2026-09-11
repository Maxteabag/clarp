import {it,expect} from 'vitest';
import {activityHeat,HEAT_WINDOW} from '../../static/lib/viz-heatmap.js';
const hits=[{id:'file',x:0,y:0,w:40,h:40},{id:'other',x:1000,y:0,w:40,h:40}];
const event=(id,ts=1000)=>({id,ts,agent_id:'a',world_target:'file'});
it('gets warmer with repeated events and cools without renormalizing',()=>{
 const single=activityHeat([event('a')],hits,1000),many=activityHeat([event('a'),event('b')],hits,1000);
 expect(many.spots[0].intensity).toBeGreaterThan(single.spots[0].intensity);
 expect(activityHeat([event('a')],hits,181000).spots[0].intensity).toBeLessThan(single.spots[0].intensity);
 expect(activityHeat([event('a')],hits,1000+HEAT_WINDOW).spots).toEqual([]);
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
