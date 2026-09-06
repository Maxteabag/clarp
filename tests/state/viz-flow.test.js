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
 expect(at(37.5).remoteRuns.map(r=>[r.conclusion,r.status])).toEqual([['','active']]); // no conclusion before evidenced completion
 expect(done.outcome.link).toBe('https://example.invalid/demo-comparison.mp4'); // the recorded media, not its thumbnail
 expect(at(44).handoffs.map(h=>[h.fromName,h.toName,h.link])).toEqual([['Builder','Reviewer','transfer']]);
 expect(at(44).workspace).toBe('demo:checkout');
});
it('shows outcomes without a plan as outcome-only slates and never claims a preview it lacks',()=>{
 const scene=flowDemo(0),m=build(scene,null,31000),report=m.slates.find(o=>o.id==='artifact:demo:artifact-report');
 expect(report.stage).toBe('outcome');expect(report.intent.none).toBe(true);expect(report.outcome.preview).toBe(false);expect(report.outcome.source_count).toBe(5);
 expect(m.work.threads).toEqual([]);
 expect(build(scene,null,44000).threads.map(t=>t.plan)).toEqual([scene.work.plans[0].id]);
});
it('scopes validation failures and only lets the same checks recover them',()=>{
 const run=(ts,outcome,scope,commands,extra={})=>({ts,finished_at:ts+1,outcome,action:'execute',evidence:{validation:'test',validation_exact:scope!=='script',validation_scope:scope,validation_commands:commands,...extra}});
 // A single command's failure is that check failing; a chain failure is an interruption with unknown culprit.
 expect(work.validationState([run(1,'failed','single',['npm test'])],10).state).toBe('failed');
 expect(work.validationState([run(1,'failed','chain',['npm run test:a','npm run build'])],10).state).toBe('interrupted');
 // An unrelated passing check never erases the failure.
 expect(work.validationState([run(1,'failed','single',['npm test']),run(3,'succeeded','single',['npm run build'])],10).state).toBe('failed');
 // The same checks passing later do, even across several runs.
 const chain=[run(1,'failed','chain',['npm run test:a','npm run test:b','npm run build']),run(3,'succeeded','chain',['npm run test:b','npm run build']),run(5,'succeeded','single',['npm run test:a'])];
 expect(work.validationState(chain,4).state).toBe('interrupted');expect(work.validationState(chain,4).unresolved).toEqual(['npm run test:a']);
 expect(work.validationState(chain,10).state).toBe('recovered');
 // A ; script's success proves nothing and cannot seal a failure.
 expect(work.validationState([run(1,'failed','single',['npm test']),run(3,'succeeded','script',['npm test'])],10).state).toBe('failed');
 // Attribution stops at the plan window.
 const scene=flowDemo(0);scene.events.push({...scene.events[2],id:'late',ts:scene.work.plans[0].completed_at+600000,finished_at:scene.work.plans[0].completed_at+600001});
 expect(build(scene,null,scene.work.plans[0].completed_at+700000).work.claimed.has('late')).toBe(false);
});
it('treats a message naming a plan as a reference unless an explicit handoff record exists',()=>{
 const scene=flowDemo(0);delete scene.work.messages[0].link;
 const m=build(scene,null,44000);
 expect(m.threads.map(t=>t.link)).toEqual(['reference']);
 expect(m.slates.find(o=>o.id===scene.work.plans[0].id).handoffs.map(h=>h.link)).toEqual(['reference']);
});

it('keeps earlier unresolved failures when another check fails and recovers',()=>{
 const run=(ts,outcome,cmd)=>({ts,finished_at:ts+1,outcome,action:'test',evidence:{validation_scope:'single',validation_commands:[cmd]}});
 const result=work.validationState([run(1,'failed','a'),run(3,'failed','b'),run(5,'succeeded','b')],10);
 expect(result.state).toBe('failed');expect(result.unresolved).toEqual(['a']);
});

