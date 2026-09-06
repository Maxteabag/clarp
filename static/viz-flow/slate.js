// The work slate: one handcrafted object that keeps its seal and place while it
// grows from a hollow intent ticket, through stitched evidence, into a framed
// outcome that can show the actual recorded artifact. Size encodes the stage
// reached, never a count. Completed slates dim and settle; they do not vanish.
const {hash}=require('./model-util.js');
exports.SIZES={intent:[84,58],evidence:[102,70],outcome:[124,86]};
exports.size=o=>exports.SIZES[o.stage]||exports.SIZES.intent;
const TYPE_COLORS={video:'#e6b96f',image:'#e6b96f',image_gallery:'#e6b96f',audio:'#e6b96f',document:'#d9d2a8',research:'#c9c2ea',file:'#d9d2a8',workflow_run:'#9dc4dc',release:'#9dc4dc',deployment:'#9dc4dc',code_change:'#9dc4dc'};
exports.typeColor=type=>TYPE_COLORS[type]||'#cbdedc';

// A deterministic seal: three strokes chosen from eight compass points plus a
// pivot. It is the object's face on the slate, on a traveling handoff and on a
// delivery token, so the same work is recognizable everywhere it appears.
exports.seal=(c,x,y,r,seed,color='#e4cf9a',alpha=1)=>{
 c.save();c.translate(x,y);c.globalAlpha=alpha;c.strokeStyle=color;c.fillStyle=color;c.lineWidth=Math.max(1,r*.16);c.lineCap='round';
 c.beginPath();c.arc(0,0,r,0,7);c.stroke();
 const pts=[];for(let i=0;i<3;i++){const k=(seed>>>(i*5))%8;pts.push([Math.cos(k/8*6.283)*r*.62,Math.sin(k/8*6.283)*r*.62]);}
 c.beginPath();c.moveTo(pts[0][0],pts[0][1]);for(const p of pts.slice(1))c.lineTo(p[0],p[1]);if((seed>>>15)&1)c.closePath();c.stroke();
 c.beginPath();c.arc(pts[(seed>>>17)%3][0]*.5,pts[(seed>>>17)%3][1]*.5,r*.14,0,7);c.fill();
 c.restore();
};

const rail=(c,o,w,h,time,reduced)=>{
 // Three cells: intent · evidence · outcome. Filled when that fact exists.
 const y=h/2-8,cells=[-w/2+16,0,w/2-16],v=o.evidence.validation;
 c.lineWidth=1;c.strokeStyle='#8fb1ad66';c.beginPath();c.moveTo(cells[0]+5,y);c.lineTo(cells[2]-5,y);c.stroke();
 const cell=(x,filled,color)=>{c.beginPath();c.rect(x-3.5,y-3.5,7,7);c.fillStyle=filled?color:'#0c202b';c.fill();c.strokeStyle=color;c.lineWidth=1.2;c.stroke();};
 cell(cells[0],!o.intent.none,'#cbdedc');
 cell(cells[1],o.evidence.events>0,v.state==='failed'?'#ed9eb0':v.state==='recovered'||v.state==='ok'?'#99e1b3':'#cbdedc');
 if(v.state==='running'){c.beginPath();const a=reduced?0:time/500;c.arc(cells[1],y,8,a,a+4.2);c.strokeStyle='#e9cc88';c.stroke();}
 else if(v.state==='failed'){c.strokeStyle='#ed9eb0';c.lineWidth=1.6;c.beginPath();c.moveTo(cells[1]-6,y-9);c.lineTo(cells[1]-2,y-5);c.lineTo(cells[1]+1,y-8);c.lineTo(cells[1]+5,y-4);c.stroke();}
 else if(v.state==='recovered'){c.strokeStyle='#99e1b3';c.lineWidth=1.4;c.beginPath();c.moveTo(cells[1]-6,y-6);c.lineTo(cells[1]-2,y-3);c.lineTo(cells[1]+6,y-10);c.stroke();}
 if(o.outcome)cell(cells[2],true,exports.typeColor(o.outcome.type));
 else if(o.finished){cell(cells[2],false,'#8fa7a6');c.beginPath();c.moveTo(cells[2]-5,y+5);c.lineTo(cells[2]+5,y-5);c.strokeStyle='#8fa7a6';c.stroke();}
 else cell(cells[2],false,'#5f7f7c');
};

