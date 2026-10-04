// Small transport/camera shell. All visual semantics live in replaceable source.
import {SourceSandbox} from '/static/lib/viz-source-sandbox.js';
import {FollowCamera,cameraForBounds,easeCamera} from '/static/lib/viz-follow-camera.js';
import {AvatarCache} from '/static/lib/viz-avatar-cache.js';
import {FlowMemory} from '/static/lib/viz-flow-memory.js';
import {flowDemo,DEMO_LENGTH_MS} from '/static/lib/viz-flow-demo.js';
import {PreviewCache,syntheticPreview} from '/static/lib/viz-preview-cache.js';
const canvas=document.getElementById('c'),ctx=canvas.getContext('2d');
const stat=document.getElementById('stat'),learning=document.getElementById('learning');
const slider=document.getElementById('t'),liveButton=document.getElementById('live');
let scene={entities:[],relations:[],events:[]},meta={},program=null,sandbox=null,base=null;
let pixelRatio=Math.min(devicePixelRatio||1,2);
let width=innerWidth,height=innerHeight,camera={x:20,y:70,k:.7},revision=0,live=true,playing=false,playhead=Date.now(),tmin=0,tmax=0;
let frames=0,failure='',loading=false,last=performance.now(),signature='',selected=null;
const avatarCache=new AvatarCache(blobs=>sandbox?.setAvatars(blobs));
// Artifact previews are host-owned thumbnails of recorded media; the demo uses
// a generated placeholder so the synthetic sequence never resembles live output.
const previewCache=new PreviewCache(()=>sandbox?.setImages(currentImages()));
let demoImages={};
function currentImages(){return demoEnabled?demoImages:previewCache.blobs();}
let programHistory=[];
let recoveryTimer=null,recoveryAttempts=0,lastFrameTimings={};
const retryButton=document.getElementById('retry-render');
const fittedViews=new Set();
const follow=new FollowCamera(),followButton=document.getElementById('follow-activity');
let followPaused=false;
try{follow.enabled=localStorage.getItem('clarp.fleet.follow')==='true';}catch{}
function followLabel(){followButton.ariaPressed=String(follow.enabled&&!followPaused);followButton.textContent=followPaused?'Resume following':follow.enabled?'Following activity':'Follow activity';}
function pauseFollowing(){if(follow.enabled){followPaused=true;followLabel();}}
followButton.onclick=()=>{
  follow.enabled=followPaused?true:!follow.enabled;followPaused=false;follow.reset();followLabel();
  try{localStorage.setItem('clarp.fleet.follow',String(follow.enabled));}catch{}
};
followLabel();
function fitView(initial=false){
  const b=initial?(meta.focusBounds||meta.bounds):meta.bounds;if(!b)return;
  const top=document.getElementById('hud').getBoundingClientRect().bottom+16;
  const bottom=document.getElementById('bar').getBoundingClientRect().top-(width<900||height<600?36:12);
  const padding=width<900?16:50,available=Math.max(40,bottom-top);
  camera.k=Math.min((width-padding*2)/b.w,available/b.h);
  camera.x=(width-b.w*camera.k)/2-b.x*camera.k;
  camera.y=top+(available-b.h*camera.k)/2-b.y*camera.k;
}
let worldBase=null,cabinetBase=null,flowBase=null,lastData=null,liveScene=null;
let demoEnabled=false,demoScene=null,demoStart=0,flowScene=null;
const flowMemory=new FlowMemory();
const demoButton=document.getElementById('flow-demo'),labelsButton=document.getElementById('flow-labels');
let actionLabels=false;
let view='world';
try{const saved=localStorage.getItem('clarp.fleet.view');if(['cabinets','flow'].includes(saved))view=saved;}catch{}
const requestedView=new URLSearchParams(location.search).get('view');
// ?window=SECONDS widens the live evidence window (server-bounded) so older real
// work can be replayed; the default remains the last hour.
const windowSeconds=Math.max(60,Math.min(90*86400,Number(new URLSearchParams(location.search).get('window'))||3600));
// ?until=EPOCH_MS anchors that window at a past instant and opens paused there.
let untilMs=Math.max(0,Math.floor(Number(new URLSearchParams(location.search).get('until'))||0));
if(['world','cabinets','flow'].includes(requestedView))view=requestedView;
const cameras={world:{...camera},cabinets:{...camera},flow:{...camera}};
function selectProgram(data){
  const baseline=view==='flow'?flowBase:view==='cabinets'?cabinetBase:worldBase;
  const current=data?.program;
  const next=(current&&(current.view||'world')===view?current:data?.view_programs?.[view])||baseline;
  const key=view+':'+JSON.stringify(next);
  if(key===signature)return;
  failure='';recoveryAttempts=0;
  if(program&&signature.startsWith(view+':')&&program!==base)programHistory.push(program);
  // Only fall back to the same view's stable baseline, never another metaphor.
  use(next);signature=key;
}
function selectView(next){
  if(!worldBase||!cabinetBase||!flowBase)return;
  follow.reset();meta={};
  cameras[view]={...camera};view=next;camera={...cameras[view]};
  base=view==='flow'?flowBase:view==='cabinets'?cabinetBase:worldBase;
  if(demoEnabled){demoEnabled=false;demoButton.ariaPressed='false';live=true;playing=false;liveButton.ariaPressed='true';}
  if(liveScene)scene=view==='flow'?(flowScene||liveScene):liveScene;
  demoButton.hidden=labelsButton.hidden=view!=='flow';
  learning.textContent=view==='flow'?'Live activity':lastData?.learning?.enabled?'Autonomous development on':'Preview · autonomous development off';
  programHistory=[];signature='';selected=null;
  document.getElementById('inspector').hidden=true;
  document.getElementById('view-flow').ariaPressed=String(view==='flow');
  document.getElementById('view-world').ariaPressed=String(view==='world');
  document.getElementById('view-cabinets').ariaPressed=String(view==='cabinets');
  document.getElementById('redesign').hidden=view!=='world';
  try{localStorage.setItem('clarp.fleet.view',view);}catch{}
  const viewURL=new URL(location.href);viewURL.searchParams.set('view',view);history.replaceState(null,'',viewURL);
  selectProgram(lastData);
}
document.getElementById('view-flow').onclick=()=>selectView('flow');
document.getElementById('view-world').onclick=()=>selectView('world');
document.getElementById('view-cabinets').onclick=()=>selectView('cabinets');
function size(){
 const oldWidth=width,oldHeight=height;width=innerWidth;height=innerHeight;
 pixelRatio=Math.min(devicePixelRatio||1,2);canvas.style.width=width+'px';canvas.style.height=height+'px';
 canvas.width=Math.round(width*pixelRatio);canvas.height=Math.round(height*pixelRatio);
 camera.x+=(width-oldWidth)/2;camera.y+=(height-oldHeight)/2;
 if(meta.bounds)fitView();
}
size();addEventListener('resize',size);
function use(next){
  clearTimeout(recoveryTimer);recoveryTimer=null;
  sandbox?.destroy();program=next;
  let goodFrames=0;
  sandbox=new SourceSandbox(next,(bitmap,result,timings)=>{
    ctx.drawImage(bitmap,0,0);bitmap.close();meta=result;frames++;
    if(selected&&!document.getElementById('inspector').hidden)inspectHit(meta.hits?.find(h=>h.id===selected));
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
  sandbox.setAvatars(avatarCache.blobs());sandbox.setImages(currentImages());
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
  [worldBase,cabinetBase,flowBase]=await Promise.all([readProgram('/static/viz-world'),readProgram('/static/viz-cabinets'),readProgram('/static/viz-flow')]);
  selectView(view);await load();
}
async function load(){
  if(loading)return;loading=true;
  try{
    const response=await fetch('/viz/events?window='+windowSeconds+(untilMs?'&until='+untilMs:''));if(!response.ok)throw Error('Fleet data unavailable');
    const data=await response.json();liveScene=data.world;flowScene=flowMemory.update(liveScene);
    if(!demoEnabled)scene=view==='flow'?flowScene:liveScene;revision=data.library_revision;
    lastData=data;selectProgram(data);
    previewCache.update((liveScene.work?.artifacts||[]).filter(a=>a.preview).map(a=>({id:a.id,url:a.preview.url})));
    avatarCache.update([...(data.actors||[]),
      ...liveScene.entities.filter(e=>e.kind==='organization'&&e.parent==='github').map(e=>({id:e.id,label:e.label,avatar_url:'/viz/owner-avatar/'+encodeURIComponent(e.label)}))]);
    if(!demoEnabled){tmin=scene.events[0]?.ts||Date.now();tmax=untilMs?untilMs:Math.max(Date.now(),scene.events.at(-1)?.ts||0);if(live)playhead=tmax;if(untilMs&&live){live=false;liveButton.ariaPressed='false';}}
    if(!failure){
      const state=data.learning||{};
      const rejected=state.last_result?.rejected?.[0]?.error;
      learning.textContent=demoEnabled?'Workflow demo · synthetic sequence, not live activity':view==='flow'&&data.authoring_view!=='flow'?'Live activity':state.designing ? (state.stage||'Astra is developing')+'…' :
        rejected ? 'Development paused: '+rejected :
        state.enabled ? 'Autonomous development on · watching for new entities' :
        'Preview · autonomous development off';
    }
  }catch(error){learning.textContent=error.message;}
  finally{loading=false;}
}
function frame(now){
  const dt=Math.min(now-last,100);last=now;
  if(demoEnabled&&playing){playhead+=dt;if(playhead>demoStart+DEMO_LENGTH_MS)playhead=demoStart;}
  else if(live)playhead=Date.now();else if(playing){playhead=Math.min(tmax,playhead+dt*120);if(playhead===tmax)playing=false;}
  slider.value=String(1000*(playhead-tmin)/Math.max(1,tmax-tmin));
  document.getElementById('clock').textContent=new Date(playhead).toLocaleTimeString();
  if(follow.enabled&&!followPaused&&!document.hidden&&!failure){
    const bounds=follow.update({events:scene.events,meta,playhead,now});
    const target=cameraForBounds(bounds,{width,top:document.getElementById('hud').getBoundingClientRect().bottom+24,bottom:document.getElementById('bar').getBoundingClientRect().top-40});
    if(target)camera=easeCamera(camera,target,dt,matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  if(width>0&&height>0&&scene.entities.length)sandbox?.draw({scene,time:now,width:canvas.width,height:canvas.height,pixelRatio,camera:{x:camera.x*pixelRatio,y:camera.y*pixelRatio,k:camera.k*pixelRatio},playhead,interaction:{selected,actionLabels},reducedMotion:matchMedia('(prefers-reduced-motion: reduce)').matches});
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);init();setInterval(()=>{if(live)load();},5000);
function replay(){live=false;liveButton.ariaPressed='false';}
liveButton.onclick=()=>{
  // Live means now: drop any historical anchor so polling returns to the present.
  if(untilMs){untilMs=0;const liveURL=new URL(location.href);liveURL.searchParams.delete('until');history.replaceState(null,'',liveURL);}
  demoEnabled=false;demoButton.ariaPressed='false';if(liveScene)scene=view==='flow'?flowScene:liveScene;live=true;playing=false;liveButton.ariaPressed='true';load();};
slider.oninput=()=>{pauseFollowing();follow.reset();replay();playing=false;playhead=tmin+(tmax-tmin)*Number(slider.value)/1000;};
document.getElementById('play').onclick=()=>{replay();if(playhead>=tmax-1000)playhead=tmin;playing=!playing;};
demoButton.onclick=()=>{
  follow.reset();meta={};
  demoEnabled=!demoEnabled;demoButton.ariaPressed=String(demoEnabled);selected=null;document.getElementById('inspector').hidden=true;
  if(demoEnabled){
    demoStart=Date.now();demoScene=flowDemo(demoStart);scene=demoScene;tmin=demoStart;tmax=demoStart+DEMO_LENGTH_MS;playhead=tmin;
    syntheticPreview().then(blob=>{demoImages={'demo:artifact-video':blob};if(demoEnabled)sandbox?.setImages(currentImages());});
    live=false;playing=true;liveButton.ariaPressed='false';
    learning.textContent='Workflow demo · synthetic sequence, not live activity';
  }else{scene=flowScene||liveScene;live=true;playing=false;liveButton.ariaPressed='true';load();}
  sandbox?.setImages(currentImages());
  // Reset only the prototype renderer's context selection for a different dataset.
  fittedViews.delete('flow');programHistory=[];signature='';selectProgram(lastData);
};
labelsButton.onclick=()=>{actionLabels=!actionLabels;labelsButton.ariaPressed=String(actionLabels);};
document.getElementById('fit').onclick=()=>{pauseFollowing();fitView();};

function inspectHit(hit){
 if(!hit){document.getElementById('inspector').hidden=true;return;}
 const set=(id,value)=>{const el=document.getElementById(id);if(el.textContent!==value)el.textContent=value;};
 set('node-title',hit.label||'');set('node-detail',[hit.purpose,hit.path,hit.sample].filter(Boolean).join('\n'));
 const link=document.getElementById('node-link'),href=typeof hit.link==='string'&&/^(\/media\/|https:\/\/)/.test(hit.link)?hit.link:'';
 link.hidden=!href;if(!href)link.removeAttribute('href');if(href){if(link.getAttribute('href')!==href)link.href=href;set('node-link',hit.linkLabel||'Open');}
}
const pointers=new Map();let gestureMoved=false;
const point=e=>({x:e.clientX,y:e.clientY});
const pinch=()=>{
 const [a,b]=[...pointers.values()];return {x:(a.x+b.x)/2,y:(a.y+b.y)/2,d:Math.max(1,Math.hypot(a.x-b.x,a.y-b.y))};
};
function zoomAt(x,y,factor,nextX=x,nextY=y){
 const old=camera.k;camera.k=Math.max(.005,Math.min(5,old*factor));
 camera.x=nextX-(x-camera.x)*camera.k/old;camera.y=nextY-(y-camera.y)*camera.k/old;
}
canvas.onpointerdown=e=>{
 if(e.pointerType==='mouse'&&e.button!==0)return;
 pauseFollowing();
 if(!pointers.size)gestureMoved=false;else gestureMoved=true;
 pointers.set(e.pointerId,{...point(e),sx:e.clientX,sy:e.clientY});canvas.setPointerCapture(e.pointerId);
};
canvas.onpointermove=e=>{
 const prev=pointers.get(e.pointerId);if(!prev)return;
 const before=pointers.size>=2?pinch():null;
 pointers.set(e.pointerId,{...prev,...point(e)});
 if(Math.hypot(e.clientX-prev.sx,e.clientY-prev.sy)>8)gestureMoved=true;
 if(before){const after=pinch();zoomAt(before.x,before.y,after.d/before.d,after.x,after.y);}
 else{camera.x+=e.clientX-prev.x;camera.y+=e.clientY-prev.y;}
};
canvas.onpointerup=e=>{
 if(!pointers.has(e.pointerId))return;
 if(pointers.size===1&&!gestureMoved){
  const x=(e.clientX-camera.x)/camera.k,y=(e.clientY-camera.y)/camera.k;
  const hit=[...(meta.hits||[])].reverse().find(b=>x>=b.x&&y>=b.y&&x<=b.x+b.w&&y<=b.y+b.h);
  if(hit){selected=hit.id;document.getElementById('inspector').hidden=false;inspectHit(hit);}
 }
 pointers.delete(e.pointerId);
};
canvas.onpointercancel=canvas.onlostpointercapture=e=>{pointers.delete(e.pointerId);gestureMoved=true;};
canvas.onwheel=e=>{e.preventDefault();pauseFollowing();zoomAt(e.clientX,e.clientY,e.deltaY<0?1.12:1/1.12);};
document.getElementById('close-inspector').onclick=()=>{document.getElementById('inspector').hidden=true;};
document.getElementById('redesign').onclick=async()=>{
 const r=await fetch('/viz/supersede',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({world:true,revision,entity_id:selected,reason:'Improve only this selected detail where needed. Keep the Lantern Works concepts, unaffected interactions, layout conventions and visual language. Prefer a compatible expansion or targeted repair; this is not a request for a redesign.'})});
 document.getElementById('design-result').textContent=r.ok?'Astra is improving this detail…':'Could not start development';
};
window.fleetWorldSnapshot=()=>({frames,revision,view,demoEnabled,actionLabels,live,playing,playhead,follow:{enabled:follow.enabled,paused:followPaused,phase:follow.phase},timeline:{since:tmin,until:tmax},camera:{...camera},failure,program:program?.title,frameTimings:lastFrameTimings,meta,scene,previews:Object.keys(currentImages())});
