import {seedPosition} from '/static/lib/viz-drawing.js';
import {DrawingCache} from '/static/lib/viz-sandbox.js';
import {IconCache} from '/static/lib/viz-icons.js';

// Verb -> colour. The palette is the vocabulary: an unknown verb falls back to
// grey rather than being dropped, so a new tool is visible before it is named.
const VERB = {
  read:'#5b8def', search:'#43b0c4', write:'#e0a33e', vcs:'#8f7ae6',
  push:'#ff5c8a', github:'#c9d1d9', build:'#4fb477', test:'#4fb477',
  query:'#c48ad1', message:'#ff9f43', spawn:'#ffd166', ops:'#7d8799',
  network:'#43b0c4', clarp:'#6ea8fe', media:'#e06fa8', review:'#a0a7b4',
};
const dflt = '#5a6472';
const col = v => VERB[v] || dflt;

const cv = document.getElementById('c'), cx = cv.getContext('2d');
const stat = document.getElementById('stat'), clock = document.getElementById('clock');
const slider = document.getElementById('t'), liveBtn = document.getElementById('live');
let W=0, H=0, DPR=1;
function size(){ DPR=Math.min(devicePixelRatio||1,2); W=innerWidth; H=innerHeight;
  cv.width=W*DPR; cv.height=H*DPR; cx.setTransform(DPR,0,0,DPR,0,0); }
size(); addEventListener('resize', ()=>{size();if(W>=900&&H>=600&&!events.length)load();});

document.getElementById('legend').innerHTML = Object.keys(VERB)
  .map(v=>`${v}<i style="background:${col(v)}"></i>`).join('<br>');

// ---- world ---------------------------------------------------------------
const nodes = new Map();     // id -> {id,kind,x,y,vx,vy,r,agent,events}
let pulses = [];             // in-flight particles
let view = {x:0, y:0, k:1};  // pan/zoom
let events = [], cursor = 0, live = true, windowMs = 3600e3, tMin=0, tMax=0;
let SPEC = {};   // archetype -> {travel,decay,persist,weight,trail}
let LABEL = new Map();   // node id -> display label
let hudTotals = {events:0, pct:0};
let registry = new Map(), revision=0, selected=null, loading=false, initialized=false;
let eventKeys=new Set();
const eventKey=ev=>ev.id??`${ev.agent_id}:${ev.ts}:${ev.verb}`;
const drawings = new DrawingCache(), icons = new IconCache();
const learning = document.getElementById('learning');
const actorId = ev => 'agent:' + ev.agent_id;
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

// Report what is actually drawn: during a scrub the window totals describe a
// different moment than the one on screen.
function hud(){
  let agents=0, places=0;
  for(const n of nodes.values()) n.agent ? agents++ : places++;
  stat.textContent =
    `${agents} agents · ${places} nodes · ${cursor}/${hudTotals.events} events` +
    ` · ${hudTotals.pct}% located` + (live ? '' : ' · replay');
}

function relabel(){
  const labels = new Map();
  for(const n of nodes.values()){
    const label = n.agent ? n.name : n.id.split(':').slice(1).join(':') || n.id;
    labels.set(label, (labels.get(label)||0)+1);
    LABEL.set(n.id,label);
  }
  for(const n of nodes.values()){
    const label=LABEL.get(n.id);
    if(labels.get(label)>1) LABEL.set(n.id,label+' · '+(n.agent?'agent '+n.id.slice(-6):n.id.split(':')[0]));
  }
}

function ensure(id, isAgent, weight, name){
  let n=nodes.get(id);
  if(!n){
    n={id, agent:isAgent, ...seedPosition(id,isAgent), vx:0,vy:0,events:0,hot:0,morph:0,name:name||id};
    nodes.set(id,n);
  }
  n.events+=weight||0;
  const presentation=registry.get(id)||{};
  if(n.presentation!==presentation){
    const key=JSON.stringify([presentation.shape,presentation.icon,presentation.logic,presentation.provisional]);
    if(n.designKey && n.designKey!==key)n.morph=0;
    n.designKey=key;n.presentation=presentation;
  }
  n.icon=icons.get(id);
  return n;
}

