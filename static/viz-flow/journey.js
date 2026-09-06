// The journey beyond the workshop. A recorded background job is a wait on a
// real boundary (GitHub Actions, TestFlight, a test lane, a Host update, a
// service); a pending decision is a wait on the owner. Each wait is a mooring
// line from the work (or its agent) to a boundary post outside the workshop
// rim. Waiting moves slowly and hollow; a release fills and settles; a failure
// frays; an expired heartbeat is a wait that ended without evidence. Quiet time
// is never drawn as waiting. Repeated observed interactions may leave a
// browser-remembered route: a faint dotted pattern, never a dependency.
const {hash}=require('./model-util.js');
const LABELS={github:'GitHub Actions',apple:'TestFlight',lane:'Test lane',host:'Host update',service:'Service',owner:'Owner decision',unknown:'External wait'};
exports.LABELS=LABELS;

exports.waitAt=(j,t)=>{
 if(j.started_at>t)return null;
 const terminalStatus=['succeeded','failed','cancelled'].includes(j.status);
 const terminal=j.terminal_at??(terminalStatus?j.updated_at:null),done=terminal!=null&&terminal<=t;
 const state=!done?'waiting':j.status==='succeeded'?'released':j.terminal_reason==='heartbeat_expired'?'expired':j.status==='cancelled'?'cancelled':'failed';
 const stale=!done&&j.heartbeat_at!=null&&t-j.heartbeat_at>(j.heartbeat_timeout_ms||600000);
 return {id:'job:'+j.id,kind:'job',boundary:j.boundary||'unknown',label:j.boundary_label||LABELS[j.boundary]||LABELS.unknown,title:j.title,detail:j.detail||'',
  agent_id:j.agent_id,agent:j.agent,session:j.session,state,stale,since:j.started_at,until:done?terminal:null,reason:done?(j.terminal_reason||''):'',link:j.link||null,seed:hash(j.id)};
};
exports.decisionAt=(d,t)=>{
 if(d.created_at>t)return null;
 const done=d.resolved_at!=null&&d.resolved_at<=t;
 return {id:'decision:'+d.id,kind:'decision',boundary:'owner',label:LABELS.owner,title:d.title,detail:d.blocks_progress?'blocks progress':'',agent_id:d.agent_id,agent:d.agent,session:d.session,
  state:done?'released':'waiting',stale:false,since:d.created_at,until:done?d.resolved_at:null,reason:done?d.status:'',link:null,seed:hash(d.id),blocks:!!d.blocks_progress};
};

// Boundary posts stand outside the right rim of the workshop, at most three.
// Boundary posts stand outside the rim on the side facing away from the rest
// of the project, so a worktree's gates never land inside its neighbour.
exports.posts=(region,boundaries,base=.4)=>{
 const angles=[base,base+.34,base-.34];
 return boundaries.slice(0,3).map((boundary,i)=>({boundary,label:LABELS[boundary]||LABELS.unknown,region:region.id,
  x:region.x+Math.cos(angles[i])*(region.rx+38),y:region.y+Math.sin(angles[i])*(region.ry+30)}));
};

