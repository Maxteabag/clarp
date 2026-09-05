const hash=s=>{let h=2166136261;for(const ch of String(s))h=Math.imul(h^ch.charCodeAt(0),16777619);return h>>>0;};
exports.hash=hash;
exports.build=scene=>{
 const entities=scene.entities||[], events=(scene.events||[]).slice().sort((a,b)=>a.ts-b.ts||String(a.id).localeCompare(String(b.id))), byId=new Map(entities.map(e=>[e.id,e]));
 const rooms=[], places=new Map(), resolved=new Map(), cards=[];
 const repos=entities.filter(e=>e.kind==='repository').sort((a,b)=>String(a.path).localeCompare(String(b.path)));
 const owner=e=>{let p=e,seen=new Set();while(p&&!seen.has(p.id)){if(p.kind==='repository')return p.id;seen.add(p.id);p=byId.get(p.parent);}return null;};
 const uncertain=e=>e.kind==='unresolved'||e.id.startsWith('path:')||(e.kind==='file'&&!/\.[a-z0-9]{1,8}$/i.test(e.path||''));
 const addRoom=(id,label,path,items,x,y,w,color,kind)=>{const h=Math.max(220,112+Math.ceil(items.length/2)*104);const r={id,label,path,x,y,w,h,color,kind,items:[]};rooms.push(r);places.set(id,{...r,cx:x+w/2,cy:y+66});items.forEach((e,i)=>{const b={...e,x:x+24+(i%2)*(w/2-12),y:y+96+Math.floor(i/2)*104,w:w/2-36,h:86,color,room:id};b.cx=b.x+b.w/2;b.cy=b.y+b.h/2;r.items.push(b);cards.push(b);places.set(e.id,b);});return r;};
 let leftY=210,rightY=210;
 repos.forEach((r,i)=>{const items=entities.filter(e=>owner(e)===r.id&&e.id!==r.id&&e.kind==='file'&&!uncertain(e)).sort((a,b)=>String(a.path).localeCompare(String(b.path)));const col=i%2,y=col?rightY:leftY;const room=addRoom(r.id,r.label,r.path,items,col?620:60,y,510,['#8edbc2','#f3bf80','#aeb9ef','#d7b1e6'][i%4],'repository');if(col)rightY+=room.h+58;else leftY+=room.h+58;});
 const archiveY=Math.max(leftY,rightY)+16;
 const loose=entities.filter(e=>e.kind==='file'&&!owner(e)&&!uncertain(e));
 if(loose.length)addRoom('archive','Recorded artifacts','Explicit file paths',loose,60,archiveY,1070,'#97becd','archive');
 const remote=entities.filter(e=>e.kind==='remote-repository').sort((a,b)=>a.id.localeCompare(b.id));
 let remoteY=210;
 for(const r of remote){const room=addRoom(r.id,r.label,r.url||'',[],1280,remoteY,430,'#c6b2f2','remote');room.org=(byId.get(r.parent)||{}).label||'GitHub';room.h=220;remoteY+=260;}
 const stations=[['lens','Reading room','read · search · query','#91d9ec'],['press','Revision press','edit · create · delete','#f2bc81'],['engine','Engine house','build · test · execute','#a7d8a2'],['signal','Signal exchange','Clarp · remote · web','#cbb7ee'],['vault','Version vault','git · commit · push','#e8a6ba'],['fog','Uncharted water','Path incomplete or absent','#a7b7c0']];
 const bottom=Math.max(archiveY+(loose.length?Math.max(220,112+Math.ceil(loose.length/2)*104):0),remoteY)+70;
 const machinery=stations.map((s,i)=>({id:'station:'+s[0],label:s[1],purpose:s[2],color:s[3],x:60+i*278,y:bottom,w:256,h:180,cx:188+i*278,cy:bottom+90,kind:s[0]}));
 machinery.forEach(s=>places.set(s.id,s));
 const category=ev=>{const a=String(ev.action||'unknown'),raw=String(ev.evidence?.raw||'');if(/^(read|search|query|media)$/.test(a))return 'lens';if(/^(edit|write|create|delete|file_change)$/.test(a))return 'press';if(/^(build|test|execute)$/.test(a))return 'engine';if(/^(vcs|commit|push|github)$/.test(a))return 'vault';if(/^(clarp|remote|network|ops|util)$/.test(a)||/web_search/.test(raw))return 'signal';return 'fog';};
 for(const e of entities){if(places.has(e.id))continue;const own=owner(e);if(e.kind==='directory'&&own){places.set(e.id,places.get(own));continue;}places.set(e.id,places.get('station:'+category({action:e.label})));}
 for(const ev of events){let dest=places.get(ev.world_target);const ent=byId.get(ev.world_target);const raw=String(ev.evidence?.raw||'');let detail='',path='';
  if(!ent||uncertain(ent)){dest=places.get('station:'+category(ev));detail=ev.evidence?.path?'Recorded fragment: '+ev.evidence.path:'Concrete target not recorded';}
  else {detail=ent.purpose||ent.kind;path=ent.path||ent.url||'';}
  if(ev.action==='github'){
   const m=raw.match(/(?:--repo\s+|repos\/)([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)(?=[\s/'"?])/);
   if(m){const r=places.get('github:'+m[1]);if(r){dest=r;path='https://github.com/'+m[1];detail='Repository identified in command';const run=raw.match(/(?:actions\/runs\/|gh run watch\s+)(\d+)(?=[/\s])/),pr=raw.match(/gh pr (?:ready|merge|view)\s+(\d+)(?=\s)/);if(run)detail='Actions run '+run[1]+' · command observed';if(pr)detail='Pull request #'+pr[1]+' · command observed';}}
  }
  resolved.set(ev.id,{dest:dest||machinery[5],detail,path});
 }
 const fragments=entities.filter(e=>uncertain(e)&&e.kind!=='unresolved').map(e=>({id:e.id,path:e.path||e.label||'Unknown',label:e.label||e.path||'Unknown'}));
 return {rooms,cards,places,events,resolved,machinery,fragments,relations:scene.relations||[],bounds:{x:0,y:0,w:1780,h:bottom+340}};
};