const glyph=(c,o,x,y,s,time,reduced,age)=>{
 const type=o.outcome.type,color=exports.typeColor(type);c.save();c.translate(x,y);c.strokeStyle=color;c.fillStyle=color;c.lineWidth=1.6;
 if(type==='research'){
  // Sources converge into the report: a constellation that culminates in one page.
  const n=Math.min(7,o.outcome.source_count||(o.outcome.sources||[]).length||3),t=reduced?1:Math.min(1,age/4000);
  for(let i=0;i<n;i++){const a=-2.9+i/(n-1||1)*2.6,r=(s*1.15)*(1.6-.6*t);const sx=Math.cos(a)*r,sy=Math.sin(a)*r*.7-s*.2;
   c.globalAlpha=.35;c.beginPath();c.moveTo(sx,sy);c.lineTo(0,-s*.1);c.stroke();c.globalAlpha=.9;c.beginPath();c.arc(sx,sy,1.6,0,7);c.fill();}
  c.globalAlpha=1;for(let j=0;j<3;j++){c.beginPath();c.moveTo(-s*.45,-s*.15+j*s*.22);c.lineTo(s*.45-(j%2)*s*.2,-s*.15+j*s*.22);c.stroke();}
 }else if(type==='document'||type==='file'){for(let j=0;j<4;j++){c.beginPath();c.moveTo(-s*.45,-s*.35+j*s*.24);c.lineTo(s*.45-(j%2)*s*.25,-s*.35+j*s*.24);c.stroke();}}
 else if(type==='audio'){for(let j=0;j<7;j++){const hgt=s*(.2+.5*Math.abs(Math.sin(j*1.7+o.seed)));c.beginPath();c.moveTo(-s*.45+j*s*.15,-hgt/2);c.lineTo(-s*.45+j*s*.15,hgt/2);c.stroke();}}
 else if(type==='video'||type==='image'||type==='image_gallery'){c.beginPath();c.rect(-s*.5,-s*.38,s,s*.76);c.stroke();if(type==='video'){c.beginPath();c.moveTo(-s*.15,-s*.2);c.lineTo(s*.25,0);c.lineTo(-s*.15,s*.2);c.closePath();c.fill();}else{c.beginPath();c.moveTo(-s*.4,s*.25);c.lineTo(-s*.1,-s*.1);c.lineTo(s*.1,s*.1);c.lineTo(s*.25,-s*.05);c.lineTo(s*.4,s*.25);c.stroke();}}
 else {c.beginPath();c.moveTo(-s*.4,0);c.lineTo(-s*.1,-s*.3);c.lineTo(s*.4,-s*.3);c.lineTo(s*.4,s*.3);c.lineTo(-s*.1,s*.3);c.closePath();c.stroke();}
 c.restore();
};

