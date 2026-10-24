// Camera-only direction: renderer layout and evidence remain untouched.
export class FollowCamera {
  constructor(){this.enabled=false;this.reset();}
  reset(){this.phase='overview';this.focusUntil=0;this.overviewUntil=0;this.latest=-Infinity;this.targets=[];}
  update({events=[],meta={},playhead,now}){
    if(!this.enabled)return null;
    let newest=-Infinity;
    const visible=events.filter(e=>Number.isFinite(e.ts)&&e.ts<=playhead);
    const stamp=e=>Number.isFinite(e.finished_at)&&e.finished_at<=playhead?Math.max(e.ts,e.finished_at):e.ts;
    for(const e of visible)newest=Math.max(newest,stamp(e));
    if(newest<this.latest)this.reset(); // Replay loop or seek to earlier evidence.
    if(newest>this.latest){
      this.latest=newest;
      if(playhead-newest<15000&&(this.phase==='focus'||now>=this.overviewUntil)){
        this.targets=visible.filter(e=>stamp(e)>=newest-2000);
        if(this.phase!=='focus'){this.focusUntil=now+6000;this.overviewUntil=now+12000;}
        this.phase='focus';
      }
    }
    if(now>=this.focusUntil)this.phase='overview';
    if(this.phase!=='focus')return meta.bounds||null;
    const ids=new Set(this.targets.flatMap(e=>[...(e.world_targets||[e.world_target]),e.remote_target,'agent:'+e.agent_id]).filter(Boolean));
    const boxes=(meta.hits||[]).filter(h=>ids.has(h.id));
    // Some generated views provide agent positions but no matching hit box.
    for(const a of meta.agents||[])if(this.targets.some(e=>e.agent_id===a.id))boxes.push({x:a.x-70,y:a.y-70,w:140,h:140});
    const valid=boxes.filter(b=>[b.x,b.y,b.w,b.h].every(Number.isFinite)&&b.w>0&&b.h>0);
    if(!valid.length)return meta.bounds||null;
    const x=Math.min(...valid.map(b=>b.x))-60,y=Math.min(...valid.map(b=>b.y))-60;
    return {x,y,w:Math.max(320,Math.max(...valid.map(b=>b.x+b.w))+60-x),h:Math.max(240,Math.max(...valid.map(b=>b.y+b.h))+60-y)};
  }
}

export function cameraForBounds(b,{width,top,bottom,padding=24,maxZoom=1.25}){
  if(!b||![b.x,b.y,b.w,b.h,width,top,bottom].every(Number.isFinite)||b.w<=0||b.h<=0)return null;
  const available=Math.max(40,bottom-top),k=Math.max(.005,Math.min(maxZoom,Math.max(40,width-padding*2)/b.w,available/b.h));
  return {k,x:width/2-(b.x+b.w/2)*k,y:top+available/2-(b.y+b.h/2)*k};
}

export function easeCamera(camera,target,dt,reducedMotion=false){
  const a=reducedMotion?1:1-Math.exp(-Math.max(0,Math.min(dt,100))/900);
  return Object.fromEntries(['x','y','k'].map(key=>[key,camera[key]+(target[key]-camera[key])*a]));
}
