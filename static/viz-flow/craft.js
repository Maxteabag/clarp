// Hand-built visual vocabulary. These are starting points, not an archetype ceiling.
const palettes=[['#1d4140','#86bfab'],['#344237','#c5c58d'],['#243d50','#9dc4dc'],['#3e3846','#c2a3bd']];
exports.palette=seed=>palettes[seed%palettes.length];
exports.contour=(c,r,seed,phase=0,inset=0)=>{
 c.beginPath();for(let i=0;i<=80;i++){
  const a=i/80*Math.PI*2,noise=1+.075*Math.sin(a*3+seed%19)+.035*Math.cos(a*5+seed%7)+.008*Math.sin(a*4+phase);
  const x=r.x+Math.cos(a)*(r.rx-inset)*noise,y=r.y+Math.sin(a)*(r.ry-inset)*noise;
  i?c.lineTo(x,y):c.moveTo(x,y);
 }c.closePath();
};
const blend=(a,b,amount)=>'#'+[0,2,4].map(i=>Math.round(parseInt(a.slice(1+i,3+i),16)*(1-amount)+parseInt(b.slice(1+i,3+i),16)*amount).toString(16).padStart(2,'0')).join('');
// Character tints come from a project's actual outputs: warm for media, parchment
// for writing, steel for code and deployments. Untouched projects keep their palette.
const TINTS={media:['#4a3a22','#e6b96f'],docs:['#3d3a2c','#d9d2a8'],code:['#243c4a','#9dc4dc']};
exports.tinted=(seed,character)=>{const [fill,ink]=exports.palette(seed);const t=TINTS[character];return t?[blend(fill,t[0],.45),blend(ink,t[1],.5)]:[fill,ink];};
exports.surface=(c,r,seed,phase,paletteSeed=seed,character=null)=>{
 const [fill,ink]=exports.tinted(paletteSeed,character);exports.contour(c,r,seed,phase);
 const g=c.createLinearGradient(r.x-r.rx,r.y-r.ry,r.x+r.rx,r.y+r.ry);g.addColorStop(0,fill);g.addColorStop(1,'#122b33');
 c.fillStyle=g;c.fill();c.strokeStyle=ink+'88';c.lineWidth=1.6;c.stroke();
 exports.contour(c,r,seed,phase,7);c.strokeStyle=ink+'20';c.lineWidth=.7;c.stroke();
 c.save();exports.contour(c,r,seed,phase,12);c.clip();c.fillStyle=ink+'22';
 for(let i=0;i<35;i++){const a=(i*2.399+seed%31),rad=Math.sqrt((i+.5)/35);c.fillRect(r.x+Math.cos(a)*r.rx*rad,r.y+Math.sin(a)*r.ry*rad,1.5,1.5);}c.restore();
};
exports.neck=(c,a,b,seed)=>{
 const [fill,ink]=exports.palette(seed);c.lineCap='round';
 const trace=()=>{c.beginPath();c.moveTo(a.x,a.y);c.bezierCurveTo(a.x+(b.x-a.x)*.4,a.y+28,b.x-(b.x-a.x)*.4,b.y-28,b.x,b.y);};
 trace();c.strokeStyle=ink+'55';c.lineWidth=81;c.stroke();trace();c.strokeStyle=fill;c.lineWidth=78;c.stroke();
 trace();c.strokeStyle=ink+'30';c.lineWidth=1;c.stroke();c.lineCap='butt';
};
exports.branch=(c,x,y)=>{c.save();c.translate(x,y);c.strokeStyle='#c8d6b4';c.lineWidth=1.5;c.beginPath();c.moveTo(-5,8);c.lineTo(-5,-8);c.moveTo(-5,3);c.quadraticCurveTo(8,3,8,-7);c.stroke();for(const [x,y] of [[-5,-8],[-5,8],[8,-7]]){c.beginPath();c.arc(x,y,2.5,0,7);c.stroke();}c.restore();};
exports.badge=(c,x,y,action,status,phase,alpha=1)=>{
 c.save();c.translate(x,y);c.globalAlpha=alpha;c.fillStyle='#0c202b';c.strokeStyle=status==='failed'?'#ef9bac':status==='running'?'#e9cc88':'#96bdb5';c.lineWidth=1.5;
 c.beginPath();c.roundRect(-14,-12,28,24,7);c.fill();c.stroke();c.beginPath();
 if(['read','search','network','status','is-active'].includes(action)){c.ellipse(0,0,9,5,0,0,7);c.stroke();c.beginPath();c.arc(0,0,2.5,0,7);c.fillStyle=c.strokeStyle;c.fill();}
 else if(['edit','write','create','delete'].includes(action)){
  c.moveTo(-7,6);c.lineTo(6,-7);c.lineTo(9,-4);c.lineTo(-4,9);c.closePath();c.stroke();
  if(action==='create'||action==='delete'){c.moveTo(-8,-7);c.lineTo(-2,-7);if(action==='create'){c.moveTo(-5,-10);c.lineTo(-5,-4);}c.stroke();}
 }else if(['commit','push'].includes(action)){c.moveTo(0,-7);c.lineTo(7,0);c.lineTo(0,7);c.lineTo(-7,0);c.closePath();c.stroke();if(action==='push'){c.moveTo(1,0);c.lineTo(10,-9);c.lineTo(5,-9);c.moveTo(10,-9);c.lineTo(10,-4);c.stroke();}}
 else if(['test','build'].includes(action)){c.moveTo(-8,0);c.lineTo(-2,6);c.lineTo(8,-7);c.stroke();}
 else if(['restart','start','stop','reload'].includes(action)){c.arc(0,0,7,.4,5.8);c.stroke();c.beginPath();c.moveTo(8,-7);c.lineTo(8,0);c.lineTo(2,-3);c.stroke();}
 else {c.moveTo(-8,-5);c.lineTo(-2,0);c.lineTo(-8,5);c.moveTo(1,6);c.lineTo(9,6);c.stroke();}
 if(status==='running'){c.beginPath();c.arc(0,0,19,phase,phase+4);c.stroke();}
 else if(status==='failed'){c.beginPath();c.moveTo(9,8);c.lineTo(16,15);c.moveTo(16,8);c.lineTo(9,15);c.stroke();}
 else if(status==='succeeded'){c.beginPath();c.moveTo(8,12);c.lineTo(11,15);c.lineTo(17,8);c.stroke();}
 c.restore();
};
exports.machine=(c,r,e,state,time,reduced)=>{
 c.save();c.translate(r.x+52,r.y-5);const run=state==='running';c.strokeStyle='#a4d1cd';c.fillStyle='#18303a';c.lineWidth=2;
 c.beginPath();c.roundRect(-30,-42,60,84,12);c.fill();c.stroke();
 for(let i=0;i<3;i++){c.beginPath();c.roundRect(-22,-31+i*24,44,17,4);c.stroke();c.fillStyle=state==='failed'?'#ed99ac':run?'#f3d59b':'#8eafa9';c.beginPath();c.arc(-13,-23+i*24,2.5,0,7);c.fill();}
 c.translate(28,25);if(run&&!reduced)c.rotate(time/650);c.fillStyle='#24444a';c.beginPath();
 for(let i=0;i<32;i++){const a=i/32*Math.PI*2,rad=i%4<2?19:15;i?c.lineTo(Math.cos(a)*rad,Math.sin(a)*rad):c.moveTo(Math.cos(a)*rad,Math.sin(a)*rad);}c.closePath();c.fill();c.stroke();c.beginPath();c.arc(0,0,6,0,7);c.stroke();c.restore();
};
exports.document=(c,f)=>{
 const p=(f.purpose||'').toLowerCase();c.strokeStyle='#779b90';c.lineWidth=2;c.beginPath();
 if(/source|interface/.test(p)){c.moveTo(-7,-8);c.lineTo(-16,0);c.lineTo(-7,8);c.moveTo(7,-8);c.lineTo(16,0);c.lineTo(7,8);c.moveTo(3,-11);c.lineTo(-3,11);}
 else if(/image|artwork/.test(p)){c.moveTo(-17,12);c.lineTo(-5,-4);c.lineTo(2,4);c.lineTo(10,-8);c.lineTo(19,12);c.closePath();}
 else if(/test/.test(p)){c.moveTo(-14,0);c.lineTo(-4,10);c.lineTo(15,-11);}
 else for(let j=0;j<4;j++){c.moveTo(-14,-9+j*6);c.lineTo(14-(j%2)*7,-9+j*6);}c.stroke();
};

