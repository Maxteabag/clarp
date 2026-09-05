// Small transport/camera shell. All visual semantics live in replaceable source.
import {SourceSandbox} from '/static/lib/viz-source-sandbox.js';
const canvas=document.getElementById('c'),ctx=canvas.getContext('2d');
const stat=document.getElementById('stat'),learning=document.getElementById('learning');
const slider=document.getElementById('t'),liveButton=document.getElementById('live');
let scene={entities:[],relations:[],events:[]},meta={},program=null,sandbox=null,base=null;
let width=innerWidth,height=innerHeight,camera={x:20,y:70,k:.7},revision=0,live=true,playing=false,playhead=Date.now(),tmin=0,tmax=0;
let frames=0,failure='',loading=false,last=performance.now(),signature='',drag=null,selected=null;
let programHistory=[];
function size(){width=innerWidth;height=innerHeight;canvas.width=width;canvas.height=height;}
size();addEventListener('resize',size);
function use(next){
  sandbox?.destroy();program=next;
  sandbox=new SourceSandbox(next,(bitmap,result)=>{
    ctx.drawImage(bitmap,0,0);bitmap.close();meta=result;frames++;
    stat.textContent=`${result.agents?.length||0} agents · ${result.territories||0} territories · ${result.files||0} located items`;
  },error=>{
    failure=error;learning.textContent='Design failed · restoring the previous world';
    if(program!==base){const prior=programHistory.pop();use(prior||base);}
    else {ctx.fillStyle='#0b1422';ctx.fillRect(0,0,width,height);ctx.fillStyle='#b4c9d5';ctx.fillText('World unavailable: '+error,40,160);}
  });
}
async function init(){
  const manifest=await(await fetch('/static/viz-world/program.json')).json();
  const files=Object.fromEntries(await Promise.all(manifest.files.map(async n=>[n,await(await fetch('/static/viz-world/'+n)).text()])));
  base={...manifest,files};use(base);await load();
}
async function load(){
  if(loading||width<900||height<600)return;loading=true;
  try{
    const response=await fetch('/viz/events?window=3600');if(!response.ok)throw Error('Fleet data unavailable');
    const data=await response.json();scene=data.world;revision=data.library_revision;
    const next=data.program;
    const nextSignature=next?JSON.stringify(next):'base';
    if(nextSignature!==signature){
      failure='';
      if(program&&program!==base)programHistory.push(program);
      if(data.previous_program)programHistory.push(data.previous_program);
      if(next)use(next);signature=nextSignature;
    }
    tmin=scene.events[0]?.ts||Date.now();tmax=Math.max(Date.now(),scene.events.at(-1)?.ts||0);
    if(live)playhead=tmax;
    if(!failure)learning.textContent=data.learning?.designing?'Astra is developing '+data.learning.designing+'…':
      next?'Autonomous world · '+(next.title||'new source revision'):base.title+' · drag to explore / scroll to enter';
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
document.getElementById('fit').onclick=()=>{
  const b=meta.bounds;if(!b)return;camera.k=Math.min((width-100)/b.w,(height-150)/b.h);
  camera.x=(width-b.w*camera.k)/2-b.x*camera.k;camera.y=100-b.y*camera.k;
};
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
 const r=await fetch('/viz/supersede',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({world:true,revision,entity_id:selected,reason:'This part of the world deserves a new visual system. Innovate and rewrite the source as needed.'})});
 document.getElementById('design-result').textContent=r.ok?'Astra is developing the world…':'Could not start development';
};
window.fleetWorldSnapshot=()=>({frames,revision,live,playing,playhead,camera:{...camera},failure,program:program?.title,meta,scene});