function prepareDrawings(){
  for(const n of nodes.values()){
    n.drawing=n.presentation.logic ? drawings.prepare(n.id,revision,n.presentation.logic,
      {events:n.events,weight:SPEC[n.presentation.archetype]?.weight||1,hot:0}) : null;
  }
}

// ---- physics -------------------------------------------------------------
// Layout is emergent, not authored: agents are pulled toward the targets they
// touch, so anyone working the same repo drifts into the same cluster.
const links = new Map();     // "agent|node" -> strength
function link(a,b){ const k=a+'|'+b; links.set(k,(links.get(k)||0)+1); }

function step(){
  const arr=[...nodes.values()];
  for(const n of arr){
    // repulsion
    for(const m of arr){
      if(m===n) continue;
      let dx=n.x-m.x, dy=n.y-m.y, d2=dx*dx+dy*dy;
      if(d2<1) { dx=n.id<m.id ? -.5 : .5; dy=.5; d2=1; }
      if(d2<40000){ const f=650/d2; n.vx+=dx*f; n.vy+=dy*f; }
    }
    // gravity to origin keeps the graph on screen
    n.vx -= n.x*0.0075; n.vy -= n.y*0.0075;

  }
  for(const [k,s] of links){
    const [a,b]=k.split('|'); const A=nodes.get(a), B=nodes.get(b);
    if(!A||!B) continue;
    const dx=B.x-A.x, dy=B.y-A.y, d=Math.hypot(dx,dy)||1;
    const rest=105, f=(d-rest)*0.010*Math.min(s,10)/10;
    A.vx+=dx/d*f; A.vy+=dy/d*f; B.vx-=dx/d*f; B.vy-=dy/d*f;
  }
  for(const n of arr){ n.vx*=0.86; n.vy*=0.86; n.x+=n.vx; n.y+=n.vy;
                       n.hot*=n.decay||.94; n.morph=Math.min(1,n.morph+.04);
 }
}

// ---- drawing -------------------------------------------------------------
function radius(n){ return n.agent ? 13+Math.min(n.events,60)*0.11
                                   : 6+Math.min(n.events,300)*0.035; }
