// Evidence supplies relationships; the visual composition is a revisable interpretation.
exports.hash=s=>{let h=2166136261;for(const c of String(s))h=Math.imul(h^c.charCodeAt(0),16777619);return h>>>0;};
exports.stateAt=(e,t)=>e.finished_at!=null&&t>=e.finished_at?(e.outcome||'unknown'):(e.finished_at!=null||e.outcome==='running'?'running':'unknown');
const slots=new Map();
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
   local.push({...r,x,y,rx:core?190:142+Math.min(3,n)*7,ry:core?143:112,project:g.id,core,
    displayName:core?'':r.path?.split('/').filter(Boolean).at(-1)||r.label});
  }
  const extent={left:Math.min(-110,...local.map(r=>r.x-r.rx-30)),right:Math.max(110,...local.map(r=>r.x+r.rx+30)),
   top:Math.min(-110,...local.map(r=>r.y-r.ry-50)),bottom:Math.max(110,...local.map(r=>r.y+r.ry+35))};
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
 const fileAnchors=[[55,-30],[105,28],[38,69]],files=[];
 for(const region of regions){
  const candidates=scene.entities.filter(e=>e.kind==='file'&&ancestor(e.id)===region.id);
  candidates.sort((a,b)=>(latest.get(b.id)?.ts||0)-(latest.get(a.id)?.ts||0)||a.id.localeCompare(b.id));
  region.totalFiles=candidates.length;
  candidates.slice(0,3).forEach((e,i)=>files.push({...e,workspace:region.id,x:region.x+fileAnchors[i][0],y:region.y+fileAnchors[i][1],event:latest.get(e.id)}));
 }
 const right=Math.max(0,...regions.map(r=>r.x+r.rx));
 const owners=scene.entities.filter(e=>e.kind==='organization'&&e.parent==='github').sort((a,b)=>a.id.localeCompare(b.id));
 let ownerY=-120;const ownerGroups=[];
 for(const o of owners){const children=scene.entities.filter(e=>e.parent===o.id&&e.kind==='remote-repository').sort((a,b)=>a.id.localeCompare(b.id)).map(r=>({...r}));
  const group={...o,x:right+170,y:ownerY,w:300,h:Math.max(170,65+Math.ceil(children.length/3)*75),repos:children};ownerY+=group.h+28;
  children.forEach((r,i)=>{r.x=group.x+50+(i%3)*95;r.y=group.y+85+Math.floor(i/3)*75;});ownerGroups.push(group);
 }
 const remoteMap=new Map(ownerGroups.flatMap(o=>o.repos.map(r=>[r.id,r])));
 const structural=(scene.relations||[]).filter(r=>regionMap.has(r.from)&&remoteMap.has(r.to));
 const minX=Math.min(-280,...regions.map(r=>r.x-r.rx-40)),minY=Math.min(-240,...regions.map(r=>r.y-r.ry-60));
 const maxX=Math.max(right+60,...ownerGroups.map(o=>o.x+o.w+55)),maxY=Math.max(240,ownerY+20,...regions.map(r=>r.y+r.ry+65));
 return {projects,regions,regionMap,repos,actors,files,fileMap:new Map(files.map(f=>[f.id,f])),ownerGroups,remoteMap,structural,history,latest,
  bounds:{x:minX,y:minY,w:maxX-minX,h:maxY-minY},github:{x:right+140,y:-205,w:360,h:ownerY+230}};
};
