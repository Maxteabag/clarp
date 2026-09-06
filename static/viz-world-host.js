// Small transport/camera shell. All visual semantics live in replaceable source.
import {SourceSandbox} from '/static/lib/viz-source-sandbox.js';
import {AvatarCache} from '/static/lib/viz-avatar-cache.js';
const canvas=document.getElementById('c'),ctx=canvas.getContext('2d');
const stat=document.getElementById('stat'),learning=document.getElementById('learning');
const slider=document.getElementById('t'),liveButton=document.getElementById('live');
let scene={entities:[],relations:[],events:[]},meta={},program=null,sandbox=null,base=null;
let width=innerWidth,height=innerHeight,camera={x:20,y:70,k:.7},revision=0,live=true,playing=false,playhead=Date.now(),tmin=0,tmax=0;
let frames=0,failure='',loading=false,last=performance.now(),signature='',drag=null,selected=null;
const avatarCache=new AvatarCache(blobs=>sandbox?.setAvatars(blobs));
let programHistory=[];
let recoveryTimer=null,recoveryAttempts=0,lastFrameTimings={};
const retryButton=document.getElementById('retry-render');
const fittedViews=new Set();
function fitView(initial=false){
  const b=initial?(meta.focusBounds||meta.bounds):meta.bounds;if(!b)return;camera.k=Math.min((width-100)/b.w,(height-230)/b.h);
  camera.x=(width-b.w*camera.k)/2-b.x*camera.k;camera.y=175-b.y*camera.k;
}
let worldBase=null,cabinetBase=null,lastData=null;
let view='world';
try{if(localStorage.getItem('clarp.fleet.view')==='cabinets')view='cabinets';}catch{}
const cameras={world:{...camera},cabinets:{...camera}};
function selectProgram(data){
  const next=view==='cabinets'?cabinetBase:(data?.program||worldBase);
  const key=view+':'+JSON.stringify(next);
  if(key===signature)return;
  failure='';recoveryAttempts=0;
  if(program&&signature.startsWith(view+':')&&program!==base)programHistory.push(program);
  // Only fall back to the same view's stable baseline, never another metaphor.
  use(next);signature=key;
}
function selectView(next){
  if(!worldBase||!cabinetBase)return;
  cameras[view]={...camera};view=next;camera={...cameras[view]};
  base=view==='cabinets'?cabinetBase:worldBase;
  programHistory=[];signature='';selected=null;
  document.getElementById('inspector').hidden=true;
  document.getElementById('view-world').ariaPressed=String(view==='world');
  document.getElementById('view-cabinets').ariaPressed=String(view==='cabinets');
  document.getElementById('redesign').hidden=view!=='world';
  try{localStorage.setItem('clarp.fleet.view',view);}catch{}
  selectProgram(lastData);
}
document.getElementById('view-world').onclick=()=>selectView('world');
document.getElementById('view-cabinets').onclick=()=>selectView('cabinets');
function size(){width=innerWidth;height=innerHeight;canvas.width=width;canvas.height=height;}
size();addEventListener('resize',size);
function use(next){
  clearTimeout(recoveryTimer);recoveryTimer=null;
  sandbox?.destroy();program=next;
  let goodFrames=0;
  sandbox=new SourceSandbox(next,(bitmap,result,timings)=>{
    ctx.drawImage(bitmap,0,0);bitmap.close();meta=result;frames++;
    lastFrameTimings=timings;
    if(failure)learning.textContent='Rendering resumed';
    failure='';retryButton.hidden=true;
    if(++goodFrames>=30)recoveryAttempts=0;
    if(!fittedViews.has(view)&&result.bounds){fittedViews.add(view);fitView(true);}
    stat.textContent=`${result.agents?.length||0} agents · ${result.territories||0} territories · ${result.files||0} located items`;
  },(error,kind)=>{
    failure=error;retryButton.hidden=false;
    // Retain the last canvas image. A delayed message or failed fallback must
    // not erase the world or leave the user with an unrecoverable blank screen.
    if(kind!=='source' && recoveryAttempts<2){
      recoveryAttempts++;
      learning.textContent='Rendering paused · reconnecting…';
      recoveryTimer=setTimeout(()=>use(program),500*recoveryAttempts);
    }else if(program!==base){
      learning.textContent='Using the last stable view';
      const prior=programHistory.pop();use(prior||base);
    }else{
      learning.textContent='Rendering paused · last picture retained';
    }
  });
  sandbox.setAvatars(avatarCache.blobs());
}
function retryRender(){
  if(!program)return;
  recoveryAttempts=0;use(program);
}
retryButton.onclick=retryRender;
document.addEventListener('visibilitychange',()=>{
  if(!document.hidden&&sandbox?.stopped)retryRender();
});

