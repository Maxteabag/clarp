// Evidence supplies relationships; the visual composition is a revisable interpretation.
const {hash}=require('./model-util.js');
const work=require('./work.js');
const journey=require('./journey.js');
const lantern=require('./lantern.js');
exports.hash=hash;
exports.stateAt=(e,t)=>e.finished_at!=null&&t>=e.finished_at?(e.outcome||'unknown'):(e.finished_at!=null||e.outcome==='running'?'running':'unknown');
const slots=new Map();
// Where slates hang inside a lobe: lower right, clear of portraits and files.
// Where lanterns stand inside a lobe: the lower right floor, clear of portraits
// and files; a second lantern to the lower left only when few agents work here.
const slateSlots=(region,actors)=>region.core?(actors<=2?[[60,100],[-95,114]]:[[60,100]]):[[30,80]];
exports.build=(scene,_selection,time)=>{
 const byId=new Map(scene.entities.map(e=>[e.id,e])),events=scene.events.filter(e=>e.ts<=time).sort((a,b)=>a.ts-b.ts);
 const ancestor=id=>{let e=byId.get(id);const seen=new Set();while(e&&!seen.has(e.id)){if(e.kind==='repository')return e.id;seen.add(e.id);e=byId.get(e.parent);}return null;};
 const history=new Map(),latest=new Map();
 for(const e of events){const h=history.get(e.agent_id)||[];h.push(e);history.set(e.agent_id,h);for(const id of e.world_targets||[e.world_target])latest.set(id,e);}
 const repos=scene.entities.filter(e=>['repository','service'].includes(e.kind)).map(e=>({...e})).sort((a,b)=>a.id.localeCompare(b.id));
 const groupFor=e=>byId.get(e.world_target)?.kind==='service'?e.world_target:e.workspace_target||ancestor(e.world_target)||(e.evidence?.cwd?'context:'+e.evidence.cwd:'unlocated');
 for(const h of history.values()){
  const e=h.at(-1),id=groupFor(e);if(repos.some(r=>r.id===id))continue;
  repos.push(byId.get(id)?.kind==='service'?{...byId.get(id)}:{id,label:id==='unlocated'?'Location unknown':(e.evidence?.cwd||id).split('/').filter(Boolean).at(-1)||'/',path:e.evidence?.cwd,kind:id==='unlocated'?'unresolved':'workspace'});
 }
 // Work objects are attributed before layout so each lobe knows what it carries.
 const actorWorkspace=agentId=>{const h=history.get(agentId);return h?groupFor(h.at(-1)):null;};
 const assembled=work.assemble(scene,time,history,groupFor,actorWorkspace);
 for(const o of assembled.objects)if(!repos.some(r=>r.id===o.workspace)){
  // A plan whose agent has no located activity in the window still exists; it
  // waits at the agent's known context rather than being invented into a repo.
  const owner=actorWorkspace(o.agent_id);o.workspace=owner&&repos.some(r=>r.id===owner)?owner:'unlocated';
  if(o.workspace==='unlocated'&&!repos.some(r=>r.id==='unlocated'))repos.push({id:'unlocated',label:'Location unknown',kind:'unresolved'});
 }
 const slatesFor=new Map();
 // Active work first, then the most recently active finished work.
 for(const o of assembled.objects.sort((a,b)=>Number(a.finished)-Number(b.finished)||a.age-b.age||a.id.localeCompare(b.id))){const l=slatesFor.get(o.workspace)||[];l.push(o);slatesFor.set(o.workspace,l);}
 const validation=work.workspaceValidation(events,time,groupFor);
 // Shared Git metadata supplies families, not a name-based guess. Each family
 // grows connected working-copy lobes; silhouettes and spacing remain source design.
 const grouped=new Map();
 for(const r of repos){const id=r.project_id||r.id;let g=grouped.get(id);if(!g){g={id,label:r.label,members:[],kind:r.kind};grouped.set(id,g);}g.members.push(r);}
 const projects=[],regions=[];
 for(const g of [...grouped.values()].sort((a,b)=>a.id.localeCompare(b.id))){
  g.members.sort((a,b)=>Number(a.is_worktree)-Number(b.is_worktree)||a.id.localeCompare(b.id));
  const main=g.members.find(r=>r.path===r.main_path)|| (g.members.length===1?g.members[0]:null);
  const satellites=g.members.filter(r=>r!==main);
  const local=[];
  for(const r of g.members){
   const i=satellites.indexOf(r),angle=-.55+i/Math.max(3,satellites.length)*Math.PI*2;
   const core=r===main,distance=core?0:330+Math.floor(i/6)*230;
   const x=Math.cos(angle)*distance,y=Math.sin(angle)*distance*.86;
   const n=[...history.values()].filter(h=>groupFor(h.at(-1))===r.id).length;
   const carries=(slatesFor.get(r.id)||[]).length;
   local.push({...r,x,y,rx:core?190:142+Math.min(3,n)*7,ry:(core?143:112)+(carries?34+(carries>1&&core?12:0):0),project:g.id,core,
    displayName:core?'':r.path?.split('/').filter(Boolean).at(-1)||r.label,carries});
  }
  const extent={left:Math.min(-110,...local.map(r=>r.x-r.rx-30)),right:Math.max(110,...local.map(r=>r.x+r.rx+30)),
   top:Math.min(-110,...local.map(r=>r.y-r.ry-50)),bottom:Math.max(110,...local.map(r=>r.y+r.ry+(r.carries?50:35)))};
  const fits=p=>!projects.some(o=>p.x+extent.right+70>o.x+o.extent.left&&p.x+extent.left-70<o.x+o.extent.right&&p.y+extent.bottom+60>o.y+o.extent.top&&p.y+extent.top-60<o.y+o.extent.bottom);
  let p=slots.get(g.id);
  if(!p||!fits(p)){
   p=null;
   for(let ring=0;!p&&ring<80;ring++)for(let i=0;i<(ring?ring*12:1);i++){
    const a=i/(ring*12||1)*Math.PI*2+.3,candidate={x:Math.cos(a)*ring*170,y:Math.sin(a)*ring*150};
    if(fits(candidate)){p=candidate;break;}
   }
   p=p||{x:projects.length*1200,y:0};slots.set(g.id,p);
  }
  const project={...g,...p,extent};projects.push(project);
  for(const r of local)regions.push({...r,x:r.x+p.x,y:r.y+p.y});
 }
 const regionMap=new Map(regions.map(r=>[r.id,r]));const actors=[];
 for(const [id,h] of history){const e=h.at(-1);actors.push({id,name:e.agent,event:e,history:h,workspace:groupFor(e)});}
 actors.sort((a,b)=>a.id.localeCompare(b.id));
 const occupancy=new Map(),counts=new Map();for(const a of actors)counts.set(a.workspace,(counts.get(a.workspace)||0)+1);
 for(const a of actors){const r=regionMap.get(a.workspace),i=occupancy.get(r.id)||0;occupancy.set(r.id,i+1);const n=counts.get(r.id);
  a.x=r.x-68+(i%2)*65;a.y=r.y-6+Math.floor(i/2)*63;
  if(n>4){a.x=r.x+Math.cos(i/n*6.28)*90;a.y=r.y+Math.sin(i/n*6.28)*60;}
 }
 const actorMap=new Map(actors.map(a=>[a.id,a]));
 const files=[];
 for(const region of regions){
  const anchors=region.core?[[55,-30],[118,-2],[140,-62]]:[[40,-52],[112,-22]];
  const candidates=scene.entities.filter(e=>e.kind==='file'&&ancestor(e.id)===region.id);
  candidates.sort((a,b)=>(latest.get(b.id)?.ts||0)-(latest.get(a.id)?.ts||0)||a.id.localeCompare(b.id));
  region.totalFiles=candidates.length;
  candidates.slice(0,anchors.length).forEach((e,i)=>files.push({...e,workspace:region.id,x:region.x+anchors[i][0],y:region.y+anchors[i][1],event:latest.get(e.id)}));
 }
 // Slates: active work first, then recent finished work; the rest is counted.
 const slates=[];
 for(const region of regions){
  const list=slatesFor.get(region.id)||[],anchors=slateSlots(region,counts.get(region.id)||0);
  region.hiddenWork=Math.max(0,list.length-anchors.length);region.validation=validation.get(region.id)||null;
  list.slice(0,anchors.length).forEach((o,i)=>slates.push({...o,x:region.x+anchors[i][0],y:region.y+anchors[i][1],region:region.id}));
 }
 for(const project of projects)project.character=work.character(assembled.objects,assembled.orphanArtifacts,new Set(regions.filter(r=>r.project===project.id).map(r=>r.id)));
 const right=Math.max(0,...regions.map(r=>r.x+r.rx));
 const owners=scene.entities.filter(e=>e.kind==='organization'&&e.parent==='github').sort((a,b)=>a.id.localeCompare(b.id));
 let ownerY=-120;const ownerGroups=[];
 for(const o of owners){const children=scene.entities.filter(e=>e.parent===o.id&&e.kind==='remote-repository').sort((a,b)=>a.id.localeCompare(b.id)).map(r=>({...r}));
  const group={...o,x:right+170,y:ownerY,w:300,h:Math.max(170,65+Math.ceil(children.length/3)*75),repos:children};ownerY+=group.h+28;
  children.forEach((r,i)=>{r.x=group.x+50+(i%3)*95;r.y=group.y+85+Math.floor(i/3)*75;r.runs=assembled.remoteRuns.filter(run=>run.remote===r.id).slice(-3);});ownerGroups.push(group);
 }
 const remoteMap=new Map(ownerGroups.flatMap(o=>o.repos.map(r=>[r.id,r])));
 const structural=(scene.relations||[]).filter(r=>regionMap.has(r.from)&&remoteMap.has(r.to));
 // Boundary posts: every wait anchors to its lantern, else to its agent, else to
 // the agent's workshop; each workshop gets one post per distinct boundary.
 const slateMap=new Map(slates.map(s=>[s.id,s]));
 const outward=region=>{const project=projects.find(p=>p.id===region.project),siblings=regions.filter(r=>r.project===region.project&&r.id!==region.id);
  if(!project||!siblings.length)return .4;
  if(!region.core)return Math.atan2(region.y-project.y,region.x-project.x);
  const mean=siblings.reduce((a,r)=>({x:a.x+r.x-region.x,y:a.y+r.y-region.y}),{x:0,y:0});return Math.atan2(-mean.y,-mean.x);};
 const posts=[],postMap=new Map();
 for(const wt of assembled.waits){
  const lanternHome=wt.work?slateMap.get(wt.work):null,actor=actorMap.get(wt.agent_id);
  const regionId=lanternHome?lanternHome.region:actor?actor.workspace:null;const region=regionId?regionMap.get(regionId):null;
  if(!region){wt.anchor=null;continue;}
  wt.anchor=lanternHome?{x:lanternHome.x,y:lanternHome.y+lantern.size(lanternHome)[1]/2-12,kind:'work'}:actor?{x:actor.x+22,y:actor.y+10,kind:'agent'}:{x:region.x,y:region.y,kind:'workspace'};
  const key=region.id+'\n'+wt.boundary;
  if(!postMap.has(key)){const list=posts.filter(p=>p.region===region.id);if(list.length>=3){region.hiddenWaits=(region.hiddenWaits||0)+1;wt.anchor=null;continue;}
   const p=journey.posts(region,[...list.map(x=>x.boundary),wt.boundary],outward(region))[list.length];p.waits=[];posts.push(p);postMap.set(key,p);}
  const post=postMap.get(key);if(post.waits.length<4)post.waits.push(wt);else wt.anchor=null;wt.post=post;
 }
 const threads=assembled.threads.filter(th=>actorMap.has(th.from)&&actorMap.has(th.to)&&th.from!==th.to);
 const minX=Math.min(-280,...regions.map(r=>r.x-r.rx-40)),minY=Math.min(-240,...regions.map(r=>r.y-r.ry-60));
 const maxX=Math.max(right+60,...ownerGroups.map(o=>o.x+o.w+55)),maxY=Math.max(240,ownerY+20,...regions.map(r=>r.y+r.ry+65));
 return {projects,regions,regionMap,repos,actors,actorMap,files,fileMap:new Map(files.map(f=>[f.id,f])),ownerGroups,remoteMap,structural,history,latest,
  slates,slateMap,work:assembled,threads,posts,waits:assembled.waits.filter(w=>w.anchor),
  bounds:{x:minX,y:minY,w:maxX-minX,h:maxY-minY},github:{x:right+140,y:-205,w:360,h:ownerY+230}};
};
