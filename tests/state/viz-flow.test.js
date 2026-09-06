import {it,expect} from 'vitest';
import fs from 'node:fs';
import {FlowMemory} from '../../static/lib/viz-flow-memory.js';
import {flowDemo} from '../../static/lib/viz-flow-demo.js';
const m={exports:{}};new Function('exports','module',fs.readFileSync(new URL('../../static/viz-flow/model.js',import.meta.url),'utf8'))(m.exports,m);
const {build}=m.exports;
it('encapsulates every remote in its owner and leaves input evidence unchanged',()=>{
 const scene=flowDemo(0);scene.entities.push({id:'github:second',label:'Second owner',kind:'organization',parent:'github'},
 {id:'other',label:'other-repo',kind:'remote-repository',parent:'github:second'});
 const before=JSON.stringify(scene);const result=build(scene,'demo:checkout',3000);
 expect(result.ownerGroups.map(o=>[o.id,o.repos.map(r=>r.id)])).toEqual([['github:example-owner',['demo:remote']],['github:second',['other']]]);
 for(const owner of result.ownerGroups)for(const repo of owner.repos){expect(repo.x).toBeGreaterThan(owner.x);expect(repo.y+40).toBeLessThan(owner.y+owner.h);}
 expect(JSON.stringify(scene)).toBe(before);
});
it('keeps avatars settled across nearby read and edit operations',()=>{
 const scene=flowDemo(0),a=build(scene,'demo:checkout',3000),b=build(scene,'demo:checkout',7000);
 const pos=x=>x.actors.find(a=>a.id==='demo-builder');
 expect([pos(a).x,pos(a).y]).toEqual([pos(b).x,pos(b).y]);
 expect(pos(a).event.action).toBe('read');expect(pos(b).event.action).toBe('edit');
});
it('persists only observed structural relations and deduplicates touches across refresh',()=>{
 const values=new Map(),storage={getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v)};
 const scene=flowDemo(0);scene.events=scene.events.slice(0,2);
 const memory=new FlowMemory(storage);memory.update(scene);const next=memory.update(scene);
 expect(next.flowMemory.touches.map(t=>t.count)).toEqual([1,1]);
 const restored=new FlowMemory(storage).update({host:scene.host,entities:[],relations:[],events:[]});
 expect(restored.relations).toHaveLength(1);expect(restored.entities.some(e=>e.id==='demo:remote'&&e.historical)).toBe(true);
 expect(new FlowMemory(storage).update({host:'Other Host',entities:[],relations:[],events:[]}).relations).toEqual([]);
});
it('has distinct successful and failed outcomes in the local demonstration',()=>{
 const scene=flowDemo(1000);expect(scene.host).toBe('Workflow demo');
 expect(scene.events.filter(e=>e.action==='test').map(e=>e.outcome)).toEqual(['failed','succeeded']);
 expect(scene.events.find(e=>e.action==='push').remote_target).toBe('demo:remote');
});
it('shows all workspaces and agents without selection-driven layout',()=>{
 const scene=flowDemo(0);scene.entities.push({id:'other',label:'Other',kind:'repository',path:'/other'});
 for(let i=0;i<8;i++)scene.events.push({...scene.events[0],id:'extra'+i,agent_id:'agent'+i,agent:'Agent '+i,world_target:'other',workspace_target:'other'});
 const a=build(scene,null,7000),b=build(scene,'other',7000);
 expect(a.regions.map(r=>r.id).sort()).toEqual(['demo:checkout','demo:service','other']);
 expect(a.actors.length).toBe(10);
 expect(a.regions.map(r=>[r.x,r.y])).toEqual(b.regions.map(r=>[r.x,r.y]));
 expect(a.actors.map(r=>[r.x,r.y])).toEqual(b.actors.map(r=>[r.x,r.y]));
});

it('groups verified worktrees into projects without merging names or scaling by selection',()=>{
 const scene=flowDemo(0);scene.entities[0].project_id='shared-git';scene.entities[0].main_path=scene.entities[0].path;
 scene.entities.push({id:'copy',label:'Workflow example',kind:'repository',path:'/worktrees/feature',project_id:'shared-git',is_worktree:true},
 {id:'unrelated',label:'Workflow example',kind:'repository',path:'/unrelated'});
 const m=build(scene,null,7000),project=m.projects.find(p=>p.id==='shared-git');
 expect(project.members.map(r=>r.id).sort()).toEqual(['copy','demo:checkout']);expect(m.projects).toHaveLength(3);
 expect(m.regionMap.get('copy').rx).toBeLessThan(m.regionMap.get('demo:checkout').rx);
});
