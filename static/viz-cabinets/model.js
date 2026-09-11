const hash=s=>{let h=2166136261;for(const c of String(s))h=Math.imul(h^c.charCodeAt(0),16777619);return h>>>0;};
exports.hash=hash;
exports.action=e=>{const r=String(e.evidence?.raw||'');if(/\bcat\s*>>/.test(r))return 'append';if(/\bctest\s|\bpytest\s/.test(r))return 'test';if(/\bgit\s+push\s/.test(r))return 'push';return String(e.action||e.verb||'unknown').toLowerCase();};
exports.build=scene=>{
 const entities=scene.entities||[],byId=new Map(entities.map(e=>[e.id,e]));
 const events=(scene.events||[]).map((e,i)=>({...e,key:String(e.id??i),ts:Number(e.ts)||0,op:exports.action(e)})).sort((a,b)=>a.ts-b.ts||a.key.localeCompare(b.key));
 const people=new Map();for(const e of events){const id=String(e.agent_id||e.agent||'unknown');if(!people.has(id))people.set(id,{id,name:e.agent||id,events:[]});people.get(id).events.push(e);}
 const agents=[...people.values()].sort((a,b)=>a.id.localeCompare(b.id));agents.forEach((a,i)=>{a.x=570+i*176;a.y=230;a.w=154;a.h=620;});
 const centerWidth=Math.max(720,agents.length*176),remoteX=570+centerWidth+46;
 const repos=entities.filter(e=>e.kind==='repository').sort((a,b)=>String(a.path||a.id).localeCompare(String(b.path||b.id)));
 const owned=e=>{let p=e;const seen=new Set();while(p&&!seen.has(p.id)){seen.add(p.id);if(p.kind==='repository')return p.id;p=byId.get(p.parent);}return null;};
 const locations=new Map(),rooms=[];let ry=212;
 for(const r of repos){const items=entities.filter(e=>e.id!==r.id&&owned(e)===r.id&&(e.kind==='file'||e.kind==='directory')).sort((a,b)=>String(a.path).localeCompare(String(b.path)));const room={...r,x:38,y:ry,w:470,h:Math.max(156,92+items.length*43),items:[]};rooms.push(room);locations.set(r.id,room);items.forEach((e,i)=>{const b={...e,x:62,y:ry+80+i*43,w:422,h:39};room.items.push(b);locations.set(e.id,b);});ry+=room.h+26;}
 const remotes=entities.filter(e=>e.kind==='remote-repository').sort((a,b)=>String(a.id).localeCompare(String(b.id))).map((e,i)=>({...e,x:remoteX,y:230+i*192,w:340,h:164}));for(const r of remotes)locations.set(r.id,r);
 const specimens=entities.filter(e=>e.path&&!owned(e)&&e.kind!=='host').sort((a,b)=>String(a.path).localeCompare(String(b.path)));
 const archiveY=Math.max(ry+28,950),archiveCols=Math.max(2,Math.floor((remoteX+320)/360));
 specimens.forEach((e,i)=>{const b={...e,x:38+(i%archiveCols)*360,y:archiveY+70+Math.floor(i/archiveCols)*88,w:338,h:74,uncertain:true};locations.set(e.id,b);});
 const archive=specimens.map(e=>locations.get(e.id));const bounds={x:0,y:0,w:remoteX+390,h:Math.max(archiveY+160+Math.ceil(specimens.length/archiveCols)*88,1200)};
 return {events,agents,rooms,remotes,locations,archive,archiveY,remoteX,centerWidth,bounds,byId,relations:scene.relations||[],host:scene.host||entities.find(e=>e.kind==='host')?.label||'Recorded host'};
};