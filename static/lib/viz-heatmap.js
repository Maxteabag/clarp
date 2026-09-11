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
// Fixed CSS-pixel bandwidth is a deliberate map-view scale: zooming changes
// the neighborhood being inspected, while a lone event keeps the same peak.
export const HEAT_SIGMA=30;
export const HEAT_COLOR_MAX=8; // decayed event weight at a kernel center
export function densityGrid(spots,camera,width,height,pixelRatio=1){
 const cssWidth=width/pixelRatio,cssHeight=height/pixelRatio;
 const cell=Math.max(6,cssWidth/256,cssHeight/256),sigma=HEAT_SIGMA/cell;
 const radius=Math.ceil(3*sigma),pad=radius+1;
 const cols=Math.ceil(cssWidth/cell)+2*pad+1,rows=Math.ceil(cssHeight/cell)+2*pad+1;
 const mass=new Float32Array(cols*rows),horizontal=new Float32Array(mass.length),density=new Float32Array(mass.length);
 for(const s of spots){
  const gx=(s.x*camera.k+camera.x)/pixelRatio/cell+pad,gy=(s.y*camera.k+camera.y)/pixelRatio/cell+pad;
  if(![gx,gy,s.weight].every(Number.isFinite)||s.weight<=0||gx<0||gy<0||gx>=cols-1||gy>=rows-1)continue;
  const x=Math.floor(gx),y=Math.floor(gy),fx=gx-x,fy=gy-y;
  // Bilinear deposition avoids an abrupt jump when a point crosses a cell.
  mass[y*cols+x]+=s.weight*(1-fx)*(1-fy);mass[y*cols+x+1]+=s.weight*fx*(1-fy);
  mass[(y+1)*cols+x]+=s.weight*(1-fx)*fy;mass[(y+1)*cols+x+1]+=s.weight*fx*fy;
 }
 const kernel=Float32Array.from({length:radius*2+1},(_,i)=>Math.exp(-.5*((i-radius)/sigma)**2));
 // Separable Gaussian smoothing on scalar weights, before any color mapping.
 // Peak-one kernels give intuitive units: one isolated fresh event peaks at ~1.
 for(let y=0;y<rows;y++)for(let x=0;x<cols;x++){
  let value=0;for(let d=-radius;d<=radius;d++)if(x+d>=0&&x+d<cols)value+=mass[y*cols+x+d]*kernel[d+radius];
  horizontal[y*cols+x]=value;
 }
 for(let y=0;y<rows;y++)for(let x=0;x<cols;x++){
  let value=0;for(let d=-radius;d<=radius;d++)if(y+d>=0&&y+d<rows)value+=horizontal[(y+d)*cols+x]*kernel[d+radius];
  density[y*cols+x]=value;
 }
 return {density,cols,rows,cell,pad};
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
 const step=2**Math.round(Math.log2(16*pixelRatio/camera.k));
 const left=Math.floor(-camera.x/camera.k/step),top=Math.floor(-camera.y/camera.k/step);
 const right=Math.ceil((width-camera.x)/camera.k/step),bottom=Math.ceil((height-camera.y)/camera.k/step),blocks=[];
 for(let y=top;y<bottom;y++)for(let x=left;x<right;x++){
  const gx=((x+.5)*step*camera.k+camera.x)/pixelRatio/grid.cell+grid.pad;
  const gy=((y+.5)*step*camera.k+camera.y)/pixelRatio/grid.cell+grid.pad;
  const ix=Math.floor(gx),iy=Math.floor(gy),fx=gx-ix,fy=gy-iy;
  if(ix<0||iy<0||ix+1>=grid.cols||iy+1>=grid.rows)continue;
  const d=grid.density,n=iy*grid.cols+ix;
  const value=d[n]*(1-fx)*(1-fy)+d[n+1]*fx*(1-fy)+d[n+grid.cols]*(1-fx)*fy+d[n+grid.cols+1]*fx*fy;
  if(value>0)blocks.push({x:x*step,y:y*step,size:step,value});
 }
 return blocks;
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
 const scale=grid.cell*pixelRatio;
 // Grid nodes live at integer coordinates; image samples live at half pixels.
 ctx.drawImage(layer,-(grid.pad+.5)*scale,-(grid.pad+.5)*scale,grid.cols*scale,grid.rows*scale);
 ctx.restore();
}
