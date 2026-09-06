const {build,hash,stateAt}=require('./model.js');
const fx=require('./effects.js');
const craft=require('./craft.js');
const {drawAgentAvatar}=require('./avatar.js');
const {drawGitHub}=require('./github.js');
let key='',model,lastTime=0;const poses=new Map();
const label=(c,s,x,y,size=12,color='#cbdedc',font='sans-serif')=>{c.fillStyle=color;c.font=`${size}px ${font}`;c.textAlign='left';c.fillText(String(s||''),x,y);};
module.exports.render=({ctx:c,scene,time,width,height,camera,playhead,interaction={},avatars={},reducedMotion=false})=>{
 const next=JSON.stringify([scene,Math.floor(playhead/500)]);
 if(!model||key!==next){model=build(scene,null,playhead);key=next;}
 const dt=Math.min(2,Math.max(.1,(time-lastTime)/16.67));lastTime=time;
 const files=model.files.map(f=>{let p=poses.get(f.id);if(!p){p={x:f.x,y:f.y,vx:0,vy:0};poses.set(f.id,p);}
  if(reducedMotion){p.x=f.x;p.y=f.y;p.vx=p.vy=0;}else{p.vx=(p.vx+(f.x-p.x)*.035*dt)*Math.pow(.76,dt);p.vy=(p.vy+(f.y-p.y)*.035*dt)*Math.pow(.76,dt);p.x+=p.vx*dt;p.y+=p.vy*dt;}return {...f,x:p.x,y:p.y};});
 const m={...model,files,fileMap:new Map(files.map(f=>[f.id,f]))},phase=reducedMotion?0:time/4000,hits=[],agents=[],visualActions=[];let drawnFiles=0;
 c.fillStyle='#091c24';c.fillRect(0,0,width,height);
 const glow=c.createRadialGradient(width*.4,height*.5,10,width*.4,height*.5,width*.7);glow.addColorStop(0,'#19484455');glow.addColorStop(1,'#091c2400');c.fillStyle=glow;c.fillRect(0,0,width,height);
 c.save();c.translate(camera.x,camera.y);c.scale(camera.k,camera.k);c.textBaseline='alphabetic';
 const drawnOrigins=new Set();
 for(const relation of m.structural){const workspace=m.regionMap.get(relation.from),a=m.projects.find(p=>p.id===workspace.project),r=m.remoteMap.get(relation.to);
  const id=a.id+'>'+r.id;if(drawnOrigins.has(id))continue;drawnOrigins.add(id);
  c.strokeStyle='#bda7d355';c.lineWidth=1.5;fx.path(c,a,r,35);c.stroke();
 }
 for(const project of m.projects){
  const seed=hash(project.id),children=m.regions.filter(r=>r.project===project.id);
  for(const r of children)if(!r.core)craft.neck(c,project,r,seed);
  if(!children.some(r=>r.core)){
   craft.surface(c,{...project,rx:100,ry:85},seed,phase*.1);
   craft.branch(c,project.x,project.y+10);
  }
  for(const r of children){
   craft.surface(c,r,seed+hash(r.id)%11,phase*.1,seed);
   if(r.kind==='service')craft.machine(c,r,m.latest.get(r.id),m.latest.get(r.id)?stateAt(m.latest.get(r.id),playhead):'unknown',time,reducedMotion);
   if(!r.core){craft.branch(c,r.x-95,r.y-r.ry+37);label(c,r.displayName,r.x-75,r.y-r.ry+42,13,'#c2d4c6');}
   hits.push({id:r.id,label:r.label+(r.displayName?' · '+r.displayName:''),path:r.path,purpose:r.kind==='service'?'Observed service':r.is_worktree?'Working copy · shared repository':r.kind==='unresolved'?'Location not recorded':'Workspace',x:r.x-r.rx,y:r.y-r.ry,w:r.rx*2,h:r.ry*2});
  }
  label(c,project.label,project.x-75,project.y-(children.some(r=>r.core)?110:43),30,'#e4dfbc','Georgia');
 }
 if(m.ownerGroups.length){
  const g=m.github;fx.region(c,g.x+g.w/2,g.y+g.h/2,g.w/2+35,g.h/2+25,phase*.04);c.fillStyle='#26263a99';c.fill();c.strokeStyle='#b19bcb44';c.stroke();
  drawGitHub(c,g.x+30,g.y+15,42);
  hits.push({id:'github',label:'GitHub',purpose:'GitHub',x:g.x+20,y:g.y+8,w:62,h:62});
 }
 for(const owner of m.ownerGroups){
  c.beginPath();c.roundRect(owner.x,owner.y,owner.w,owner.h,28);c.fillStyle='#35334755';c.fill();c.strokeStyle='#b6a3ca55';c.stroke();
  const portrait=avatars[owner.id];if(portrait){c.save();c.beginPath();c.arc(owner.x+35,owner.y+29,20,0,7);c.clip();c.drawImage(portrait,owner.x+15,owner.y+9,40,40);c.restore();}
  else label(c,owner.label,owner.x+18,owner.y+35,15,'#d6c1ea','Georgia');
  hits.push({id:owner.id,label:owner.label,purpose:'GitHub repository owner',x:owner.x,y:owner.y,w:owner.w,h:53});
  for(const r of owner.repos){c.fillStyle='#343a51';c.strokeStyle='#bca8d377';c.beginPath();c.roundRect(r.x-24,r.y-19,48,38,13);c.fill();c.stroke();
   c.strokeStyle='#bba7d4';c.beginPath();c.moveTo(r.x-8,r.y-6);c.lineTo(r.x-8,r.y+8);c.lineTo(r.x+8,r.y+8);c.moveTo(r.x-8,r.y);c.lineTo(r.x+8,r.y-8);c.stroke();
   label(c,r.label.length>15?r.label.slice(0,14)+'…':r.label,r.x-34,r.y+32,9,'#c6bfdb');
   hits.push({id:r.id,label:r.label,path:r.url,purpose:'Remote repository · '+owner.label,x:r.x-36,y:r.y-23,w:78,h:65});
  }
 }
 // Recent file detail is bounded per workspace; workspace and agent coverage is not.
 for(const f of m.files){
  const first=scene.events.find(e=>(e.world_targets||[e.world_target]).includes(f.id));if(first?.action==='create'&&playhead<first.ts)continue;drawnFiles++;
  const e=f.event,state=e?stateAt(e,playhead):'unknown',age=e?playhead-(e.finished_at??e.ts):Infinity;
  const deleted=e?.action==='delete'&&state==='succeeded',creating=e?.action==='create'&&state!=='succeeded';
  c.save();if(deleted)c.globalAlpha=Math.max(.08,1-age/2500);if(creating)c.globalAlpha=.3;
  const float=reducedMotion?0:Math.sin(time*.0014+hash(f.id))*2.5;c.translate(f.x,f.y+float);c.scale(.58,.58);
  c.shadowColor='#030f16';c.shadowBlur=12;c.fillStyle='#213f47';c.beginPath();c.roundRect(-42,-33,84,66,15);c.fill();c.shadowBlur=0;
  c.fillStyle='#d5d5b9';c.beginPath();c.moveTo(-22,-24);c.lineTo(12,-24);c.lineTo(24,-12);c.lineTo(24,25);c.lineTo(-22,25);c.closePath();c.fill();c.strokeStyle='#667e73';
  craft.document(c,f);
  c.restore();
  label(c,f.label.length>20?f.label.slice(0,19)+'…':f.label,f.x-42,f.y+34,10,'#d3e1d7');
  if(e&&['edit','write','create','delete'].includes(e.action)&&state==='succeeded'&&age<120000){c.globalAlpha=.25*Math.exp(-age/35000);c.strokeStyle=fx.color(e.action);c.lineWidth=3;fx.region(c,f.x,f.y,30,24,0);c.stroke();c.globalAlpha=1;}
  c.save();c.translate(f.x,f.y);c.scale(.65,.65);fx.action(c,{x:0,y:0},e,playhead,reducedMotion);c.restore();
  if(e&&age<8000)visualActions.push({target:f.id,action:e.action,state});
  hits.push({id:f.id,label:f.label,path:f.path,purpose:f.purpose,x:f.x-40,y:f.y-26,w:85,h:67});
 }
 for(const actor of m.actors){
  const e=actor.event,status=stateAt(e,playhead),col=fx.color(e.action),age=playhead-(e.finished_at??e.ts);
  const bob=reducedMotion?0:Math.sin(time*.0018+hash(actor.id))*3;
  const pos={x:actor.x,y:actor.y+bob};
  c.save();c.translate(pos.x,pos.y);c.scale(.92,.92);drawAgentAvatar(c,0,0,actor.name,col,phase,status==='running',avatars[actor.id]);c.restore();
  const f=m.fileMap.get(e.world_target)||(e.world_targets||[]).map(id=>m.fileMap.get(id)).find(Boolean);
  const active=status==='running'||age<8000;
  // Completed work leaves a still, fading glyph. Only recorded running work
  // receives a moving halo: a recent afterimage is not invented ongoing work.
  const recent=actor.history.filter(v=>playhead-(v.finished_at??v.ts)<180000);
  const remembered=recent.filter((v,i)=>i===recent.length-1||v.action!==recent[i+1].action||stateAt(v,playhead)!==stateAt(recent[i+1],playhead)).slice(-3);
  remembered.forEach((v,i)=>{const state=stateAt(v,playhead),elapsed=Math.max(0,playhead-(v.finished_at??v.ts));
   craft.badge(c,pos.x-30+i*29,pos.y+47,v.action,state,reducedMotion?0:time/700,state==='running'?1:Math.max(.12,Math.exp(-elapsed/75000)));
  });
  if(!remembered.length){c.save();c.globalAlpha=.22;craft.badge(c,pos.x,pos.y+47,e.action,status,0);c.restore();}
  if(active&&!f){visualActions.push({target:e.world_target,action:e.action,state:status});}

  // The avatar stays settled within its context and reaches toward nearby work.
  if(f&&active){fx.beam(c,pos,{x:f.x-25,y:f.y},e.action,(playhead-e.ts)/1800,Math.max(.2,1-Math.max(0,age)/8000),status==='failed',reducedMotion);}
  if(!f&&active&&['test','build','commit'].includes(e.action)){fx.action(c,{x:pos.x+65,y:pos.y+40},e,playhead,reducedMotion);visualActions.push({target:e.world_target,action:e.action,state:status});}
  if(e.action==='push'&&e.remote_target&&active){const remote=m.remoteMap.get(e.remote_target);if(remote)visualActions.push({target:e.remote_target,action:'push',state:status});if(remote)fx.beam(c,pos,remote,'push',(playhead-e.ts)/2000,1,status==='failed',reducedMotion);}
  // Recorded repeated touches leave quiet traces, never an inferred handoff.
  const trails=(scene.flowMemory?.touches||[]).filter(t=>t.agent===actor.id&&t.lastObserved<=playhead&&m.fileMap.has(t.target)).slice(-5);
  for(const trail of trails){const f=m.fileMap.get(trail.target);c.globalAlpha=.025+Math.min(.1,Math.log1p(trail.count)*.02);c.strokeStyle='#8dbabc';c.lineWidth=1;fx.path(c,pos,f);c.stroke();c.globalAlpha=1;}
  if(interaction.actionLabels)label(c,e.action+(status==='running'?'…':status==='failed'?' · failed':''),pos.x-35,pos.y+36,9,status==='failed'?'#ee9bad':'#acc5c5');
  const service=m.regionMap.get(e.world_target);
  if(service?.kind==='service'&&active)fx.beam(c,pos,{x:service.x+25,y:service.y},e.action,(playhead-e.ts)/1800,1,status==='failed',reducedMotion);
  const knownTarget=f||(service?.kind==='service'?service:null)||m.structural.find(r=>r.to===e.remote_target);
  if(!knownTarget&&e.location_scope!=='target'){c.strokeStyle='#7e9ca166';c.setLineDash([2,5]);c.beginPath();c.arc(pos.x,pos.y,36,0,7);c.stroke();c.setLineDash([]);}
  agents.push({id:actor.id,agent:actor.name,target:actor.workspace,x:pos.x,y:pos.y,action:e.action,status,interactionTarget:f?.id||e.remote_target||null});
  hits.push({id:'agent:'+actor.id,label:actor.name,purpose:e.action+' · '+status+(knownTarget?'':' · exact target not recorded'),path:e.evidence?.path||'',sample:e.evidence?.raw||'',x:pos.x-35,y:pos.y-60,w:100,h:115});
 }
 if(interaction.selected){const h=hits.find(h=>h.id===interaction.selected);if(h){c.strokeStyle='#f3dfaeaa';c.lineWidth=1;c.beginPath();c.roundRect(h.x-3,h.y-3,h.w+6,h.h+6,16);c.stroke();}}
 c.restore();return {title:'Flow · The Lantern Works',hits,bounds:m.bounds,agents,territories:m.projects.length+m.ownerGroups.length,files:drawnFiles,visualActions,
  projects:m.projects.map(p=>({id:p.id,label:p.label,children:p.members.map(r=>r.id)})),recentTraces:m.actors.filter(a=>playhead-(a.event.finished_at??a.event.ts)<180000).length,
  workspaces:m.regions.map(r=>({id:r.id,x:r.x,y:r.y,rx:r.rx,ry:r.ry})),ownerGroups:m.ownerGroups.map(o=>({id:o.id,children:o.repos.map(r=>r.id)})),ownerPortraits:m.ownerGroups.filter(o=>avatars[o.id]).map(o=>o.id),githubLogo:m.ownerGroups.length>0};
};
