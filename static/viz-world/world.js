const {build,hash,stateAt}=require('./model.js');
const TITLE='The Lantern Works';
let cachedKey='',model=null;
const ink='#e7e8dc',muted='#8ca6a6';
function box(c,x,y,w,h,r,fill,stroke){c.beginPath();c.roundRect(x,y,w,h,r);if(fill){c.fillStyle=fill;c.fill();}if(stroke){c.strokeStyle=stroke;c.stroke();}}
function line(c,x,y,X,Y,color,width=1){c.strokeStyle=color;c.lineWidth=width;c.beginPath();c.moveTo(x,y);c.lineTo(X,Y);c.stroke();}
function text(c,s,x,y,size=12,color=ink,font='sans-serif'){c.fillStyle=color;c.font=size+'px '+font;c.textAlign='left';c.fillText(String(s||''),x,y);}
function short(c,s,w){s=String(s||'');if(c.measureText(s).width<=w)return s;while(s.length&&c.measureText(s+'…').width>w)s=s.slice(0,-1);return s+'…';}
function fitted(c,s,x,y,w,size=12,color=ink,font='sans-serif'){c.font=size+'px '+font;text(c,short(c,s,w),x,y,size,color,font);}
function wheel(c,x,y,r,t,col){c.save();c.translate(x,y);c.rotate(t);c.strokeStyle=col;c.lineWidth=2;c.beginPath();c.arc(0,0,r,0,Math.PI*2);c.stroke();for(let j=0;j<8;j++){const a=j*Math.PI/4;line(c,Math.cos(a)*r*.4,Math.sin(a)*r*.4,Math.cos(a)*(r+4),Math.sin(a)*(r+4),col,2);}c.beginPath();c.arc(0,0,r*.3,0,7);c.stroke();c.restore();}
function pathPoint(a,b,t){const mid=(a.y+b.y)/2;const q=1-t;return {x:q*q*q*a.x+3*q*q*t*a.x+3*q*t*t*b.x+t*t*t*b.x,y:q*q*q*a.y+3*q*q*t*mid+3*q*t*t*mid+t*t*t*b.y};}
function route(c,a,b,color){c.strokeStyle=color;c.beginPath();c.moveTo(a.x,a.y);c.bezierCurveTo(a.x,(a.y+b.y)/2,b.x,(a.y+b.y)/2,b.x,b.y);c.stroke();}
function operation(c,x,y,a,p,col){c.save();c.translate(x,y);c.strokeStyle=col;c.fillStyle=col;c.lineWidth=2;
 if(/read|search|query/.test(a)){const sx=-30+p*60;c.globalAlpha=.13;c.beginPath();c.moveTo(sx,-30);c.lineTo(sx-25,24);c.lineTo(sx+25,24);c.closePath();c.fill();c.globalAlpha=1;line(c,sx,-30,sx,24,col,2);c.beginPath();c.arc(sx,-32,6,0,7);c.stroke();}
 else if(/edit|write|file_change/.test(a)){line(c,-35,-28,35,-28,col);const px=-30+p*60;line(c,px,-28,px,12,col,4);for(let i=0;i<5;i++)line(c,-28+i*13,25,-20+i*13,25,col,3);}
 else if(a==='create'){for(let i=0;i<5;i++){const yy=26-i*11;box(c,-27,yy,54,7,2,i/5<=p?col:'#233c40');}line(c,0,-42,0,-28,col);line(c,-7,-35,7,-35,col);}
 else if(a==='delete'){for(let i=0;i<8;i++){c.globalAlpha=1-p*.65;c.fillRect(-28+i*8,8+p*(12+i%3*8),4,12);}c.globalAlpha=1;line(c,-32,-20,32,-20,col,4);line(c,-8,-30,8,-30,col,3);}
 else if(/build|test|execute/.test(a)){wheel(c,-15,0,19,p*6.28,col);wheel(c,22,12,13,-p*6.28,col);if(a==='test'){line(c,-30,34,-23,40,col);line(c,-23,40,-12,27,col);}}
 else if(/push|deploy|remote|network/.test(a)){line(c,0,30,0,-32,col,3);line(c,0,-32,-9,-20,col,3);line(c,0,-32,9,-20,col,3);box(c,-9,25-p*50,18,12,3,col);}
 else if(/commit|vcs/.test(a)){for(let i=0;i<3;i++)box(c,-28+i*12,-14+i*9,40,12,3,null,col);line(c,-36,28,38,28,col,2);}
 else if(a==='media'){box(c,-32,-23,64,46,4,null,col);c.beginPath();c.moveTo(-26,17);c.lineTo(-10,-4);c.lineTo(2,9);c.lineTo(15,-12);c.lineTo(27,17);c.stroke();}
 else {wheel(c,0,0,26,p*3.14,col);line(c,-38,32,38,32,col);}
 c.restore();}
