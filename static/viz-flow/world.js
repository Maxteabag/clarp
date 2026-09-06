const {build,hash,stateAt}=require('./model.js');
const fx=require('./effects.js');
const {drawAgentAvatar}=require('./avatar.js');
let key='',model,focusId=null,lastTime=0;const poses=new Map();
const label=(c,s,x,y,size=12,color='#cbdedc',font='sans-serif')=>{c.fillStyle=color;c.font=`${size}px ${font}`;c.textAlign='left';c.fillText(String(s||''),x,y);};
module.exports.render=({ctx:c,scene,time,width,height,camera,playhead,interaction={},avatars={},reducedMotion=false})=>{
 if(focusId&&!scene.entities.some(e=>e.id===focusId&&e.kind==='repository'))focusId=null;
 if(interaction.selected&&scene.entities.some(e=>e.id===interaction.selected&&e.kind==='repository'))focusId=interaction.selected;
 const next=JSON.stringify([scene,focusId,Math.floor(playhead/500)]);
 if(!model||key!==next){model=build(scene,focusId,playhead);key=next;if(!focusId&&model.chosen)focusId=model.chosen.id;}
 const dt=Math.min(2,Math.max(.1,(time-lastTime)/16.67));lastTime=time;
 const displayFiles=model.files.map(f=>{
  const id=model.chosen?.id+':'+f.id;let p=poses.get(id);
  if(!p){p={x:f.x,y:f.y,vx:0,vy:0};poses.set(id,p);}
  if(reducedMotion){p.x=f.x;p.y=f.y;p.vx=p.vy=0;}
  else{p.vx=(p.vx+(f.x-p.x)*.035*dt)*Math.pow(.76,dt);p.vy=(p.vy+(f.y-p.y)*.035*dt)*Math.pow(.76,dt);p.x+=p.vx*dt;p.y+=p.vy*dt;}
  return {...f,x:p.x,y:p.y};
 });
 if(poses.size>200){const keep=new Set(model.files.map(f=>model.chosen?.id+':'+f.id));for(const id of poses.keys())if(!keep.has(id))poses.delete(id);}
 const m={...model,files:displayFiles,fileMap:new Map(displayFiles.map(f=>[f.id,f]))},phase=reducedMotion?0:time/4000,hits=[],agents=[],visualActions=[];let drawnFiles=0;
 c.fillStyle='#091c24';c.fillRect(0,0,width,height);
 const glow=c.createRadialGradient(width*.4,height*.5,10,width*.4,height*.5,width*.7);glow.addColorStop(0,'#19484455');glow.addColorStop(1,'#091c2400');c.fillStyle=glow;c.fillRect(0,0,width,height);
 c.save();c.translate(camera.x,camera.y);c.scale(camera.k,camera.k);c.textBaseline='alphabetic';
 // Quiet current lines provide atmosphere without an imposed grid.
 for(let j=0;j<17;j++){c.strokeStyle='#25494a';c.globalAlpha=.2;c.beginPath();for(let i=0;i<45;i++){const x=130+i*32,y=190+j*40+Math.sin(i*.19+j*.37+phase*.18)*9;i?c.lineTo(x,y):c.moveTo(x,y);}c.stroke();}c.globalAlpha=1;
 label(c,'FLOW / THE LANTERN WORKS',190,175,11,'#7fa9a8','monospace');
 label(c,m.chosen?.label||'Waiting for activity',185,214,30,'#f0e6c9','Georgia');
 label(c,m.chosen?.path||'No checkout recorded',190,238,10,'#8aabaa','monospace');
 const center={x:615,y:490};
 fx.region(c,center.x,center.y,m.region.rx,m.region.ry,phase*.08);c.fillStyle='#173a3b99';c.fill();c.strokeStyle='#81b5a145';c.lineWidth=1.5;c.stroke();
 fx.region(c,center.x,center.y,m.region.rx+10,m.region.ry+9,phase*.08);c.strokeStyle='#8cbaa418';c.stroke();
 if(m.chosen)hits.push({id:m.chosen.id,label:m.chosen.label,path:m.chosen.path,purpose:'Focused workspace',x:210,y:255,w:800,h:490});
 // Folder proximity appears as soft subregions around related file objects.
 const folders=new Map();for(const file of m.files){const f=folders.get(file.parent)||[];f.push(file);folders.set(file.parent,f);}
 for(const [id,files] of folders){if(files.length<2)continue;const x=files.reduce((s,f)=>s+f.x,0)/files.length,y=files.reduce((s,f)=>s+f.y,0)/files.length;
  c.strokeStyle='#91b8ae28';c.setLineDash([2,7]);fx.region(c,x,y,Math.max(78,...files.map(f=>Math.abs(f.x-x)+60)),Math.max(75,...files.map(f=>Math.abs(f.y-y)+52)),0);c.stroke();c.setLineDash([]);
 }
 // Parent ownership is visible even with a single GitHub owner.
 const harborHeight=Math.max(250,(m.ownerGroups.at(-1)?.y||260)+(m.ownerGroups.at(-1)?.h||180)-230),hy=230+harborHeight/2;
 fx.region(c,1300,hy,210,harborHeight/2+55,phase*.04);c.fillStyle='#26263a99';c.fill();c.strokeStyle='#b19bcb44';c.stroke();
 label(c,'GitHub',1150,228,23,'#d7c5ed','Georgia');
 for(const owner of m.ownerGroups){
  c.beginPath();c.roundRect(owner.x,owner.y,owner.w,Math.max(owner.h,70+Math.ceil(owner.repos.length/3)*72),32);c.fillStyle='#35334755';c.fill();c.strokeStyle='#b6a3ca55';c.stroke();
  label(c,owner.label,owner.x+22,owner.y+33,17,'#d6c1ea','Georgia');
  hits.push({id:owner.id,label:owner.label,purpose:'GitHub repository owner',x:owner.x,y:owner.y,w:owner.w,h:owner.h});
  for(const r of owner.repos){
   c.fillStyle='#343a51';c.strokeStyle='#bca8d377';c.beginPath();c.roundRect(r.x-27,r.y-22,54,43,15);c.fill();c.stroke();
   c.strokeStyle='#bba7d4';c.beginPath();c.moveTo(r.x-9,r.y-7);c.lineTo(r.x-9,r.y+9);c.lineTo(r.x+9,r.y+9);c.moveTo(r.x-9,r.y);c.lineTo(r.x+9,r.y-9);c.stroke();
   label(c,r.label.length>15?r.label.slice(0,14)+'…':r.label,r.x-36,r.y+37,9,'#c6bfdb');
   hits.push({id:r.id,label:r.label,path:r.url,purpose:(r.historical?'Previously observed remote repository owned by ':'Remote repository owned by ')+owner.label,x:r.x-40,y:r.y-26,w:80,h:70});
  }
 }
 for(const relation of m.structural){const r=m.remoteMap.get(relation.to);const a={x:995,y:460},b={x:r.x-28,y:r.y};c.strokeStyle='#bda7d34d';c.lineWidth=1;fx.path(c,a,b,45);c.stroke();
  const memory=(scene.flowMemory?.relations||[]).find(x=>x.from===relation.from&&x.to===relation.to);
  hits.push({id:'relation:'+relation.from+':'+relation.to,label:'Configured origin',purpose:memory?'Persisted repository relationship; activity is shown separately':'Repository relationship',x:1000,y:Math.min(a.y,b.y)-25,w:105,h:Math.abs(a.y-b.y)+50});
 }
 // Quiet workspaces remain compact and selectable rather than permanently open.
 label(c,'NEARBY WORKSPACES',205,802,10,'#779b9d','monospace');
 for(const r of m.quiet){const radius=21+(hash(r.id)%8);c.fillStyle='#203c42';c.strokeStyle='#779f9c55';c.beginPath();c.ellipse(r.x,r.y,radius+17,radius*.7,0,0,7);c.fill();c.stroke();
  const residents=[...m.history.values()].map(h=>h.at(-1)).filter(e=>e.workspace_target===r.id);
  const fresh=residents.filter(e=>playhead-(e.finished_at??e.ts)<15000||stateAt(e,playhead)==='running');
  if(fresh.length){c.strokeStyle=fresh.some(e=>stateAt(e,playhead)==='failed')?'#ed9eb0':'#a3d9c4';c.globalAlpha=.5;c.beginPath();c.ellipse(r.x,r.y,radius+22,radius*.7+6,0,0,7);c.stroke();c.globalAlpha=1;}
  residents.slice(0,3).forEach((e,i)=>{const x=r.x+(i-(Math.min(3,residents.length)-1)/2)*20;c.save();c.beginPath();c.arc(x,r.y,9,0,7);c.clip();if(avatars[e.agent_id])c.drawImage(avatars[e.agent_id],x-9,r.y-9,18,18);else{c.fillStyle='#c2d8d5';c.fill();}c.restore();});
  label(c,r.label,r.x-42,r.y+36,11,'#a9c4c1');const tail=r.path?.split('/').pop();if(tail!==r.label)label(c,tail?.slice(0,20),r.x-42,r.y+49,8,'#6d9295','monospace');
  hits.push({id:r.id,label:r.label,path:r.path,purpose:'Focus this workspace',x:r.x-52,y:r.y-24,w:105,h:80});
 }
 // The complete file set remains in the inspector; only the current focus is expanded.
 for(const f of m.files){
  const first=scene.events.find(e=>(e.world_targets||[e.world_target]).includes(f.id));if(first?.action==='create'&&playhead<first.ts)continue;drawnFiles++;
  const e=f.event,state=e?stateAt(e,playhead):'unknown',age=e?playhead-(e.finished_at??e.ts):Infinity;
  const deleted=e?.action==='delete'&&state==='succeeded',creating=e?.action==='create'&&state!=='succeeded';
  c.save();if(deleted)c.globalAlpha=Math.max(.08,1-age/2500);if(creating)c.globalAlpha=.3;
  const float=reducedMotion?0:Math.sin(time*.0014+hash(f.id))*2.5;c.translate(f.x,f.y+float);
  c.shadowColor='#030f16';c.shadowBlur=12;c.fillStyle='#213f47';c.beginPath();c.roundRect(-42,-33,84,66,15);c.fill();c.shadowBlur=0;
  c.fillStyle='#d5d5b9';c.beginPath();c.moveTo(-22,-24);c.lineTo(12,-24);c.lineTo(24,-12);c.lineTo(24,25);c.lineTo(-22,25);c.closePath();c.fill();c.strokeStyle='#667e73';
  for(let j=0;j<5;j++){c.beginPath();c.moveTo(-14,-9+j*6);c.lineTo(14-(j%2)*7,-9+j*6);c.stroke();}
  c.fillStyle='#294a49';c.font='600 8px monospace';c.fillText((f.extension||'').slice(1).toUpperCase(),-14,-15);c.restore();
  label(c,f.label.length>20?f.label.slice(0,19)+'…':f.label,f.x-48,f.y+54,11,'#d3e1d7');
  if(e&&['edit','write','create','delete'].includes(e.action)&&state==='succeeded'&&age<120000){c.globalAlpha=.25*Math.exp(-age/35000);c.strokeStyle=fx.color(e.action);c.lineWidth=3;fx.region(c,f.x,f.y,49,40,0);c.stroke();c.globalAlpha=1;}
  fx.action(c,f,e,playhead,reducedMotion);
  if(e&&age<8000)visualActions.push({target:f.id,action:e.action,state});
  hits.push({id:f.id,label:f.label,path:f.path,purpose:f.purpose,x:f.x-50,y:f.y-40,w:110,h:105});
 }
 for(const actor of m.actors){
  const e=actor.event,status=stateAt(e,playhead),col=fx.color(e.action),age=playhead-(e.finished_at??e.ts);
  const bob=reducedMotion?0:Math.sin(time*.0018+hash(actor.id))*3;
  const pos={x:actor.x,y:actor.y+bob};
  drawAgentAvatar(c,pos.x,pos.y,actor.name,col,phase,status==='running',avatars[actor.id]);
  const f=m.fileMap.get(e.world_target)||(e.world_targets||[]).map(id=>m.fileMap.get(id)).find(Boolean);
  const active=status==='running'||age<5000;
  // The avatar stays settled within its context and reaches toward nearby work.
  if(f&&active){fx.beam(c,pos,{x:f.x-25,y:f.y},e.action,(playhead-e.ts)/1800,Math.max(.2,1-Math.max(0,age)/8000),status==='failed',reducedMotion);}
  if(!f&&active&&['test','build','commit'].includes(e.action)){fx.action(c,{x:pos.x+65,y:pos.y+40},e,playhead,reducedMotion);visualActions.push({target:e.world_target,action:e.action,state:status});}
  if(e.action==='push'&&e.remote_target&&active){const remote=m.remoteMap.get(e.remote_target);if(remote)visualActions.push({target:e.remote_target,action:'push',state:status});if(remote)fx.beam(c,pos,remote,'push',(playhead-e.ts)/2000,1,status==='failed',reducedMotion);}
  // Recorded repeated touches leave quiet traces, never an inferred handoff.
  const trails=(scene.flowMemory?.touches||[]).filter(t=>t.agent===actor.id&&t.lastObserved<=playhead&&m.fileMap.has(t.target)).slice(-5);
  for(const trail of trails){const f=m.fileMap.get(trail.target);c.globalAlpha=.025+Math.min(.1,Math.log1p(trail.count)*.02);c.strokeStyle='#8dbabc';c.lineWidth=1;fx.path(c,pos,f);c.stroke();c.globalAlpha=1;}
  if(interaction.actionLabels)label(c,e.action+(status==='running'?'…':status==='failed'?' · failed':''),pos.x-35,pos.y+45,10,status==='failed'?'#ee9bad':'#acc5c5');
  const knownTarget=f||m.structural.find(r=>r.to===e.remote_target);
  if(!knownTarget&&e.location_scope!=='target'){c.strokeStyle='#7e9ca166';c.setLineDash([2,5]);c.beginPath();c.arc(pos.x,pos.y,36,0,7);c.stroke();c.setLineDash([]);}
  agents.push({id:actor.id,agent:actor.name,target:m.chosen?.id,x:pos.x,y:pos.y,action:e.action,status,interactionTarget:f?.id||e.remote_target||null});
  hits.push({id:'agent:'+actor.id,label:actor.name,purpose:e.action+' · '+status+(knownTarget?'':' · exact target not recorded'),path:e.evidence?.path||'',sample:e.evidence?.raw||'',x:pos.x-35,y:pos.y-60,w:100,h:115});
 }
 if(m.totalFiles>m.files.length)label(c,'More files available in World',650,766,10,'#6e9697');
 if(!m.files.length)label(c,'Workspace known · exact targets not recorded',420,700,11,'#8eacab');
 if(!m.actors.length)label(c,'No recent agent activity in this workspace',340,475,14,'#8eacab');
 if(interaction.selected){const h=hits.find(h=>h.id===interaction.selected);if(h){c.strokeStyle='#f3dfaeaa';c.lineWidth=1;c.beginPath();c.roundRect(h.x-5,h.y-5,h.w+10,h.h+10,16);c.stroke();}}
 c.restore();return {title:'Flow · The Lantern Works',hits,bounds:m.bounds,focusBounds:m.bounds,agents,territories:1+m.ownerGroups.length,files:drawnFiles,visualActions,
  ownerGroups:m.ownerGroups.map(o=>({id:o.id,children:o.repos.map(r=>r.id)})),focusedRepository:m.chosen?.id};
};
