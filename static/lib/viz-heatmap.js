// Recorded activity density, not progress, importance or successful outcomes.
export const HEAT_WINDOW=15*60*1000;
export function activityHeat(events,hits,playhead){
 const positions=new Map(hits.filter(h=>[h.x,h.y,h.w,h.h].every(Number.isFinite)).map(h=>[h.id,{x:h.x+h.w/2,y:h.y+h.h/2}]));
 const cells=new Map(),seen=new Set();let located=0,unlocated=0;
 for(const e of events){
  const age=playhead-e.ts;if(!Number.isFinite(age)||age<0||age>=HEAT_WINDOW)continue;
  const identity=String(e.agent_id)+':'+(e.call_id||e.id||JSON.stringify([e.ts,e.action,e.world_target,e.world_targets]));
  if(seen.has(identity))continue;seen.add(identity);
  let targets=[...new Set(e.world_targets||[e.world_target])].filter(id=>positions.has(id));
  if(!targets.length&&positions.has(e.workspace_target))targets=[e.workspace_target];
  if(!targets.length){unlocated++;continue;}located++;
  const weight=Math.exp(-age/(3*60*1000))/targets.length;
  for(const id of targets){const p=positions.get(id),key=Math.round(p.x/80)+':'+Math.round(p.y/80);let c=cells.get(key);
   if(!c){c={x:0,y:0,weight:0};cells.set(key,c);}c.x+=p.x*weight;c.y+=p.y*weight;c.weight+=weight;
  }
 }
 const spots=[...cells.values()].map(c=>({x:c.x/c.weight,y:c.y/c.weight,weight:c.weight,intensity:1-Math.exp(-c.weight/4)})).sort((a,b)=>b.weight-a.weight);
 return {spots:spots.slice(0,128),located,unlocated,truncated:spots.length>128};
}
export function drawHeat(ctx,heat,camera,pixelRatio=1){
 ctx.save();ctx.globalCompositeOperation='screen';
 for(const s of heat.spots){
  const x=s.x*camera.k+camera.x,y=s.y*camera.k+camera.y;
  const radius=Math.max(28*pixelRatio,Math.min(140*pixelRatio,180*camera.k));
  if(x+radius<0||y+radius<0||x-radius>ctx.canvas.width||y-radius>ctx.canvas.height)continue;
  const gradient=ctx.createRadialGradient(x,y,0,x,y,radius),alpha=.36*Math.sqrt(s.intensity);
  const color=s.intensity>.65?'255,135,65':s.intensity>.25?'239,190,82':'70,169,152';
  gradient.addColorStop(0,`rgba(${color},${alpha})`);gradient.addColorStop(.4,`rgba(${color},${alpha*.6})`);gradient.addColorStop(1,`rgba(${color},0)`);
  ctx.fillStyle=gradient;ctx.fillRect(x-radius,y-radius,radius*2,radius*2);
 }
 ctx.restore();
}