function draw(){
  cx.clearRect(0,0,W,H);
  cx.save(); cx.translate(W/2+view.x, H/2+view.y); cx.scale(view.k, view.k);

  for(const [k] of links){
    const [a,b]=k.split('|'); const A=nodes.get(a), B=nodes.get(b);
    if(!A||!B) continue;
    const w=Math.min(links.get(k)||1,10);
    cx.strokeStyle=`rgba(130,155,200,${0.05+0.035*w})`;
    cx.beginPath(); cx.moveTo(A.x,A.y); cx.lineTo(B.x,B.y); cx.stroke();
  }
  for(const p of pulses){
    const A=nodes.get(p.a), B=nodes.get(p.b); if(!A||!B) continue;
    const x=A.x+(B.x-A.x)*p.t, y=A.y+(B.y-A.y)*p.t;
    if(p.trail>0){                        // channel/process draw a wake
      const t0=Math.max(0,p.t-p.trail);
      cx.strokeStyle=p.c; cx.globalAlpha=0.30*(1-p.t); cx.lineWidth=1.6;
      cx.beginPath();
      cx.moveTo(A.x+(B.x-A.x)*t0, A.y+(B.y-A.y)*t0); cx.lineTo(x,y); cx.stroke();
    }
    cx.globalAlpha=Math.min(1,(1-p.t)*2.2);
    cx.fillStyle=p.c; cx.beginPath(); cx.arc(x,y,2.6,0,7); cx.fill();
    cx.globalAlpha=1;
  }
  for(const n of nodes.values()){
    const r=radius(n);
    if(n.hot>0.05){
      cx.fillStyle=`rgba(180,205,255,${0.16*n.hot})`;
      cx.beginPath(); cx.arc(n.x,n.y,r+9*n.hot,0,7); cx.fill();
    }
    const provisional=n.presentation.provisional || n.drawing?.status==='failed' || n.drawing?.status==='pending';
    cx.fillStyle=n.agent ? '#dfe6f5' : provisional ? '#242a36' : '#233e5c';
    cx.strokeStyle=provisional ? '#8b96a7' : '#7392b6';
    cx.lineWidth=1.2;
    cx.save(); cx.translate(n.x,n.y);
    if(!provisional && n.morph<1){
      cx.globalAlpha=1-n.morph;cx.setLineDash([3,4]);
      cx.beginPath();cx.arc(0,0,r+3,0,Math.PI*2);cx.stroke();cx.setLineDash([]);
      cx.globalAlpha=n.morph;
    }
    if(provisional){
      cx.setLineDash([3,4]);
      cx.globalAlpha=reducedMotion ? .8 : .6+.25*Math.sin(last/500);
      cx.beginPath();cx.arc(0,0,r+3,0,Math.PI*2);cx.stroke();
      cx.setLineDash([]);cx.globalAlpha=1;
    } else if(n.drawing?.status==='ready'){
      for(const command of n.drawing.commands){
        const a=command.args.map(v=>v*r); cx.beginPath();
        if(command.op==='circle') cx.arc(a[0],a[1],a[2],0,Math.PI*2);
        if(command.op==='rect') cx.rect(...a);
        if(command.op==='line'){cx.moveTo(a[0],a[1]);cx.lineTo(a[2],a[3]);}
        if(command.op!=='line') cx.fill(); cx.stroke();
      }
    } else {
      const shape=n.agent ? 'circle' : n.presentation.shape||'circle';
      cx.beginPath();
      if(shape==='box') cx.roundRect(-r,-r,2*r,2*r,4);
      else if(shape==='diamond' || shape==='hexagon'){
        const sides=shape==='diamond'?4:6;
        for(let i=0;i<=sides;i++){
          const a=i*Math.PI*2/sides-Math.PI/2;
          i ? cx.lineTo(Math.cos(a)*r,Math.sin(a)*r) : cx.moveTo(Math.cos(a)*r,Math.sin(a)*r);
        }
      } else cx.arc(0,0,r,0,Math.PI*2);
      if(shape!=='ring') cx.fill();cx.stroke();
    }
    cx.textAlign='center'; cx.textBaseline='middle';
    if(provisional){cx.fillStyle='#a9b5c7';cx.font='15px Georgia';cx.fillText('?',0,0);}
    else if(n.icon?.ready && !n.agent) cx.drawImage(n.icon.image,-8,-8,16,16);
    else {
      cx.fillStyle=n.agent?'#152031':'#ccdaec';cx.font='600 10px ui-sans-serif';
      const glyph=n.agent ? n.name.slice(0,2) : (n.presentation.icon||'').replace('glyph:','').slice(0,3);
      cx.fillText(glyph,0,0);
    }
    cx.restore();
    cx.fillStyle=n.agent?'#e0e8f4':provisional?'#929fb2':'#aabfd9';
    cx.font=n.agent?'600 11px ui-sans-serif':'10px ui-sans-serif';
    cx.textAlign='center';cx.textBaseline='middle';
    cx.fillText(LABEL.get(n.id)||n.name,n.x,n.y+r+11);

  }
  cx.restore();
}

