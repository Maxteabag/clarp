// A compact focus lens over the full world, not a second inventory of its files.
exports.hash=s=>{let h=2166136261;for(const c of String(s))h=Math.imul(h^c.charCodeAt(0),16777619);return h>>>0;};
exports.stateAt=(e,t)=>e.finished_at!=null&&t>=e.finished_at?(e.outcome||'unknown'):(e.finished_at!=null||e.outcome==='running'?'running':'unknown');
exports.build=(scene,focus,time)=>{
 const byId=new Map(scene.entities.map(e=>[e.id,e])),events=scene.events.filter(e=>e.ts<=time).sort((a,b)=>a.ts-b.ts);
 const repos=scene.entities.filter(e=>e.kind==='repository').sort((a,b)=>a.id.localeCompare(b.id));
 const ancestor=id=>{let e=byId.get(id);const seen=new Set();while(e&&!seen.has(e.id)){if(e.kind==='repository')return e.id;seen.add(e.id);e=byId.get(e.parent);}return null;};
 const scores=new Map();
 for(const e of events){const id=e.workspace_target||ancestor(e.world_target);if(!id)continue;
  const age=Math.max(0,time-e.ts),weight=['edit','write','create','delete','commit','push','test'].includes(e.action)?5:1;
  scores.set(id,(scores.get(id)||0)+weight*Math.exp(-age/180000));}
 const lastByAgent=new Map(events.map(e=>[e.agent_id,e]));
 const occupied=new Set([...lastByAgent.values()].map(e=>e.workspace_target||ancestor(e.world_target)));
 const current=repos.filter(r=>occupied.has(r.id));
 const concrete=current.filter(r=>scene.entities.some(e=>e.kind==='file'&&ancestor(e.id)===r.id));
 const chosen=byId.get(focus)||(concrete.length?concrete:current.length?current:repos).slice().sort((a,b)=>(scores.get(b.id)||0)-(scores.get(a.id)||0))[0];
 const inFocus=e=>chosen&&(e.workspace_target===chosen.id||ancestor(e.world_target)===chosen.id);
 const relevant=events.filter(inFocus),latest=new Map(),history=new Map();
 for(const e of events){const h=history.get(e.agent_id)||[];h.push(e);history.set(e.agent_id,h);}
 for(const e of relevant)for(const id of e.world_targets||[e.world_target])latest.set(id,e);
 const candidates=scene.entities.filter(e=>e.kind==='file'&&ancestor(e.id)===chosen?.id);
 candidates.sort((a,b)=>{
  const ea=latest.get(a.id),eb=latest.get(b.id);return (eb?.ts||0)-(ea?.ts||0)||a.id.localeCompare(b.id);
 });
 const selected=candidates.slice(0,8).sort((a,b)=>String(a.parent).localeCompare(String(b.parent))||a.id.localeCompare(b.id));
 const sparse=[[760,390],[750,555],[710,660]],four=[[720,325],[880,420],[860,570],[700,655]],dense=[[650,290],[810,315],[925,420],[780,440],[925,565],[770,575],[800,695],[635,650]];
 const anchors=selected.length<4?sparse:selected.length===4?four:dense;
 const files=selected.map((e,i)=>({...e,x:anchors[i][0],y:anchors[i][1],w:106,h:70,event:latest.get(e.id)}));
 const fileMap=new Map(files.map(f=>[f.id,f]));
 const actors=[];
 for(const [id,h] of history){const last=h.at(-1);if(!inFocus(last))continue;
  actors.push({id,name:last.agent,history:h,event:last});}
 actors.sort((a,b)=>a.id.localeCompare(b.id));
 actors.slice(0,5).forEach((a,i)=>{a.x=selected.length?390+(i%2)*125:565+(i%2)*95;a.y=selected.length?345+Math.floor(i/2)*150:430+Math.floor(i/2)*95;});
 const owners=scene.entities.filter(e=>e.kind==='organization'&&e.parent==='github').sort((a,b)=>a.id.localeCompare(b.id));
 let ownerY=260;
 const ownerGroups=owners.map(o=>{
  const children=scene.entities.filter(e=>e.parent===o.id&&e.kind==='remote-repository').sort((a,b)=>a.id.localeCompare(b.id)).map(r=>({...r}));
  const group={...o,x:1130,y:ownerY,w:340,h:Math.max(180,70+Math.ceil(children.length/3)*72),repos:children};
  ownerY+=group.h+25;return group;
 });
 for(const group of ownerGroups)group.repos.forEach((r,i)=>{r.x=group.x+65+(i%3)*100;r.y=group.y+83+Math.floor(i/3)*72;});
 const remoteMap=new Map(ownerGroups.flatMap(o=>o.repos.map(r=>[r.id,r])));
 const structural=(scene.relations||[]).filter(r=>r.from===chosen?.id&&remoteMap.has(r.to));
 const quiet=repos.filter(r=>r.id!==chosen?.id).slice(0,16).map((r,i)=>({...r,x:260+i%8*140,y:835+Math.floor(i/8)*70}));
 return {chosen,repos,files,fileMap,actors:actors.slice(0,5),hiddenAgents:Math.max(0,actors.length-5),
  ownerGroups,remoteMap,structural,quiet,latest,relevant,history,totalFiles:candidates.length,region:{rx:!selected.length?210:selected.length<4?335:405,ry:!selected.length?160:selected.length<4?225:280},
  bounds:{x:120,y:130,w:1430,h:Math.max(860,ownerY+40,quiet.length/8*70+790)}};
};