const {drawAgentAvatar}=require('./avatar.js');
const ACTIONS={read:['Reading…','Read'],search:['Searching…','Searched'],edit:['Editing…','Edited'],write:['Writing…','Wrote'],create:['Creating…','Created'],delete:['Deleting…','Deleted'],commit:['Committing…','Committed'],push:['Pushing…','Pushed'],build:['Building…','Built'],test:['Testing…','Tested'],execute:['Executing…','Executed'],vcs:['Working with Git…','Git operation'],github:['Working on GitHub…','GitHub operation'],message:['Sending…','Message']};
module.exports.render=({ctx:c,scene={},time=0,width=1200,height=800,camera={},playhead,interaction={},reducedMotion=false,avatars={}})=>{
 const key=JSON.stringify([scene.entities||[],scene.relations||[],scene.events||[]]);if(key!==cachedKey||!model){model=build(scene);cachedKey=key;}
 const m=model,now=playhead||0,ambient=reducedMotion?0:time*.001,k=camera.k||1;
 const history=new Map(),latest=new Map(),hits=[],agents=[];let drawnFiles=0;
 for(const ev of m.events){if(ev.ts>now)break;const h=history.get(ev.agent_id)||[];h.push(ev);history.set(ev.agent_id,h);latest.set(ev.world_target,ev);for(const target of ev.world_targets||[])latest.set(target,ev);}
 if(!interaction.heatmapBackground){c.fillStyle='#0a1b25';c.fillRect(0,0,width,height);}c.save();c.translate(camera.x||0,camera.y||0);c.scale(k,k);c.textBaseline='alphabetic';
 for(let j=0;j<40;j++){const y=100+j*(m.bounds.h-100)/40;c.strokeStyle='#254451';c.globalAlpha=.2;c.beginPath();for(let i=0;i<50;i++){const x=i*40,yy=y+Math.sin(i*.48+j*.7+ambient*.15)*5;i?c.lineTo(x,yy):c.moveTo(x,yy);}c.stroke();}c.globalAlpha=1;
 text(c,'CLARP / LIVING FLEET',60,42,12,'#93babf','monospace');text(c,TITLE,58,85,38,'#ece8ce','Georgia');
 text(c,'Agents work at the places they touch.',60,115,15,muted);text(c,'GITHUB / REMOTE HARBOR',2100,135,13,'#c6b2f2','monospace');
 for(const rel of m.relations){const a=m.places.get(rel.from),b=m.places.get(rel.to);if(!a||!b)continue;c.lineWidth=1;c.setLineDash([4,10]);route(c,{x:a.x+a.w,y:a.y+50},{x:b.x,y:b.y+50},'#bba8d240');c.setLineDash([]);}
 const hit=b=>hits.push({id:b.id,label:b.label,purpose:b.purpose||b.kind,path:b.path||b.url||'',x:b.x,y:b.y,w:b.w,h:b.h});
 for(const r of m.rooms){box(c,r.x-5,r.y+8,r.w+10,r.h,26,'#152c34');box(c,r.x,r.y,r.w,r.h,22,r.kind==='remote'?'#292c41':'#1b343b',r.color+'70');
  text(c,r.label,r.x+22,r.y+33,24,r.color,'Georgia');fitted(c,r.path||r.url,r.x+22,r.y+54,r.w-44,10,muted,'monospace');line(c,r.x+22,r.y+72,r.x+r.w-22,r.y+72,r.color+'35');
  if(r.kind==='remote')text(c,'Remote repository',r.x+22,r.y+107,12,muted);
  else if(!r.items?.length)text(c,'Workspace known · individual target may be unrecorded',r.x+22,r.y+r.h-16,11,muted);
  hit({...r,h:72});
 }
 for(const d of m.directories){box(c,d.x,d.y,d.w,d.h,12,'#112930',d.color+'25');fitted(c,d.label,d.x+12,d.y+22,d.w-24,11,d.color,'monospace');hit({...d,h:30});}
 for(const b of m.cards){
  const first=m.events.find(e=>(e.world_targets||[e.world_target]).includes(b.id));
  if(first?.action==='create'&&now<first.ts)continue;drawnFiles++;
  const ev=latest.get(b.id),state=ev?stateAt(ev,now):'unknown',recent=ev&&ev.finished_at!=null&&now-ev.finished_at<1800;
  const removed=ev?.action==='delete'&&state==='succeeded';c.save();if(ev?.action==='create'&&state==='running'){c.globalAlpha=.4;c.setLineDash([3,4]);}if(removed)c.globalAlpha=recent?Math.max(.2,1-(now-ev.finished_at)/1800):.3;
  box(c,b.x,b.y,b.w,b.h,8,'#18323b',b.color+'40');text(c,(b.extension||'·').replace('.','').toUpperCase(),b.x+10,b.y+25,10,b.color,'monospace');
  fitted(c,b.label,b.x+43,b.y+23,b.w-54,12,ink);fitted(c,b.purpose,b.x+43,b.y+44,b.w-54,9,muted);
  if(ev?.action==='create'&&state!=='succeeded')text(c,state==='failed'?'not created':state==='running'?'creation pending':'creation unconfirmed',b.x+10,b.y+58,9,'#e7b77a');
  if(removed)text(c,'deleted',b.x+10,b.y+58,9,'#ed9cac');c.restore();
  if(ev&&(state==='running'||recent)){
   const color=state==='failed'?'#f3929c':b.color;box(c,b.x-3,b.y-3,b.w+6,b.h+6,10,null,color);
   if(ev.action==='read'&&state==='running'&&!reducedMotion){const px=b.x+((now-ev.ts)/1200%1)*b.w;line(c,px,b.y+3,px,b.y+b.h-3,color);}
   if(['edit','write'].includes(ev.action)&&state==='running'){const px=b.x+12+(reducedMotion?.5:(now-ev.ts)/800%1)*(b.w-24);line(c,px,b.y+b.h-8,Math.min(px+18,b.x+b.w-5),b.y+b.h-8,color,3);}
   if(ev.action==='create'&&state==='succeeded'&&recent){c.globalAlpha=reducedMotion?.25:Math.max(0,1-(now-ev.finished_at)/1800);box(c,b.x-7,b.y-7,b.w+14,b.h+14,12,null,'#8ee5b8');c.globalAlpha=1;}
   if(state==='failed')text(c,'Failed',b.x+b.w-45,b.y+14,10,'#f3929c');
  }
  hit(b);
 }
 const unknownEvents=[...history.values()].filter(h=>m.resolve(h.at(-1)).id==='unlocated');
 if(unknownEvents.length){const u=m.unlocated;box(c,u.x,u.y,u.w,u.h,15,'#172730','#60788455');text(c,u.label,u.x+18,u.y+28,18,'#b8c8c9','Georgia');text(c,'No destination is inferred from an old location or missing path.',u.x+18,u.y+49,11,muted);hit(u);}
 const occupied=new Map();
 for(const [id,h] of history){
  const ev=h.at(-1),status=stateAt(ev,now);let dest=m.resolve(ev),grouped=false;
  // A burst across sibling files groups the avatar at their directory while
  // actual individual targets remain visible. Repeated work does not restart travel.
  const burst=h.filter(e=>ev.ts-e.ts<=1200),parents=new Set(burst.map(e=>scene.entities.find(x=>x.id===e.world_target)?.parent));
  if(burst.length>=3&&parents.size===1&&new Set(burst.map(e=>e.world_target)).size>1){const parent=m.places.get([...parents][0]);if(parent){dest=parent;grouped=true;}}
  let i=h.length-2,changed=ev.ts;
  while(i>=0&&m.resolve(h[i]).id===m.resolve(ev).id){changed=h[i].ts;i--;}
  if(grouped)changed=burst[0].ts;
  const before=grouped?h.findLast(e=>e.ts<changed):(i>=0?h[i]:null);
  const previous=before?m.resolve(before):dest;
  const slot=occupied.get(dest.id)||0;occupied.set(dest.id,slot+1);
  const A={x:previous.x+45,y:previous.y+85};
  const B={x:dest.kind==='file'?dest.x+dest.w-35:dest.x+48+(slot%6)*90,y:dest.kind==='file'?dest.y+dest.h/2:dest.y+92+Math.floor(slot/6)*84};
  if(dest.id==='unlocated'){B.x=dest.x+400+slot*90;B.y=dest.y+62;}
  const travel=reducedMotion?1:Math.min(1,Math.max(0,(now-changed)/700));
  const p=pathPoint(A,B,travel*travel*(3-2*travel));const col=status==='failed'?'#f3929c':['#e7b77a','#9cdeca','#cab5ed','#e7aebf','#b7cbf5'][hash(id)%5];
  if(previous.id!==dest.id&&travel<1){c.lineWidth=1.5;route(c,A,B,col+'55');}
  drawAgentAvatar(c,p.x,p.y,ev.agent||id,col,ambient,status==='running',avatars[id]);
  const words=ACTIONS[ev.action]||[ev.action+'…',ev.action];let label=status==='running'?words[0]:status==='failed'?words[1]+' · failed':status==='succeeded'?words[1]+' · done':'Observed '+ev.action;
  if(ev.location_scope==='workspace'&&['read','edit','write','create','delete'].includes(ev.action))label+=' · target unknown';
  if(ev.location_scope==='invocation')label+=' · destination pending';
  if(dest.id==='unlocated')label+=' · location unknown';
  fitted(c,label,p.x-50,p.y+44,260,10,col);if(status!=='running')text(c,'last observed',p.x-50,p.y+58,8,muted);
  if(ev.action==='push'&&ev.remote_target){
   const remote=m.places.get(ev.remote_target);if(remote&&(status==='running'||(ev.finished_at!=null&&now-ev.finished_at<2200))){
    const a={x:dest.x+dest.w,y:dest.y+50},b={x:remote.x,y:remote.y+50};c.lineWidth=2;route(c,a,b,status==='failed'?'#f3929c99':'#c6b2f299');
    if(!reducedMotion&&status!=='failed')for(let j=0;j<4;j++){const q=pathPoint(a,b,((now-ev.ts)/1800+j*.23)%1);box(c,q.x-4,q.y-3,8,6,2,'#c6b2f2');}
    text(c,'push → configured origin',(a.x+b.x)/2-50,(a.y+b.y)/2-10,10,'#c6b2f2');
   }
  }
  if(['commit','build','test'].includes(ev.action)&&status==='running'){operation(c,dest.x+dest.w-50,dest.y+110,ev.action,reducedMotion?.5:ambient%1,col);}
  agents.push({id,agent:ev.agent,target:dest.id,x:p.x,y:p.y,action:ev.action,status,label});
  hits.push({id:'agent:'+id,label:ev.agent,purpose:label,path:ev.evidence?.path||'',sample:ev.evidence?.raw||'',x:p.x-34,y:p.y-59,w:68,h:90});
 }
 if(interaction.selected){const target=hits.find(h=>h.id===interaction.selected);if(target)box(c,target.x-4,target.y-4,target.w+8,target.h+8,10,null,'#fff0be');}
 const votes=new Map();
 for(const a of agents){const ev=history.get(a.id)?.at(-1);if(ev?.workspace_target)votes.set(ev.workspace_target,(votes.get(ev.workspace_target)||0)+1);}
 const focus=m.places.get([...votes].sort((a,b)=>b[1]-a[1])[0]?.[0]);
 const focusBounds=focus?{x:Math.max(0,focus.x-45),y:Math.max(0,focus.y-120),w:1320,h:Math.max(800,Math.min(focus.h+240,1300))}:m.bounds;
 c.restore();return {title:TITLE,heatmapBackground:!!interaction.heatmapBackground,hits,bounds:m.bounds,focusBounds,agents,territories:m.rooms.length,files:drawnFiles};
};
