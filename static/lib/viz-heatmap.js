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
  for(const id of targets){const p=positions.get(id),key=p.x+':'+p.y;let c=cells.get(key);
   if(!c){c={x:0,y:0,weight:0};cells.set(key,c);}c.x+=p.x*weight;c.y+=p.y*weight;c.weight+=weight;
  }
 }
 const spots=[...cells.values()].map(c=>({x:c.x/c.weight,y:c.y/c.weight,weight:c.weight}));
 return {spots,located,unlocated,truncated:false};
}
// Fixed world-space bandwidth: the camera only changes how the field is viewed.
export const HEAT_SIGMA=300;
export const HEAT_PIXEL_SIZE=160;
export const HEAT_COLOR_MAX=8; // decayed event weight at a kernel center
export function densityGrid(spots,camera,width,height,pixelRatio=1){
 const worldWidth=width/camera.k,worldHeight=height/camera.k;
 const cell=Math.max(6,worldWidth/256,worldHeight/256);
 const originX=(Math.floor(-camera.x/camera.k/cell)-1)*cell,originY=(Math.floor(-camera.y/camera.k/cell)-1)*cell;
 const cols=Math.ceil(worldWidth/cell)+3,rows=Math.ceil(worldHeight/cell)+3,density=new Float32Array(cols*rows);
 const support=3*HEAT_SIGMA;
 // Sample the same Gaussian field at world coordinates. Coarse overview grids
 // affect display resolution only, never kernel width or the color domain.
 for(const s of spots){
  if(![s.x,s.y,s.weight].every(Number.isFinite)||s.weight<=0)continue;
  const left=Math.max(0,Math.ceil((s.x-support-originX)/cell)),right=Math.min(cols-1,Math.floor((s.x+support-originX)/cell));
  const top=Math.max(0,Math.ceil((s.y-support-originY)/cell)),bottom=Math.min(rows-1,Math.floor((s.y+support-originY)/cell));
  if(left>right||top>bottom)continue;
  const xs=[];for(let x=left;x<=right;x++)xs.push(Math.exp(-.5*((originX+x*cell-s.x)/HEAT_SIGMA)**2));
  for(let y=top;y<=bottom;y++){
   const wy=s.weight*Math.exp(-.5*((originY+y*cell-s.y)/HEAT_SIGMA)**2);
   for(let x=left;x<=right;x++)density[y*cols+x]+=wy*xs[x-left];
  }
 }
 return {density,cols,rows,cell,originX,originY,spots};
}
export function heatColor(value){
 const t=Math.max(0,Math.min(1,value/HEAT_COLOR_MAX));
 const stops=[[70,169,152],[239,190,82],[255,135,65]];
 const i=t<.5?0:1,f=t<.5?t*2:(t-.5)*2;
 return [...stops[i].map((v,c)=>Math.round(v+(stops[i+1][c]-v)*f)),Math.round(115*Math.sqrt(t))];
}
const layers=new WeakMap();
const palette=Uint8ClampedArray.from(Array.from({length:256},(_,i)=>heatColor(i/255*HEAT_COLOR_MAX)).flat());
export function pixelHeat(grid,camera,width,height,pixelRatio=1){
 const step=HEAT_PIXEL_SIZE;
 const left=Math.floor(-camera.x/camera.k/step),top=Math.floor(-camera.y/camera.k/step);
 const right=Math.ceil((width-camera.x)/camera.k/step),bottom=Math.ceil((height-camera.y)/camera.k/step),blocks=new Map();
 // Visit only cells touched by events, not the potentially enormous empty map.
 for(const s of grid.spots){
  if(![s.x,s.y,s.weight].every(Number.isFinite)||s.weight<=0)continue;
  const x0=Math.max(left,Math.ceil((s.x-3*HEAT_SIGMA)/step-.5)),x1=Math.min(right-1,Math.floor((s.x+3*HEAT_SIGMA)/step-.5));
  const y0=Math.max(top,Math.ceil((s.y-3*HEAT_SIGMA)/step-.5)),y1=Math.min(bottom-1,Math.floor((s.y+3*HEAT_SIGMA)/step-.5));
  for(let y=y0;y<=y1;y++)for(let x=x0;x<=x1;x++){
   const value=s.weight*Math.exp(-.5*((((x+.5)*step-s.x)/HEAT_SIGMA)**2+(((y+.5)*step-s.y)/HEAT_SIGMA)**2)),key=x+':'+y;
   const b=blocks.get(key);if(b)b.value+=value;else blocks.set(key,{x:x*step,y:y*step,size:step,value});
  }
 }
 return [...blocks.values()];
}
export function drawHeat(ctx,heat,camera,pixelRatio=1,style='smooth'){
 if(!heat.spots.length)return;
 const grid=densityGrid(heat.spots,camera,ctx.canvas.width,ctx.canvas.height,pixelRatio);
 if(style==='pixelated'){
  ctx.save();ctx.globalCompositeOperation='source-over';
  for(const block of pixelHeat(grid,camera,ctx.canvas.width,ctx.canvas.height,pixelRatio)){
   const color=heatColor(block.value),x=Math.round(block.x*camera.k+camera.x),y=Math.round(block.y*camera.k+camera.y);
   ctx.fillStyle=`rgba(${color[0]},${color[1]},${color[2]},${color[3]/255})`;
   ctx.fillRect(x,y,Math.round((block.x+block.size)*camera.k+camera.x)-x,Math.round((block.y+block.size)*camera.k+camera.y)-y);
  }
  ctx.restore();return;
 }
 let layer=layers.get(ctx);if(!layer){layer=document.createElement('canvas');layers.set(ctx,layer);}
 layer.width=grid.cols;layer.height=grid.rows;
 const paint=layer.getContext('2d'),pixels=paint.createImageData(grid.cols,grid.rows);
 for(let i=0;i<grid.density.length;i++)if(grid.density[i]>0){
  const color=Math.min(255,Math.round(grid.density[i]/HEAT_COLOR_MAX*255))*4,p=i*4;
  pixels.data[p]=palette[color];pixels.data[p+1]=palette[color+1];pixels.data[p+2]=palette[color+2];pixels.data[p+3]=palette[color+3];
 }
 paint.putImageData(pixels,0,0);
 ctx.save();ctx.globalCompositeOperation='source-over';ctx.imageSmoothingEnabled=true;
 const scale=grid.cell*camera.k;
 // Grid nodes live at integer coordinates; image samples live at half pixels.
 ctx.drawImage(layer,(grid.originX-grid.cell/2)*camera.k+camera.x,(grid.originY-grid.cell/2)*camera.k+camera.y,grid.cols*scale,grid.rows*scale);
 ctx.restore();
}