async function init(){
  async function readProgram(root){
    const manifest=await(await fetch(root+'/program.json')).json();
    const files=Object.fromEntries(await Promise.all(manifest.files.map(async n=>[n,await(await fetch(root+'/'+n)).text()])));
    return {...manifest,files};
  }
  [worldBase,cabinetBase]=await Promise.all([readProgram('/static/viz-world'),readProgram('/static/viz-cabinets')]);
  selectView(view);await load();
}
async function load(){
  if(loading||width<900||height<600)return;loading=true;
  try{
    const response=await fetch('/viz/events?window=3600');if(!response.ok)throw Error('Fleet data unavailable');
    const data=await response.json();scene=data.world;revision=data.library_revision;
    lastData=data;selectProgram(data);
    avatarCache.update(data.actors||[]);
    tmin=scene.events[0]?.ts||Date.now();tmax=Math.max(Date.now(),scene.events.at(-1)?.ts||0);
    if(live)playhead=tmax;
    if(!failure){
      const state=data.learning||{};
      const rejected=state.last_result?.rejected?.[0]?.error;
      learning.textContent=state.designing ? (state.stage||'Astra is developing')+'…' :
        rejected ? 'Development paused: '+rejected :
        state.enabled ? 'Autonomous development on · watching for new entities' :
        'Preview · autonomous development off';
    }
  }catch(error){learning.textContent=error.message;}
  finally{loading=false;}
}
function frame(now){
  const dt=Math.min(now-last,100);last=now;
  if(live)playhead=Date.now();else if(playing){playhead=Math.min(tmax,playhead+dt*120);if(playhead===tmax)playing=false;}
  slider.value=String(1000*(playhead-tmin)/Math.max(1,tmax-tmin));
  document.getElementById('clock').textContent=new Date(playhead).toLocaleTimeString();
  if(width>=900&&height>=600&&scene.entities.length)sandbox?.draw({scene,time:now,width,height,camera,playhead,interaction:{selected},reducedMotion:matchMedia('(prefers-reduced-motion: reduce)').matches});
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);init();setInterval(()=>{if(live)load();},5000);
function replay(){live=false;liveButton.ariaPressed='false';}
liveButton.onclick=()=>{live=true;playing=false;liveButton.ariaPressed='true';load();};
slider.oninput=()=>{replay();playing=false;playhead=tmin+(tmax-tmin)*Number(slider.value)/1000;};
document.getElementById('play').onclick=()=>{replay();if(playhead>=tmax-1000)playhead=tmin;playing=!playing;};
document.getElementById('fit').onclick=()=>fitView();

canvas.onpointerdown=e=>{drag={x:e.clientX,y:e.clientY,sx:e.clientX,sy:e.clientY};canvas.setPointerCapture(e.pointerId);};
canvas.onpointermove=e=>{if(drag){camera.x+=e.clientX-drag.x;camera.y+=e.clientY-drag.y;drag={...drag,x:e.clientX,y:e.clientY};}};
canvas.onpointerup=e=>{
 if(drag&&Math.hypot(e.clientX-drag.sx,e.clientY-drag.sy)<5){
  const x=(e.clientX-camera.x)/camera.k,y=(e.clientY-camera.y)/camera.k;
  const hit=[...(meta.hits||[])].reverse().find(b=>x>=b.x&&y>=b.y&&x<=b.x+b.w&&y<=b.y+b.h);
  if(hit){selected=hit.id;document.getElementById('inspector').hidden=false;document.getElementById('node-title').textContent=hit.label;
   document.getElementById('node-detail').textContent=[hit.purpose,hit.path,hit.sample].filter(Boolean).join('\n');}
 }drag=null;
};
canvas.onpointercancel=()=>{drag=null;};
canvas.onwheel=e=>{e.preventDefault();const old=camera.k;camera.k=Math.max(.12,Math.min(5,camera.k*(e.deltaY<0?1.12:1/1.12)));
 camera.x=e.clientX-(e.clientX-camera.x)*camera.k/old;camera.y=e.clientY-(e.clientY-camera.y)*camera.k/old;};
document.getElementById('close-inspector').onclick=()=>{document.getElementById('inspector').hidden=true;};
document.getElementById('redesign').onclick=async()=>{
 const r=await fetch('/viz/supersede',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({world:true,revision,entity_id:selected,reason:'Improve only this selected detail where needed. Keep the Lantern Works concepts, unaffected interactions, layout conventions and visual language. Prefer a compatible expansion or targeted repair; this is not a request for a redesign.'})});
 document.getElementById('design-result').textContent=r.ok?'Astra is improving this detail…':'Could not start development';
};
window.fleetWorldSnapshot=()=>({frames,revision,view,live,playing,playhead,timeline:{since:tmin,until:tmax},camera:{...camera},failure,program:program?.title,frameTimings:lastFrameTimings,meta,scene});
