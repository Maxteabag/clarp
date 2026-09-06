import {it,expect} from 'vitest';
import fs from 'node:fs';
const module={exports:{}};
new Function('exports','module',fs.readFileSync(new URL('../../static/viz-world/model.js',import.meta.url),'utf8'))(module.exports,module);
const {build,stateAt}=module.exports;
it('reconstructs running, success and failure at the chosen replay time',()=>{
 expect(stateAt({finished_at:200,outcome:'succeeded'},150)).toBe('running');
 expect(stateAt({finished_at:200,outcome:'succeeded'},250)).toBe('succeeded');
 expect(stateAt({finished_at:200,outcome:'failed'},250)).toBe('failed');
 expect(stateAt({finished_at:null,outcome:'unknown'},250)).toBe('unknown');
});
it('places real files within their checkout and has no action-category destinations',()=>{
 const scene={entities:[{id:'r',label:'Clarp',kind:'repository',path:'/repo'},
 {id:'d',label:'server',kind:'directory',parent:'r',path:'/repo/server'},
 {id:'f',label:'api.py',kind:'file',parent:'d',path:'/repo/server/api.py'}],events:[{id:1,ts:1,world_target:'f'}]};
 const m=build(scene),file=m.places.get('f'),repo=m.places.get('r');
 expect(file.x).toBeGreaterThan(repo.x);expect(file.y).toBeGreaterThan(repo.y);
 expect(file.x+file.w).toBeLessThan(repo.x+repo.w);expect(file.y+file.h).toBeLessThan(repo.y+repo.h);
 expect([...m.places.keys()].some(x=>x.startsWith('station:'))).toBe(false);
 expect(m.resolve({world_target:'missing'}).id).toBe('unlocated');
});
