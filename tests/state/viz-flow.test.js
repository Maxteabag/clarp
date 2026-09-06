import {it,expect} from 'vitest';
import fs from 'node:fs';
import {FlowMemory} from '../../static/lib/viz-flow-memory.js';
import {flowDemo} from '../../static/lib/viz-flow-demo.js';
// Load the CommonJS Flow modules the way the sandbox worker does.
const modules={};
function load(name){
 if(modules[name])return modules[name].exports;
 const m={exports:{}};modules[name]=m;
 new Function('exports','module','require',fs.readFileSync(new URL('../../static/viz-flow/'+name,import.meta.url),'utf8'))(m.exports,m,p=>load(p.replace('./','')));
 return m.exports;
}
const {build}=load('model.js');const work=load('work.js');
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

it('follows one work object from declared intent through attributed evidence to a recorded outcome',()=>{
 const scene=flowDemo(0),plan=scene.work.plans[0].id;
 const at=s=>build(scene,null,s*1000).slates.find(o=>o.id===plan);
 expect(at(2).stage).toBe('intent');                       // reads alone keep the ticket hollow
 expect(at(7).stage).toBe('evidence');expect(at(7).evidence.files).toEqual(['demo:source']);
 expect(at(15).evidence.validation.state).toBe('failed');  // an interruption stays…
 expect(at(19).evidence.validation.state).toBe('failed');
 expect(at(25).evidence.validation.state).toBe('recovered');// …until an evidenced rerun succeeds
 const done=at(39);expect(done.stage).toBe('outcome');expect(done.outcome.id).toBe('demo:artifact-video');expect(done.outcome.preview).toBe(true);
 expect(done.remoteRuns.map(r=>r.conclusion)).toEqual(['failure']);
 expect(at(44).handoffs.map(h=>[h.fromName,h.toName])).toEqual([['Builder','Reviewer']]);
 expect(at(44).workspace).toBe('demo:checkout');
});
it('shows outcomes without a plan as outcome-only slates and never claims a preview it lacks',()=>{
 const scene=flowDemo(0),m=build(scene,null,31000),report=m.slates.find(o=>o.id==='artifact:demo:artifact-report');
 expect(report.stage).toBe('outcome');expect(report.intent.none).toBe(true);expect(report.outcome.preview).toBe(false);expect(report.outcome.source_count).toBe(5);
 expect(m.work.threads).toEqual([]);
 expect(build(scene,null,44000).threads.map(t=>t.plan)).toEqual([scene.work.plans[0].id]);
});
it('keeps compound-script validation outcomes inexact and attributes nothing outside the plan window',()=>{
 const runs=[{ts:1,finished_at:2,outcome:'failed',action:'execute',evidence:{validation:'test',validation_exact:false}},
  {ts:3,finished_at:4,outcome:'succeeded',action:'execute',evidence:{validation:'test',validation_exact:false}}];
 expect(work.validationState(runs,10).state).toBe('failed');  // an inexact success cannot seal a failure
 expect(work.validationState([{...runs[1],evidence:{validation:'test',validation_exact:true}},runs[0]],10).state).toBe('recovered'); // order of arrival does not matter
 expect(work.validationState([runs[0],{...runs[1],evidence:{validation:'test',validation_exact:true}}],10).state).toBe('recovered');
 const scene=flowDemo(0);scene.events.push({...scene.events[2],id:'late',ts:scene.work.plans[0].completed_at+600000,finished_at:scene.work.plans[0].completed_at+600001});
 const m=build(scene,null,scene.work.plans[0].completed_at+700000);
 expect(m.work.claimed.has('late')).toBe(false);
});