// ---- clock / replay ------------------------------------------------------
function emit(ev){
  // How it draws comes from the archetype spec the server shipped, so a new
  // (verb -> archetype) assignment changes the picture with no client change.
  const sp = SPEC[ev.archetype] || SPEC.pulse ||
             {travel:0.035, decay:0.94, weight:1, trail:0};
  const had=nodes.has(ev.target);
  const A=ensure(actorId(ev),true,1,ev.agent), B=ensure(ev.target,false,sp.weight);
  if(!had) relabel();                 // a new node may create an ambiguity
  hud();
  link(actorId(ev), ev.target);
  animate(ev);
}
function animate(ev){
  const A=nodes.get(actorId(ev)),B=nodes.get(ev.target);
  if(!A||!B)return;
  const sp=SPEC[ev.archetype]||{weight:1,travel:.035,trail:0,decay:.94};
  A.hot=1;B.hot=Math.max(B.hot,.8*sp.weight);B.decay=sp.decay;
  if(!reducedMotion)pulses.push({a:actorId(ev),b:ev.target,c:col(ev.verb),t:0,
    v:sp.travel,trail:sp.trail||0,decay:sp.decay});
}
// Build topology without animating: nodes, links and weights, no pulses.
// Thousands of queued particles would swamp the canvas and tell you nothing.
function prime(uptoTs){
  for(const ev of events){
    if(ev.ts>uptoTs) break;
    const A=ensure(actorId(ev),true,1,ev.agent);
    const sp=SPEC[ev.archetype]||{weight:1};
    ensure(ev.target,false,sp.weight||1);
    link(actorId(ev), ev.target);
    cursor++;
  }
  relabel(); hud();
  prepareDrawings();
  clock.textContent = new Date(uptoTs).toLocaleTimeString();
}
function advanceTo(ts){
  while(cursor<events.length && events[cursor].ts<=ts) emit(events[cursor++]);
  clock.textContent = new Date(ts).toLocaleTimeString();
}

let last=performance.now();
function frame(now){
  if(W<900||H<600){requestAnimationFrame(frame);return;}
  frameCount++;
  const dt=Math.min(now-last,50); last=now;
  if(live && tMax){ tMax = Date.now(); advanceTo(tMax); slider.value=1000; }
  else if(tMax){ advanceTo(tMin + (tMax-tMin)*(slider.value/1000)); }
  for(const p of pulses) p.t += p.v;
  pulses = pulses.filter(p=>p.t<1);
  step(); draw();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ---- data ----------------------------------------------------------------
async function load(){
  if(loading || W<900 || H<600) return;
  loading=true;
  try{
    const r=await fetch(`/viz/events?window=${Math.round(windowMs/1000)}`, {credentials:'same-origin'});
    if(!r.ok) throw Error(`Fleet data unavailable (${r.status})`);
    const d=await r.json();
    SPEC=d.archetypes||{}; revision=d.library_revision||0;
    registry=new Map(d.nodes.map(n=>[n.id,n]));
    events=d.events; cursor=0;
    // Preserve positions across refreshes, including the provisional morph.
    for(const [oldId,newId] of Object.entries(d.resolved||{})){
      if(nodes.has(oldId) && !nodes.has(newId)){
        const old=nodes.get(oldId);nodes.set(newId,{...old,id:newId,morph:0});nodes.delete(oldId);
      }
    }
    const present=new Set(events.flatMap(ev=>[actorId(ev),ev.target]));
    for(const [id,n] of nodes){if(!present.has(id))nodes.delete(id);else n.events=0;}
    links.clear();pulses=[];
    tMin=events.length?events[0].ts:Date.now();
    const newest=events.length?events.at(-1).ts:Date.now();
    hudTotals={events:d.coverage.events,pct:d.coverage.specific_pct};
    prime(newest);
    if(initialized)for(const ev of events)if(!eventKeys.has(eventKey(ev)))animate(ev);
    eventKeys=new Set(events.map(eventKey));initialized=true;
    tMax=Math.max(newest,Date.now());
    learning.textContent=d.learning?.designing ? `designing ${d.learning.designing}…` :
      d.learning?.queued?.length ? `identifying ${d.learning.queued.length} new tools…` :
      events.length ? 'Past hour · drag to explore' : 'No activity in the past hour';
    hud();
  } catch(e){stat.textContent=e.message;}
  finally {loading=false;}
}
load(); setInterval(()=>{if(live)load();},5000);

liveBtn.onclick=()=>{live=!live;liveBtn.ariaPressed=String(live);hud();if(live)load();};
slider.oninput=()=>{
  live=false;liveBtn.ariaPressed='false';
  cursor=0;nodes.clear();links.clear();pulses=[];
  if(events.length)prime(tMin+(tMax-tMin)*(slider.value/1000));
};

function inspect(n){
  selected=n.id;
  document.getElementById('inspector').hidden=false;
  document.getElementById('node-title').textContent=LABEL.get(n.id)||n.id;
  document.getElementById('node-detail').textContent=`${Math.round(n.events)} weighted events · ${n.presentation.archetype||'activity'}`;
  document.getElementById('redesign').hidden=n.agent||!n.presentation.redesignable;
  document.getElementById('design-result').textContent='';
}
document.getElementById('close-inspector').onclick=()=>{document.getElementById('inspector').hidden=true;};
document.getElementById('redesign').onclick=async()=>{
  const result=document.getElementById('design-result');
  try{
    const r=await fetch('/viz/supersede',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({entity_id:selected,revision,reason:'This representation is wrong; rethink it.'})});
    if(!r.ok)throw Error(`Could not start redesign (${r.status})`);
    result.textContent='Rethinking this representation…';
  }catch(e){result.textContent=e.message;}
};
// Read-only inspection for accessibility tooling and headless visual checks.
window.fleetMapSnapshot=()=>({nodes:[...nodes.values()].map(n=>({id:n.id,label:LABEL.get(n.id),
  x:n.x,y:n.y,agent:n.agent,provisional:!!n.presentation.provisional,drawing:n.drawing?.status})),
  cursor,revision,live,view:{...view},pulses:pulses.length,frames:frameCount});