const glyph=(c,boundary,x,y,s,color)=>{
 c.save();c.translate(x,y);c.strokeStyle=color;c.fillStyle=color;c.lineWidth=1.4;c.lineCap='round';
 if(boundary==='github'){c.beginPath();c.moveTo(-s*.5,-s*.4);c.lineTo(-s*.5,s*.5);c.lineTo(s*.5,s*.5);c.moveTo(-s*.5,0);c.lineTo(s*.5,-s*.5);c.stroke();}
 else if(boundary==='apple'){c.beginPath();c.moveTo(-s*.55,s*.35);c.lineTo(s*.55,-s*.1);c.lineTo(-s*.1,s*.05);c.closePath();c.stroke();c.beginPath();c.moveTo(-s*.1,s*.05);c.lineTo(-s*.3,s*.55);c.stroke();}
 else if(boundary==='lane'){for(let i=0;i<3;i++)for(let j=0;j<2;j++){if((i+j)%2)continue;c.fillRect(-s*.5+i*s*.33,-s*.5+j*s*.5,s*.33,s*.5);}c.beginPath();c.rect(-s*.5,-s*.5,s,s);c.stroke();}
 else if(boundary==='host'){c.beginPath();c.moveTo(-s*.55,0);c.lineTo(0,-s*.55);c.lineTo(s*.55,0);c.moveTo(-s*.4,-s*.1);c.lineTo(-s*.4,s*.5);c.lineTo(s*.4,s*.5);c.lineTo(s*.4,-s*.1);c.stroke();}
 else if(boundary==='service'){c.beginPath();c.arc(0,0,s*.3,0,7);c.stroke();for(let i=0;i<6;i++){const a=i/6*6.283;c.beginPath();c.moveTo(Math.cos(a)*s*.32,Math.sin(a)*s*.32);c.lineTo(Math.cos(a)*s*.55,Math.sin(a)*s*.55);c.stroke();}}
 else if(boundary==='owner'){c.beginPath();c.arc(0,-s*.2,s*.25,0,7);c.stroke();c.beginPath();c.moveTo(-s*.5,s*.55);c.quadraticCurveTo(0,s*.05,s*.5,s*.55);c.stroke();}
 else {c.font=`${Math.round(s)}px Georgia`;c.textAlign='center';c.textBaseline='middle';c.fillText('?',0,1);}
 c.restore();
};
exports.drawPost=(c,post,waits,time,t,reduced,detail)=>{
 const waiting=waits.some(w=>w.state==='waiting'),failed=waits.some(w=>w.state==='failed'||w.state==='expired'),stale=waits.some(w=>w.state==='waiting'&&w.stale);
 const color=failed&&!waiting?'#e0705a':waiting?'#e9cc88':'#99e1b3';
 c.save();c.translate(post.x,post.y);
 // A gate: two posts and a crossbar, the boundary's mark hanging beneath.
 c.strokeStyle='#8fa7a6';c.lineWidth=1.6;c.lineCap='round';c.beginPath();c.moveTo(-12,14);c.lineTo(-12,-12);c.moveTo(12,14);c.lineTo(12,-12);c.moveTo(-15,-12);c.lineTo(15,-12);c.stroke();
 glyph(c,post.boundary,0,3,11,'#cbdedc');
 // The ring above the gate is the state of what waits here.
 c.beginPath();c.arc(0,-24,7,0,7);c.strokeStyle=color;c.lineWidth=1.8;
 if(waiting){if(stale)c.setLineDash([2,3]);const p=reduced?0:time/900;c.beginPath();c.arc(0,-24,7,p,p+4.6);c.stroke();c.setLineDash([]);}
 else if(failed){c.stroke();c.beginPath();c.moveTo(-4,-28);c.lineTo(4,-20);c.moveTo(4,-28);c.lineTo(-4,-20);c.stroke();}
 else {c.fillStyle=color;c.fill();}
 if(detail>=1){c.fillStyle='#b5c7c6';c.font='9px sans-serif';c.textAlign='center';c.textBaseline='alphabetic';c.fillText(post.label,0,28);}
 c.restore();
};
// The mooring: from the work or its agent to the post.
exports.drawMooring=(c,from,post,w,time,t,reduced)=>{
 const to={x:post.x-14,y:post.y};const age=w.until!=null?t-w.until:0;
 c.save();c.lineCap='round';
 const path=()=>{c.beginPath();c.moveTo(from.x,from.y);c.quadraticCurveTo((from.x+to.x)/2,Math.max(from.y,to.y)+26,to.x,to.y);};
 if(w.state==='waiting'){c.strokeStyle='#e9cc88';c.globalAlpha=.55;c.lineWidth=1.3;if(w.stale)c.setLineDash([3,5]);path();c.stroke();c.setLineDash([]);
  if(!reduced){const p=(time/3200+w.seed%7/7)%1,q=1-p,mx=(from.x+to.x)/2,my=Math.max(from.y,to.y)+26;const x=q*q*from.x+2*q*p*mx+p*p*to.x,y=q*q*from.y+2*q*p*my+p*p*to.y;
   c.globalAlpha=.9;c.fillStyle='#f0d998';c.shadowColor='#f0d998';c.shadowBlur=8;c.beginPath();c.arc(x,y,2.6,0,7);c.fill();}}
 else if(w.state==='released'){c.strokeStyle='#99e1b3';c.globalAlpha=Math.max(.06,.35-age/1800000*.29);c.lineWidth=1.2;path();c.stroke();}
 else {c.strokeStyle='#e0705a';c.globalAlpha=Math.max(.15,.7-age/3600000*.5);c.lineWidth=1.2;c.setLineDash(w.state==='expired'?[2,6]:[6,4]);path();c.stroke();c.setLineDash([]);}
 c.restore();
};
// A remembered route: dotted, still, and quieter than anything live.
exports.drawRoute=(c,a,b,count,detail)=>{
 c.save();c.strokeStyle='#8dbabc';c.globalAlpha=.05+Math.min(.14,Math.log1p(count)*.035);c.lineWidth=1;c.setLineDash([1,6]);
 c.beginPath();c.moveTo(a.x,a.y);c.quadraticCurveTo((a.x+b.x)/2,(a.y+b.y)/2+30,b.x,b.y);c.stroke();c.setLineDash([]);
 if(detail>=2){const mx=(a.x+b.x)/2*.5+((a.x+b.x)/2)*.5,my=(a.y+b.y)/2+15;c.globalAlpha=.35;c.fillStyle='#8dbabc';c.font='8px sans-serif';c.textAlign='center';c.textBaseline='middle';c.fillText('×'+count,mx,my);}
 c.restore();
};
exports.describe=w=>{
 const s={waiting:'waiting'+(w.stale?' · heartbeat overdue':''),released:'released',failed:'failed',expired:'wait ended without evidence (heartbeat expired)',cancelled:'cancelled'}[w.state]||w.state;
 return (w.kind==='decision'?'Decision · ':'Wait · ')+w.label+' · '+w.title+' · '+s+(w.detail?' · '+w.detail:'')+' · '+w.agent+(w.attributed?' · attributed to “'+w.attributed+'” by agent and time window':'');
};