it('uses recorded lifecycle for lantern motion and CSS scale for detail',()=>{
 const module={exports:{}};
 new Function('exports','module','require',fs.readFileSync(new URL('../../static/viz-flow/lantern.js',import.meta.url),'utf8'))(module.exports,module,()=>({}));
 const lantern=module.exports;
 const object={stage:'evidence',finished:false,age:0,evidence:{running:false,validation:{state:'none'}}};
 expect(lantern.isRunning(object)).toBe(false);
 expect(lantern.isRunning({...object,evidence:{...object.evidence,running:true}})).toBe(true);
 expect(lantern.detailLevel(.4,1)).toBe(lantern.detailLevel(.8,2));
 expect(lantern.detailLevel(.8,2)).toBe(0);
 expect(lantern.detailLevel(1.6,2)).toBe(1);
});
it('draws waiting only from recorded jobs and decisions, at the playhead, and attributes them by window',()=>{
 const journey=load('journey.js');
 const job={id:'j',agent_id:'demo-builder',agent:'Builder',kind:'github-workflow',boundary:'github',title:'Verify',status:'succeeded',started_at:1000,updated_at:5000,terminal_at:5000,terminal_reason:'',heartbeat_at:4000,heartbeat_timeout_ms:600000};
 expect(journey.waitAt(job,500)).toBeNull();                       // not started yet
 expect(journey.waitAt(job,3000).state).toBe('waiting');            // open at this playhead, whatever the row says now
 expect(journey.waitAt(job,6000).state).toBe('released');
 expect(journey.waitAt({...job,status:'failed',terminal_reason:'heartbeat_expired'},6000).state).toBe('expired');
 expect(journey.waitAt({...job,terminal_at:null,status:'running',heartbeat_at:1000,heartbeat_timeout_ms:500},3000).stale).toBe(true);
 expect(journey.decisionAt({id:'d',agent_id:'x',title:'Ship?',status:'pending',created_at:100,resolved_at:null,blocks_progress:1},200).state).toBe('waiting');
 const scene=flowDemo(0);
 const m36=build(scene,null,36500),ci=m36.waits.find(w=>w.id==='job:demo-job-ci');
 expect(ci.state).toBe('waiting');expect(ci.work).toBe(scene.work.plans[0].id);expect(ci.anchor.kind).toBe('work');
 expect(m36.posts.map(p=>p.boundary).sort()).toEqual(['apple','github']);
 const m50=build(scene,null,50000);
 expect(m50.waits.find(w=>w.id==='job:demo-job-ci').state).toBe('released');
 expect(m50.waits.find(w=>w.id==='job:demo-job-tf').state).toBe('expired');
 expect(m50.waits.find(w=>w.id==='decision:demo-decision').state).toBe('waiting');
 expect(build(scene,null,20000).waits).toEqual([]);                // quiet time draws no waiting
});
it('remembers repeated routes as bounded patterns without counting a record twice',()=>{
 const values=new Map(),storage={getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v)};
 const scene=flowDemo(0);const memory=new FlowMemory(storage);
 memory.update(scene);const again=memory.update(scene);
 const routes=again.flowMemory.routes;
 expect(routes.find(r=>r.kind==='delivery').count).toBe(1);
 expect(routes.find(r=>r.kind==='message'&&r.from==='demo-builder').count).toBe(1);
 expect(routes.filter(r=>r.kind==='wait').map(r=>r.to).sort()).toEqual(['apple','github']);
 const second={...scene,events:scene.events.map(e=>({...e,id:e.id+':2'})),work:{...scene.work,messages:scene.work.messages.map(x=>({...x,id:x.id+':2'})),jobs:scene.work.jobs.map(j=>({...j,id:j.id+':2'}))}};
 expect(memory.update(second).flowMemory.routes.find(r=>r.kind==='delivery').count).toBe(2);
});
