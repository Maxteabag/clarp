import {it,expect} from 'vitest';
import fs from 'node:fs';
const modules={};function load(name){if(modules[name])return modules[name].exports;const m={exports:{}};modules[name]=m;new Function('exports','module','require',fs.readFileSync(new URL('../../static/viz-flow/'+name,import.meta.url),'utf8'))(m.exports,m,p=>load(p.replace('./','')));return m.exports;}
const {build}=load('model.js');
export function fixture(){
 const entities=[{id:'repo',kind:'repository',path:'/work/clarp',label:'Clarp'},{id:'ios',kind:'repository',path:'/work/clarp-ios',label:'iOS'}];
 const paths=['desktop/main.cpp','server/api.py','README.md'];
 paths.forEach((path,i)=>entities.push({id:'f'+i,kind:'file',parent:'repo',path:'/work/clarp/'+path,label:path.split('/').at(-1)}));
 entities.push({id:'swift',kind:'file',parent:'ios',path:'/work/clarp-ios/App.swift',label:'App.swift'});
 const events=['f0','f1','f2','swift'].map((id,i)=>({id:'e'+i,agent_id:'a'+i,agent:'Agent '+i,ts:1000,world_target:id,workspace_target:i===3?'ios':'repo',action:'edit',evidence:{path:entities.find(e=>e.id===id).path}}));
 return {entities,events,relations:[{from:'repo',to:'github:Maxteabag/clarp',kind:'remote',label:'origin'},{from:'ios',to:'github:Maxteabag/clarp-ios',kind:'remote',label:'origin'}]};
}
it('keeps components inside repositories, root files outside components, and iOS separate',()=>{
 const m=build(fixture(),null,2000);expect(m.projects).toHaveLength(1);
 expect(m.regions.map(r=>r.id).sort()).toEqual(['ios','repo']);
 expect(m.componentAreas.map(a=>a.name)).toEqual(['desktop','server']);
 const repo=m.regionMap.get('repo');for(const area of m.componentAreas){expect(area.x-area.w/2).toBeGreaterThan(repo.x-repo.rx);expect(area.y+area.h/2).toBeLessThan(repo.y+repo.ry);}
 const root=m.files.find(f=>f.id==='f2');expect(root.component).toBeNull();expect(root.y).toBeLessThan(repo.y+repo.ry);
 expect(m.componentAreas.some(a=>Math.abs(root.x-a.x)<a.w/2&&Math.abs(root.y-a.y)<a.h/2)).toBe(false);
 expect(m.actorMap.get('a0').component).toContain('desktop');expect(m.actorMap.get('a1').component).toContain('server');
});
it('withholds future component membership and does not group unrelated remotes by name',()=>{
 const s=fixture();s.events[1].ts=3000;s.relations=[];const m=build(s,null,2000);
 expect(m.componentAreas.map(a=>a.name)).toEqual(['desktop']);expect(m.projects).toHaveLength(2);
});