// Rim ornament for a project's character: small frames, ruled lines or rivets
// along the upper contour. Quiet, deterministic, and only when outputs exist.
exports.ornament=(c,r,character,ink)=>{
 if(!character)return;c.save();c.strokeStyle=ink+'aa';c.fillStyle=ink+'aa';c.lineWidth=1;
 for(let i=0;i<4;i++){const a=-2.35+i*.28,x=r.x+Math.cos(a)*(r.rx-16),y=r.y+Math.sin(a)*(r.ry-16);
  if(character==='media'){c.beginPath();c.rect(x-5,y-4,10,8);c.stroke();}
  else if(character==='docs'){c.beginPath();c.moveTo(x-6,y-2);c.lineTo(x+6,y-2);c.moveTo(x-6,y+2);c.lineTo(x+3,y+2);c.stroke();}
  else {c.beginPath();c.arc(x,y,2.2,0,7);c.fill();}
 }c.restore();
};
// Validation on the workshop itself. A crack is an interruption that stays until
// an evidenced rerun succeeds; the seam it leaves fades over half an hour.
exports.kiln=(c,r,v,time,t,reduced)=>{
 if(!v||v.state==='none')return;
 // Right shoulder of the lobe, clear of the project title above it.
 const a0=-.78,a1=-.08,lampA=-.43,lx=r.x+Math.cos(lampA)*(r.rx-8),ly=r.y+Math.sin(lampA)*(r.ry-8);
 c.save();c.lineCap='round';
 const along=(jag)=>{c.beginPath();for(let i=0;i<=10;i++){const a=a0+(a1-a0)*i/10,j=jag?((i*7)%3-1)*3:0;const x=r.x+Math.cos(a)*(r.rx-3+j),y=r.y+Math.sin(a)*(r.ry-3+j);i?c.lineTo(x,y):c.moveTo(x,y);}};
 if(v.state==='failed'){c.strokeStyle='#ed9eb0';c.lineWidth=2.2;along(true);c.stroke();c.fillStyle='#ed9eb0';c.beginPath();c.arc(lx,ly,4,0,7);c.fill();}
 else if(v.state==='recovered'){c.globalAlpha=Math.max(.12,1-Math.max(0,t-v.recoveredAt)/1800000);c.strokeStyle='#99e1b3';c.lineWidth=1.4;along(false);c.stroke();c.fillStyle='#99e1b3';c.beginPath();c.arc(lx,ly,3.5,0,7);c.fill();}
 else if(v.state==='running'){c.strokeStyle='#e9cc88';c.lineWidth=1.6;const p=reduced?0:time/600;c.beginPath();c.arc(lx,ly,8,p,p+4.4);c.stroke();c.fillStyle='#e9cc88';c.beginPath();c.arc(lx,ly,2.5,0,7);c.fill();}
 else if(v.state==='ok'){c.globalAlpha=Math.max(0,1-Math.max(0,t-v.okAt)/600000);c.fillStyle='#99e1b3';c.beginPath();c.arc(lx,ly,3.5,0,7);c.fill();}
 c.restore();
};
// Structural origin: a still double rail with anchors. It is a configured fact,
// so it never moves on its own; deliveries travel along it.
exports.rail=(c,path,a,b)=>{
 c.save();c.lineCap='round';path(c,a,b);c.strokeStyle='#bda7d366';c.lineWidth=4;c.stroke();path(c,a,b);c.strokeStyle='#0d1f2a';c.lineWidth=2;c.stroke();
 c.fillStyle='#bda7d3aa';for(const p of [a,b]){c.beginPath();c.arc(p.x,p.y,2.6,0,7);c.fill();}c.restore();
};
