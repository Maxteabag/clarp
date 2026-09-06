// Real places are destinations. Actions and outcomes are properties of work.
const hash=s=>{let h=2166136261;for(const ch of String(s))h=Math.imul(h^ch.charCodeAt(0),16777619);return h>>>0;};
exports.hash=hash;
exports.build=scene=>{
 const entities=scene.entities||[],byId=new Map(entities.map(e=>[e.id,e]));
 const events=(scene.events||[]).slice().sort((a,b)=>a.ts-b.ts||String(a.id).localeCompare(String(b.id)));
 const rooms=[],cards=[],places=new Map(),directories=[];
 const ancestor=e=>{let p=e;const seen=new Set();while(p&&!seen.has(p.id)){if(p.kind==='repository')return p;seen.add(p.id);p=byId.get(p.parent);}return null;};
 const repos=entities.filter(e=>e.kind==='repository').sort((a,b)=>a.id.localeCompare(b.id));
 const groups=repos.map(r=>({...r,items:entities.filter(e=>ancestor(e)?.id===r.id&&e.id!==r.id&&e.kind==='file')}));
 const loose=entities.filter(e=>!ancestor(e)&&['file','directory'].includes(e.kind)&&events.some(ev=>ev.world_target===e.id));
 if(loose.length)groups.push({id:'local-paths',label:'Other local workspaces',path:scene.host,kind:'workspace',items:loose});
 const columnY=[160,160,160];
 for(const [index,r] of groups.entries()){
  const col=columnY.indexOf(Math.min(...columnY)),x=60+col*650,y=columnY[col],w=620;
  const buckets=new Map();
  for(const item of r.items){const parent=byId.get(item.parent);const key=parent?.id||r.id;if(!buckets.has(key))buckets.set(key,{parent,items:[]});buckets.get(key).items.push(item);}
  let yy=y+95;
  const room={...r,x,y,w,h:150,color:['#8edbc2','#f3bf80','#aeb9ef','#d7b1e6'][index%4],kind:'repository'};
  rooms.push(room);places.set(r.id,room);
  for(const [key,bucket] of [...buckets].sort(([a],[b])=>a.localeCompare(b))){
   const dh=42+Math.ceil(bucket.items.length/2)*80,dir={id:key,label:bucket.parent?.path?.replace((r.path||'')+'/','')||r.label,
    path:bucket.parent?.path||r.path,x:x+18,y:yy,w:w-36,h:dh,color:room.color,kind:'directory'};
   directories.push(dir);if(key!==r.id)places.set(key,dir);
   bucket.items.sort((a,b)=>a.id.localeCompare(b.id)).forEach((item,i)=>{
    const card={...item,x:x+30+(i%2)*286,y:yy+35+Math.floor(i/2)*80,w:274,h:68,color:room.color};
    cards.push(card);places.set(item.id,card);
   });yy+=dh+14;
  }
  room.h=Math.max(160,yy-y+35);columnY[col]+=room.h+55;
 }
 const remote=entities.filter(e=>e.kind==='remote-repository').sort((a,b)=>a.id.localeCompare(b.id));
 let ry=160;
 for(const r of remote){const room={...r,x:2100,y:ry,w:440,h:155,color:'#c6b2f2',kind:'remote'};rooms.push(room);places.set(r.id,room);ry+=190;}
 // Directories used as execution contexts stay inside their actual checkout.
 // They are not invented files; the precise path remains inspectable.
 for(const e of entities){
  if(places.has(e.id))continue;
  const repo=ancestor(e);const room=repo&&places.get(repo.id);
  if(room){places.set(e.id,{...room,id:e.id,label:e.label,path:e.path,purpose:e.purpose,kind:e.kind});}
 }
 const unlocated={id:'unlocated',label:'Location not recorded',x:60,y:Math.max(...columnY)+10,w:1270,h:110,kind:'unresolved'};
 for(const e of entities)if(!places.has(e.id))places.set(e.id,unlocated);
 for(const b of places.values()){b.cx=b.x+b.w/2;b.cy=b.y+(b.kind==='file'?b.h/2:65);}
 unlocated.cx=unlocated.x+unlocated.w/2;unlocated.cy=unlocated.y+55;
 const resolve=ev=>places.get(ev.world_target)||unlocated;
 return {rooms,cards,directories,places,events,resolve,unlocated,relations:scene.relations||[],
  bounds:{x:0,y:0,w:2600,h:Math.max(ry,unlocated.y+unlocated.h)+60}};
};
exports.stateAt=(event,time)=>{
 if(event.finished_at!=null && time>=event.finished_at)return event.outcome==='succeeded'?'succeeded':event.outcome==='failed'?'failed':'unknown';
 if(event.finished_at!=null||event.outcome==='running')return 'running';
 return 'unknown';
};