let frameCount=0;

// Pan/zoom move the camera, never clamp world positions to the viewport.
document.getElementById('fit').onclick=()=>{
  if(!nodes.size)return;
  const arr=[...nodes.values()];
  const minX=Math.min(...arr.map(n=>n.x))-90,maxX=Math.max(...arr.map(n=>n.x))+90;
  const minY=Math.min(...arr.map(n=>n.y))-70,maxY=Math.max(...arr.map(n=>n.y))+70;
  view.k=Math.max(.15,Math.min(2,(W-100)/(maxX-minX),(H-180)/(maxY-minY)));
  view.x=-(minX+maxX)/2*view.k;
  view.y=35-(minY+maxY)/2*view.k;
};
// pan + zoom
let drag=null;
cv.onpointerdown=e=>{ drag={x:e.clientX,y:e.clientY,startX:e.clientX,startY:e.clientY}; cv.classList.add('drag');
                      cv.setPointerCapture(e.pointerId); };
cv.onpointermove=e=>{ if(!drag) return;
  view.x+=e.clientX-drag.x; view.y+=e.clientY-drag.y;
  drag={...drag,x:e.clientX,y:e.clientY}; };
cv.onpointerup=e=>{
  if(drag && Math.hypot(e.clientX-drag.startX,e.clientY-drag.startY)<5){
    const x=(e.clientX-W/2-view.x)/view.k,y=(e.clientY-H/2-view.y)/view.k;
    const hit=[...nodes.values()].find(n=>Math.hypot(n.x-x,n.y-y)<radius(n)+8);
    if(hit)inspect(hit);
  }
  drag=null;cv.classList.remove('drag');
};
cv.onpointercancel=()=>{drag=null;cv.classList.remove('drag');};
cv.onwheel=e=>{e.preventDefault();
  const old=view.k;
  view.k=Math.max(.15,Math.min(4,view.k*(e.deltaY<0?1.1:1/1.1)));
  view.x=(e.clientX-W/2)-(e.clientX-W/2-view.x)*view.k/old;
  view.y=(e.clientY-H/2)-(e.clientY-H/2-view.y)*view.k/old;
};
