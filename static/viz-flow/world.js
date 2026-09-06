const {build,hash,stateAt}=require('./model.js');
const fx=require('./effects.js');
const craft=require('./craft.js');
const slate=require('./slate.js');
const lantern=require('./lantern.js');
const {drawAgentAvatar}=require('./avatar.js');
const {drawGitHub}=require('./github.js');
let key='',model,lastTime=0;const poses=new Map();
const label=(c,s,x,y,size=12,color='#cbdedc',font='sans-serif')=>{c.fillStyle=color;c.font=`${size}px ${font}`;c.textAlign='left';c.fillText(String(s||''),x,y);};
const clock=ms=>{const d=new Date(ms);return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');};
// The inspection text is the evidence contract for one object: intent, evidence,
// validation and outcome are separate lines, and unknowns are stated as such.
const describe=o=>{
 const v=o.evidence.validation;
 const lines=['Work · '+o.agent+' · '+o.status+(o.completed_at?' at '+clock(o.completed_at):''),
  o.intent.none?'Intent: no plan recorded · outcome only':'Intent: '+o.intent.done+'/'+o.intent.total+' declared items complete'+(o.intent.current?' · now: '+o.intent.current:''),
  'Evidence: '+o.evidence.events+' tool events, '+o.evidence.changes+' recorded changes across '+o.evidence.files.length+' files, '+v.runs+' validation runs'+(v.state==='none'?'':' · '+v.kind+' '+({failed:'failed',interrupted:'chain failed; failing step unknown',recovered:'recovered: the same checks later passed',ok:'succeeded',running:'running',unknown:'outcome unknown'}[v.state]||v.state)+(v.unresolved?.length?' · unresolved: '+v.unresolved.slice(0,3).join(' · '):''))+' · attributed by '+o.evidence.basis,
  o.outcome?'Outcome: '+o.outcome.type+' “'+o.outcome.title+'” at '+clock(o.outcome.created_at)+(o.outcome.count>1?' (+'+(o.outcome.count-1)+' more)':'')+(o.outcome.preview?' · preview from recorded media':'')+(o.outcome.source_count?' · '+o.outcome.source_count+' sources':''):o.finished?'Outcome: none recorded · closed without an artifact':'Outcome: none yet'];
 for(const h of o.handoffs)lines.push((h.link==='transfer'?'Handoff: ':'Referenced: ')+h.fromName+' → '+h.toName+' at '+clock(h.ts)+(h.link==='transfer'?' (explicit handoff record)':' (message names this plan; not a transfer)'));
 if(o.remoteRuns.length)lines.push('Remote checks: '+o.remoteRuns.map(r=>r.conclusion||'running').join(', '));
 return lines.join('\n');
};
module.exports.render=({ctx:c,scene,time,width,height,camera,playhead,interaction={},avatars={},images={},reducedMotion=false})=>{
 const next=JSON.stringify([scene,Math.floor(playhead/500)]);
 if(!model||key!==next){model=build(scene,null,playhead);key=next;}
 const dt=Math.min(2,Math.max(.1,(time-lastTime)/16.67));lastTime=time;
 const files=model.files.map(f=>{let p=poses.get(f.id);if(!p){p={x:f.x,y:f.y,vx:0,vy:0};poses.set(f.id,p);}
  if(reducedMotion){p.x=f.x;p.y=f.y;p.vx=p.vy=0;}else{p.vx=(p.vx+(f.x-p.x)*.035*dt)*Math.pow(.76,dt);p.vy=(p.vy+(f.y-p.y)*.035*dt)*Math.pow(.76,dt);p.x+=p.vx*dt;p.y+=p.vy*dt;}return {...f,x:p.x,y:p.y};});
 const m={...model,files,fileMap:new Map(files.map(f=>[f.id,f]))},phase=reducedMotion?0:time/4000,hits=[],agents=[],visualActions=[];let drawnFiles=0;
 // Zoom reveals detail; selection never does. Overview keeps silhouettes, light
 // and identity; names and small marks appear as the viewer moves closer.
 const detail=camera.k<.45?0:camera.k<1.1?1:2;
 const relations={rails:0,tethers:0,threads:0,deliveries:0,discoveries:0};
 c.fillStyle='#091c24';c.fillRect(0,0,width,height);
 const glow=c.createRadialGradient(width*.4,height*.5,10,width*.4,height*.5,width*.7);glow.addColorStop(0,'#19484455');glow.addColorStop(1,'#091c2400');c.fillStyle=glow;c.fillRect(0,0,width,height);
 c.save();c.translate(camera.x,camera.y);c.scale(camera.k,camera.k);c.textBaseline='alphabetic';
 // Belonging is material and still: configured origins are double rails with
 // anchors. They never move by themselves; only deliveries travel along them.
 const drawnOrigins=new Map();
 for(const relation of m.structural){const workspace=m.regionMap.get(relation.from),a=m.projects.find(p=>p.id===workspace.project),r=m.remoteMap.get(relation.to);
  const id=a.id+'>'+r.id;if(drawnOrigins.has(id))continue;drawnOrigins.set(id,{a,r});relations.rails++;
  craft.rail(c,(c,a,b)=>fx.path(c,a,b,35),a,r);
 }
 for(const project of m.projects){
  const seed=hash(project.id),children=m.regions.filter(r=>r.project===project.id);
  for(const r of children)if(!r.core)craft.neck(c,project,r,seed);
  if(!children.some(r=>r.core)){
   craft.surface(c,{...project,rx:100,ry:85},seed,phase*.1,seed,project.character);
   craft.branch(c,project.x,project.y+10);
  }
  for(const r of children){
   craft.surface(c,r,seed+hash(r.id)%11,phase*.1,seed,project.character);
   craft.ornament(c,r,project.character,craft.tinted(seed,project.character)[1]);
   if(r.kind==='service')craft.machine(c,r,m.latest.get(r.id),m.latest.get(r.id)?stateAt(m.latest.get(r.id),playhead):'unknown',time,reducedMotion);
   if(!r.core){craft.branch(c,r.x-95,r.y-r.ry+37);label(c,r.displayName,r.x-75,r.y-r.ry+42,13,'#c2d4c6');}
   craft.kiln(c,r,r.validation,time,playhead,reducedMotion);
   if(r.validation&&r.validation.state!=='none')visualActions.push({target:r.id,action:'validation',state:r.validation.state});
   const v=r.validation;
   hits.push({id:r.id,label:r.label+(r.displayName?' · '+r.displayName:''),path:r.path,purpose:(r.kind==='service'?'Observed service':r.is_worktree?'Working copy · shared repository':r.kind==='unresolved'?'Location not recorded':'Workspace')+(v&&v.state!=='none'?'\nValidation: '+v.runs+' runs · '+({failed:'a check failed',interrupted:'a check chain failed; failing step unknown',recovered:'recovered: the same checks later passed',ok:'passing',running:'running now',unknown:'outcome unknown'}[v.state]||v.state)+(v.unresolved?.length?' · unresolved: '+v.unresolved.slice(0,3).join(' · '):''):'')+(r.hiddenWork?'\n+'+r.hiddenWork+' more work objects not drawn':''),x:r.x-r.rx,y:r.y-r.ry,w:r.rx*2,h:r.ry*2});
   if(r.hiddenWork){label(c,'+'+r.hiddenWork,r.x+(r.core?165:95),r.y+(r.core?118:100),10,'#b9ad86','Georgia');}
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
   if(detail>=1)label(c,r.label.length>15?r.label.slice(0,14)+'…':r.label,r.x-34,r.y+32,9,'#c6bfdb');
   // Recorded remote checks: results at the remote, not invented from a push.
   (r.runs||[]).forEach((run,i)=>{const x=r.x-8+i*8,y=r.y-26,done=run.status!=='active'&&run.conclusion;
    c.beginPath();c.arc(x,y,3,0,7);
    if(!done){c.strokeStyle='#e9cc88';c.lineWidth=1.2;c.stroke();if(!reducedMotion){c.beginPath();c.arc(x,y,3,time/400,time/400+2);c.stroke();}}
    else {c.fillStyle=run.conclusion==='success'?'#99e1b3':run.conclusion==='failure'?'#ed9eb0':'#a5bec6';c.fill();}
    visualActions.push({target:r.id,action:'check',state:done?run.conclusion:'running'});});
   hits.push({id:r.id,label:r.label,path:r.url,purpose:'Remote repository · '+owner.label+((r.runs||[]).length?'\nRemote checks: '+r.runs.map(run=>(run.workflow||'run')+' '+(run.conclusion||'running')+(run.branch?' on '+run.branch:'')).join(' · '):''),x:r.x-36,y:r.y-30,w:78,h:72});
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
  if(detail>=1)label(c,f.label.length>20?f.label.slice(0,19)+'…':f.label,f.x-42,f.y+34,10,'#d3e1d7');
  if(e&&['edit','write','create','delete'].includes(e.action)&&state==='succeeded'&&age<120000){c.globalAlpha=.25*Math.exp(-age/35000);c.strokeStyle=fx.color(e.action);c.lineWidth=3;fx.region(c,f.x,f.y,30,24,0);c.stroke();c.globalAlpha=1;}
  c.save();c.translate(f.x,f.y);c.scale(.65,.65);fx.action(c,{x:0,y:0},e,playhead,reducedMotion);c.restore();
  if(e&&age<8000)visualActions.push({target:f.id,action:e.action,state});
  hits.push({id:f.id,label:f.label,path:f.path,purpose:f.purpose,x:f.x-40,y:f.y-26,w:85,h:67});
 }
 // Work slates hang inside their workshop and keep their place as they grow.
 const workObjects=[];
 for(const o of m.slates){
  const project=m.projects.find(p=>p.id===m.regionMap.get(o.region)?.project),ink=craft.tinted(hash(project?.id||o.region),project?.character)[1];
  const {w,h}=lantern.draw(c,o,o,ink,images,time,playhead,reducedMotion,detail);
  if(interaction.actionLabels)label(c,o.stage+(o.finished?' · '+o.status:''),o.x-w/2,o.y+h/2+11,9,'#acc5c5');
  visualActions.push({target:o.id,action:'work',state:o.stage});
  workObjects.push({id:o.id,stage:o.stage,status:o.status,workspace:o.workspace,x:o.x,y:o.y,w,h,artifact:o.outcome?.id||null,preview:!!(o.outcome&&images[o.outcome.id]),validation:o.evidence.validation.state,handoffs:o.handoffs.filter(h=>h.link==='transfer').length,references:o.handoffs.filter(h=>h.link!=='transfer').length,link:o.outcome?.link||null,agent:o.agent_id});
  hits.push({id:'work:'+o.id,label:o.title,purpose:describe(o),link:o.outcome?.link||null,linkLabel:o.outcome?'Open '+o.outcome.type:'',x:o.x-w/2-3,y:o.y-h/2-3,w:w+6,h:h+6});
 }
 for(const actor of m.actors){
  const e=actor.event,status=stateAt(e,playhead),col=fx.color(e.action),age=playhead-(e.finished_at??e.ts);
  const bob=reducedMotion?0:Math.sin(time*.0018+hash(actor.id))*3;
  const pos={x:actor.x,y:actor.y+bob};actor.pos=pos;
  c.save();c.translate(pos.x,pos.y);c.scale(.92,.92);drawAgentAvatar(c,0,0,actor.name,col,phase,status==='running',avatars[actor.id]);c.restore();
  const f=m.fileMap.get(e.world_target)||(e.world_targets||[]).map(id=>m.fileMap.get(id)).find(Boolean);
  const active=status==='running'||age<8000;
  // Completed work leaves a still, fading glyph. Only recorded running work
  // receives a moving halo: a recent afterimage is not invented ongoing work.
  const recent=actor.history.filter(v=>playhead-(v.finished_at??v.ts)<180000);
  const remembered=recent.filter((v,i)=>i===recent.length-1||v.action!==recent[i+1].action||stateAt(v,playhead)!==stateAt(recent[i+1],playhead)).slice(-3);
  if(detail>=1){remembered.forEach((v,i)=>{const state=stateAt(v,playhead),elapsed=Math.max(0,playhead-(v.finished_at??v.ts));
   craft.badge(c,pos.x-30+i*29,pos.y+47,v.action,state,reducedMotion?0:time/700,state==='running'?1:Math.max(.12,Math.exp(-elapsed/75000)));
  });
  if(!remembered.length){c.save();c.globalAlpha=.22;craft.badge(c,pos.x,pos.y+47,e.action,status,0);c.restore();}}
  if(active&&!f){visualActions.push({target:e.world_target,action:e.action,state:status});}
  // The avatar stays settled within its context and reaches toward nearby work.
  if(f&&active){fx.beam(c,pos,{x:f.x-25,y:f.y},e.action,(playhead-e.ts)/1800,Math.max(.2,1-Math.max(0,age)/8000),status==='failed',reducedMotion);if(['read','search'].includes(e.action))relations.discoveries++;}
  if(!f&&active&&['test','build','commit'].includes(e.action)){fx.action(c,{x:pos.x+65,y:pos.y+40},e,playhead,reducedMotion);visualActions.push({target:e.world_target,action:e.action,state:status});}
  // Attribution tether: current work reaches its slate quietly while active.
  const claimedBy=active&&m.work.claimed.has(e.id)?m.slates.find(s=>s.agent_id===actor.id&&e.ts>=s.created_at&&e.ts<=(s.completed_at!=null?s.completed_at+120000:playhead)):null;
  if(claimedBy){relations.tethers++;c.save();c.globalAlpha=.3;c.strokeStyle=col;c.lineWidth=1.2;c.setLineDash([1,4]);c.beginPath();c.moveTo(pos.x+22,pos.y+8);c.quadraticCurveTo((pos.x+claimedBy.x)/2,pos.y+40,claimedBy.x-lantern.size(claimedBy)[0]/2,claimedBy.y);c.stroke();c.restore();}
  // Delivery: a push carries a recognizable object along the origin rail and
  // arrives at the remote. The avatar itself stays at the checkout.
  if(e.action==='push'&&e.remote_target){const remote=m.remoteMap.get(e.remote_target),region=m.regionMap.get(actor.workspace),project=m.projects.find(p=>p.id===region?.project);
   const since=playhead-e.ts,duration=Math.max(2500,(e.finished_at??e.ts+2500)-e.ts),arrived=e.finished_at!=null&&playhead>=e.finished_at&&status!=='failed';
   if(remote&&project&&(active||(arrived&&playhead-e.finished_at<30000))){relations.deliveries++;visualActions.push({target:e.remote_target,action:'push',state:arrived?'delivered':status});
    const carried=m.slates.find(s=>s.agent_id===actor.id&&e.ts>=s.created_at&&e.ts<=(s.completed_at!=null?s.completed_at+120000:playhead));
    if(!arrived&&status!=='failed'){const tt=reducedMotion?.5:Math.min(.98,since/duration),p=fx.point(project,remote,tt);fx.carry(c,p,'#c7acf3');
     if(carried)slate.seal(c,p.x,p.y,7,carried.seed,'#f0e6c8');else{c.save();c.strokeStyle='#e9d29b';c.lineWidth=1.5;c.beginPath();c.moveTo(p.x,p.y-6);c.lineTo(p.x+6,p.y);c.lineTo(p.x,p.y+6);c.lineTo(p.x-6,p.y);c.closePath();c.stroke();c.restore();}}
    else if(arrived){c.save();c.globalAlpha=Math.max(0,1-(playhead-e.finished_at)/30000)*.8;c.fillStyle='#c7acf344';c.beginPath();c.arc(remote.x,remote.y,30,0,7);c.fill();if(carried)slate.seal(c,remote.x+22,remote.y-22,6,carried.seed,'#f0e6c8');c.restore();}
    else if(status==='failed'){fx.beam(c,pos,remote,'push',0,1,true,reducedMotion);}
   }}
  // Recorded repeated touches leave quiet traces, never an inferred handoff.
  const trails=(scene.flowMemory?.touches||[]).filter(t=>t.agent===actor.id&&t.lastObserved<=playhead&&m.fileMap.has(t.target)).slice(-5);
  for(const trail of trails){const f=m.fileMap.get(trail.target);c.globalAlpha=.025+Math.min(.1,Math.log1p(trail.count)*.02);c.strokeStyle='#8dbabc';c.lineWidth=1;fx.path(c,pos,f);c.stroke();c.globalAlpha=1;}
  if(interaction.actionLabels)label(c,e.action+(status==='running'?'…':status==='failed'?' · failed':''),pos.x-35,pos.y+36,9,status==='failed'?'#ee9bad':'#acc5c5');
  const service=m.regionMap.get(e.world_target);
  if(service?.kind==='service'&&active)fx.beam(c,pos,{x:service.x+25,y:service.y},e.action,(playhead-e.ts)/1800,1,status==='failed',reducedMotion);
  const knownTarget=f||(service?.kind==='service'?service:null)||m.structural.find(r=>r.to===e.remote_target);
  if(!knownTarget&&e.location_scope!=='target'){c.strokeStyle='#7e9ca166';c.setLineDash([2,5]);c.beginPath();c.arc(pos.x,pos.y,36,0,7);c.stroke();c.setLineDash([]);}
  agents.push({id:actor.id,agent:actor.name,target:actor.workspace,x:pos.x,y:pos.y,action:e.action,status,interactionTarget:f?.id||e.remote_target||null,work:claimedBy?.id||null});
  hits.push({id:'agent:'+actor.id,label:actor.name,purpose:e.action+' · '+status+(knownTarget?'':' · exact target not recorded')+(claimedBy?'\nWorking on: '+claimedBy.title:''),path:e.evidence?.path||'',sample:e.evidence?.raw||'',x:pos.x-35,y:pos.y-60,w:100,h:115});
 }
 // Explicit collaboration: a recorded agent-to-agent message is a thread with a
 // knot. When the message names a plan, that work's seal travels the thread.
 for(const th of m.threads){
  const a=m.actorMap.get(th.from).pos,b=m.actorMap.get(th.to).pos,age=playhead-th.ts;if(!a||!b||age>1800000)continue;
  relations.threads++;fx.thread(c,a,b,age);
  const knot=fx.sag(a,b,.5),plan=th.plan?m.slateMap.get(th.plan)||m.work.objects.find(o=>o.id===th.plan):null,transfer=!!plan&&th.link==='transfer';
  // Only an explicit handoff record carries the seal across; a message that
  // merely names a plan pins that plan's seal at the knot as a reference.
  const kind=transfer?'handoff':plan?'reference':'message';
  if(age<6000){const tt=reducedMotion?.5:age/6000,p=fx.sag(a,b,tt);fx.carry(c,p,'#e4cf9a');
   if(transfer)slate.seal(c,p.x,p.y,8,plan.seed,'#f0e6c8');else{c.save();c.strokeStyle='#e4cf9a';c.lineWidth=1.4;c.beginPath();c.rect(p.x-6,p.y-4,12,8);c.moveTo(p.x-6,p.y-4);c.lineTo(p.x,p.y+1);c.lineTo(p.x+6,p.y-4);c.stroke();c.restore();}
   visualActions.push({target:th.id,action:kind,state:'traveling'});}
  else visualActions.push({target:th.id,action:kind,state:'settled'});
  if(plan&&(age>=6000||!transfer))slate.seal(c,knot.x,knot.y,5,plan.seed,'#d8c9a0',Math.max(.2,1-age/1800000));
  hits.push({id:'thread:'+th.id,label:th.fromName+' → '+th.toName,purpose:(transfer?'Explicit handoff · “'+plan.title+'” transferred':plan?'Agent message referencing “'+plan.title+'” · a reference, not a transfer':'Agent message · collaboration, no object transfer')+' · '+clock(th.ts),sample:th.excerpt,x:knot.x-14,y:knot.y-14,w:28,h:28});
 }
 if(interaction.selected){const h=hits.find(h=>h.id===interaction.selected);if(h){c.strokeStyle='#f3dfaeaa';c.lineWidth=1;c.beginPath();c.roundRect(h.x-3,h.y-3,h.w+6,h.h+6,16);c.stroke();}}
 c.restore();return {title:'Flow · The Lantern Works',hits,bounds:m.bounds,agents,territories:m.projects.length+m.ownerGroups.length,files:drawnFiles,visualActions,
  projects:m.projects.map(p=>({id:p.id,label:p.label,children:p.members.map(r=>r.id),character:p.character||null})),recentTraces:m.actors.filter(a=>playhead-(a.event.finished_at??a.event.ts)<180000).length,
  detail,workspaces:m.regions.map(r=>({id:r.id,x:r.x,y:r.y,rx:r.rx,ry:r.ry,validation:r.validation?.state||'none',hiddenWork:r.hiddenWork||0})),ownerGroups:m.ownerGroups.map(o=>({id:o.id,children:o.repos.map(r=>r.id),runs:o.repos.flatMap(r=>(r.runs||[]).map(run=>({repo:r.id,conclusion:run.conclusion,status:run.status})))})),ownerPortraits:m.ownerGroups.filter(o=>avatars[o.id]).map(o=>o.id),githubLogo:m.ownerGroups.length>0,
  workObjects,relations,workEvidence:{available:m.work.available,synthetic:m.work.synthetic,plans:m.work.objects.length,threads:m.threads.length,contract:m.work.contract}};
};