exports.draw=(c,o,pos,ink,images,time,t,reduced)=>{
 const [w,h]=exports.size(o),active=!o.finished,age=o.age;
 const dim=active?1:Math.max(.42,1-Math.max(0,age-120000)/3600000*.6);
 c.save();c.translate(pos.x,pos.y);c.globalAlpha=dim;
 // Body: a hand-cut tablet. Intent is hollow; evidence fills it; outcome frames it.
 const seed=o.seed,jit=i=>((seed>>>(i*3))%7-3)*.35;
 c.beginPath();c.moveTo(-w/2+jit(1),-h/2);c.lineTo(w/2,-h/2+jit(2));c.lineTo(w/2+jit(3),h/2);c.lineTo(-w/2,h/2+jit(4));c.closePath();
 c.shadowColor='#030f16';c.shadowBlur=active?10:4;c.fillStyle=o.stage==='intent'?'#0f2029':'#16303a';c.fill();c.shadowBlur=0;
 c.lineWidth=o.stage==='outcome'?2:1.2;c.strokeStyle=o.stage==='intent'?ink+'88':ink;c.stroke();
 if(o.stage==='intent'){c.setLineDash([3,4]);c.strokeStyle=ink+'55';c.beginPath();c.rect(-w/2+4,-h/2+4,w-8,h-8);c.stroke();c.setLineDash([]);}
 // Seal and title strip
 exports.seal(c,-w/2+13,-h/2+13,8,seed,active?'#e4cf9a':'#b9ad86');
 c.fillStyle='#e7e8dc';c.font='11px Georgia';c.textAlign='left';c.textBaseline='alphabetic';
 const max=w>110?21:w>90?17:13,title=o.title.length>max?o.title.slice(0,max-1)+'…':o.title;c.fillText(title,-w/2+26,-h/2+17);
 // Evidence stitches along the left edge: one per changed file, bounded.
 c.strokeStyle='#f4c078';c.lineWidth=1.4;for(let i=0;i<Math.min(6,o.evidence.files.length);i++){c.beginPath();c.moveTo(-w/2-1,-h/2+28+i*6);c.lineTo(-w/2+6,-h/2+24+i*6);c.stroke();}
 // Outcome: the actual artifact preview develops into the frame when provenance supports it.
 if(o.outcome){
  const fx=-w/2+9,fy=-h/2+23,fw=w-18,fh=h-38,image=images[o.outcome.id],develop=reduced?1:Math.min(1,Math.max(0,t-o.outcome.created_at)/2500);
  c.save();c.beginPath();c.rect(fx,fy,fw,fh);c.clip();
  if(image){c.globalAlpha=dim*develop;c.drawImage(image,fx,fy,fw,fh);c.globalAlpha=dim*(1-develop)*.9;c.fillStyle='#0f2029';c.fillRect(fx,fy,fw,fh);c.globalAlpha=dim;}
  else {c.fillStyle='#0f2029';c.fillRect(fx,fy,fw,fh);glyph(c,o,fx+fw/2,fy+fh/2,Math.min(fw,fh)*.7,time,reduced,t-o.outcome.created_at);}
  c.restore();
  c.strokeStyle=exports.typeColor(o.outcome.type);c.lineWidth=1.2;c.beginPath();c.rect(fx,fy,fw,fh);c.stroke();
  if(image&&o.outcome.type==='video'){c.fillStyle='#0b1c23cc';c.beginPath();c.arc(fx+fw-9,fy+fh-9,7,0,7);c.fill();c.fillStyle='#f0e6c8';c.beginPath();c.moveTo(fx+fw-11.5,fy+fh-13);c.lineTo(fx+fw-5,fy+fh-9);c.lineTo(fx+fw-11.5,fy+fh-5);c.closePath();c.fill();}
  if(o.outcome.count>1){c.fillStyle='#e7e8dc';c.font='9px sans-serif';c.textAlign='right';c.fillText('+'+(o.outcome.count-1),fx+fw-2,fy-3);}
 }else if(o.stage==='evidence'&&o.intent.current){c.fillStyle='#b5c7c6';c.font='italic 10px Georgia';c.fillText(o.intent.current.length>21?o.intent.current.slice(0,20)+'…':o.intent.current,-w/2+10,-h/2+36);}
 rail(c,o,w,h,time,reduced);
 // A failed validation cracks the slate's top edge; a recovery leaves a green seam.
 const v=o.evidence.validation;
 if(v.state==='failed'||v.state==='recovered'){c.strokeStyle=v.state==='failed'?'#ed9eb0':'#99e1b3';c.lineWidth=v.state==='failed'?1.8:1.2;
  c.globalAlpha=dim*(v.state==='failed'?1:Math.max(.15,1-Math.max(0,t-v.recoveredAt)/1800000));
  c.beginPath();c.moveTo(w*.05,-h/2);c.lineTo(w*.12,-h/2+6);c.lineTo(w*.08,-h/2+11);c.lineTo(w*.18,-h/2+18);c.stroke();c.globalAlpha=dim;}
 c.restore();
 return {w,h};
};
