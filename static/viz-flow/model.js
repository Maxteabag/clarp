// One overall map. Selection never changes scale, coverage or placement.
exports.hash=s=>{let h=2166136261;for(const c of String(s))h=Math.imul(h^c.charCodeAt(0),16777619);return h>>>0;};
exports.stateAt=(e,t)=>e.finished_at!=null&&t>=e.finished_at?(e.outcome||'unknown'):(e.finished_at!=null||e.outcome==='running'?'running':'unknown');
const slots=new Map();
exports.build=(scene,_selection,time)=>{
 const byId=new Map(scene.entities.map(e=>[e.id,e])),events=scene.events.filter(e=>e.ts<=time).sort((a,b)=>a.ts-b.ts);
 const ancestor=id=>{let e=byId.get(id);const seen=new Set();while(e&&!seen.has(e.id)){if(e.kind==='repository')return e.id;seen.add(e.id);e=byId.get(e.parent);}return null;};
 const history=new Map(),latest=new Map();
 for(const e of events){const h=history.get(e.agent_id)||[];h.push(e);history.set(e.agent_id,h);for(const id of e.world_targets||[e.world_target])latest.set(id,e);}
 const repos=scene.entities.filter(e=>e.kind==='repository').map(e=>({...e})).sort((a,b)=>a.id.localeCompare(b.id));
 const groupFor=e=>e.workspace_target||ancestor(e.world_target)||(e.evidence?.cwd?'context:'+e.evidence.cwd:'unlocated');
 for(const h of history.values()){
  const e=h.at(-1),id=groupFor(e);if(repos.some(r=>r.id===id))continue;
  repos.push({id,label:id==='unlocated'?'Location unknown':(e.evidence?.cwd||id).split('/').filter(Boolean).at(-1)||'/',path:e.evidence?.cwd,kind:id==='unlocated'?'unresolved':'workspace'});
 }
 // Equal elliptical regions pack around related checkouts; a stable slot is
 // retained as new observations arrive, rather than promoting one workspace.
 const regions=[];
 for(const r of repos){
  let p=slots.get(r.id);
  if(!p){
   const family=regions.find(x=>x.label===r.label);const anchor=family||{x:0,y:0};let best=null;
   const extent=Math.ceil(Math.sqrt(repos.length))+2;
   for(let row=-extent;row<=extent;row++)for(let col=-extent;col<=extent;col++){
    const x=(col+(Math.abs(row)%2)*.5)*520,y=row*340;
    if([...slots.values()].some(s=>((s.x-x)/520)**2+((s.y-y)/380)**2<.99))continue;
    const score=((x-anchor.x)/520)**2+((y-anchor.y)/380)**2+Math.hypot(x,y)*.0001;
    if(!best||score<best.score)best={x,y,score};
   }
   p=best||{x:regions.length*540,y:0};slots.set(r.id,p);
  }
  regions.push({...r,x:p.x,y:p.y,rx:235,ry:165});
 }
 const regionMap=new Map(regions.map(r=>[r.id,r]));const actors=[];
 for(const [id,h] of history){const e=h.at(-1);actors.push({id,name:e.agent,event:e,history:h,workspace:groupFor(e)});}
 actors.sort((a,b)=>a.id.localeCompare(b.id));
 const occupancy=new Map(),counts=new Map();for(const a of actors)counts.set(a.workspace,(counts.get(a.workspace)||0)+1);
 for(const a of actors){const r=regionMap.get(a.workspace),i=occupancy.get(r.id)||0;occupancy.set(r.id,i+1);const n=counts.get(r.id);a.x=n>6?r.x-95+Math.cos(i/n*6.28)*95:r.x-150+(i%3)*65;a.y=n>6?r.y+Math.sin(i/n*6.28)*105:r.y-35+Math.floor(i/3)*78;}
 const fileAnchors=[[65,-65],[160,-38],[65,58],[160,83]],files=[];
 for(const region of regions){
  const candidates=scene.entities.filter(e=>e.kind==='file'&&ancestor(e.id)===region.id);
  candidates.sort((a,b)=>(latest.get(b.id)?.ts||0)-(latest.get(a.id)?.ts||0)||a.id.localeCompare(b.id));
  region.totalFiles=candidates.length;
  candidates.slice(0,4).sort((a,b)=>String(a.parent).localeCompare(String(b.parent))||a.id.localeCompare(b.id)).forEach((e,i)=>files.push({...e,workspace:region.id,x:region.x+fileAnchors[i][0],y:region.y+fileAnchors[i][1],event:latest.get(e.id)}));
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
 return {regions,regionMap,repos,actors,files,fileMap:new Map(files.map(f=>[f.id,f])),ownerGroups,remoteMap,structural,history,latest,
  bounds:{x:minX,y:minY,w:maxX-minX,h:maxY-minY},github:{x:right+140,y:-205,w:360,h:ownerY+230}};
};
