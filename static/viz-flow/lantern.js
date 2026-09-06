// The lantern: what The Lantern Works actually makes. One piece of work is one
// lantern that keeps its seal while its material changes. Intent is an unlit
// wire frame; making adds paper panes; a running check sweeps light around the
// base; an interruption cracks a pane and dims the light to an ember; recovery
// mends the crack with a gold seam that stays; publication lights the lantern
// and the real preview shows through its front pane. Finished lanterns dim
// slowly and keep standing. Light means state, never decoration.
const {seal,typeColor,glyph}=require('./slate.js');
exports.seal=seal;exports.typeColor=typeColor;
exports.SIZES={intent:[56,78],evidence:[68,98],outcome:[90,126]};
exports.size=o=>exports.SIZES[o.stage]||exports.SIZES.intent;
const PAPER='#e8dcb4',WIRE='#8fa7a6',EMBER='#e0705a',GOLD='#e4c26a';
const alphaHex=a=>Math.round(Math.max(0,Math.min(1,a))*255).toString(16).padStart(2,'0');

// Geometry, all relative to the lantern center.
const anatomy=(w,h)=>{
 const top=-h/2,capTop=top+16,capBottom=top+27,bodyTop=capBottom,bodyBottom=h/2-17,baseBottom=h/2-8;
 const bulge=w*.06;
 const body=[[-w*.36,bodyTop],[w*.36,bodyTop],[w*.5,bodyTop+(bodyBottom-bodyTop)*.28],[w*.5-bulge*.3,bodyBottom-4],[w*.36,bodyBottom],[-w*.36,bodyBottom],[-w*.5+bulge*.3,bodyBottom-4],[-w*.5,bodyTop+(bodyBottom-bodyTop)*.28]];
 return {top,capTop,capBottom,bodyTop,bodyBottom,baseBottom,body,facets:[-w*.5,-w*.17,w*.17,w*.5]};
};
const poly=(c,pts)=>{c.beginPath();pts.forEach(([x,y],i)=>i?c.lineTo(x,y):c.moveTo(x,y));c.closePath();};

// The light: how the lantern reads from far away.
exports.light=(o,t,images)=>{
 const v=o.evidence.validation,age=o.age;
 let glow=o.stage==='intent'?0:o.stage==='evidence'?.35:.85,color='#f0c674';
 if(v.state==='failed'||v.state==='interrupted'){glow=Math.max(glow*.45,.28);color=EMBER;}
 else if(v.state==='recovered'&&t-v.recoveredAt<1800000)color='#f2d38a';
 if(o.finished&&!(v.state==='failed'||v.state==='interrupted'))glow*=Math.max(.3,1-Math.max(0,age-120000)/3600000*.7);
 if(o.finished&&!o.outcome)glow=0;
 return {glow,color};
};

const crackPath=(c,w,a)=>{c.beginPath();c.moveTo(w*.05,a.bodyTop+2);c.lineTo(w*.12,a.bodyTop+14);c.lineTo(w*.04,a.bodyTop+26);c.lineTo(w*.14,a.bodyTop+40);c.lineTo(w*.08,a.bodyTop+52);};

exports.draw=(c,o,pos,ink,images,time,t,reduced,detail=1)=>{
 const [w,h]=exports.size(o),a=anatomy(w,h),v=o.evidence.validation,{glow,color}=exports.light(o,t,images);
 const running=v.state==='running'||(o.stage!=='intent'&&!o.finished&&o.age<8000);
 const flicker=running&&!reduced?.06*Math.sin(time/90+o.seed%7)+.04*Math.sin(time/230):0;
 const dim=o.finished?Math.max(.5,1-Math.max(0,o.age-120000)/3600000*.5):1;
 c.save();c.translate(pos.x,pos.y);
 // Glow first: the whole reading at overview scale.
 if(glow>0){const g=c.createRadialGradient(0,a.bodyTop+(a.bodyBottom-a.bodyTop)*.5,w*.1,0,a.bodyTop+(a.bodyBottom-a.bodyTop)*.5,w*1.15);
  g.addColorStop(0,color+alphaHex((glow+flicker)*.55));g.addColorStop(1,color+'00');c.fillStyle=g;c.fillRect(-w*1.3,a.top-10,w*2.6,h+20);}
 c.globalAlpha=dim;
 // Base and feet
 c.fillStyle='#1b2b32';c.strokeStyle=WIRE;c.lineWidth=1.2;
 c.beginPath();c.moveTo(-w*.4,a.bodyBottom);c.lineTo(w*.4,a.bodyBottom);c.lineTo(w*.33,a.baseBottom);c.lineTo(-w*.33,a.baseBottom);c.closePath();c.fill();c.stroke();
 c.beginPath();c.moveTo(-w*.26,a.baseBottom);c.lineTo(-w*.3,a.baseBottom+7);c.moveTo(w*.26,a.baseBottom);c.lineTo(w*.3,a.baseBottom+7);c.stroke();
 // Body panes
 const lit=o.stage==='outcome'||(o.stage==='evidence'&&o.evidence.files.length>0);
 const paneCount=o.stage==='intent'?0:Math.max(1,Math.min(3,o.evidence.files.length||(o.outcome?3:1)));
 poly(c,a.body);c.fillStyle=o.stage==='intent'?'#0f2029':(o.finished&&!o.outcome?'#1a262c':'#2a3a3e');c.fill();
 const image=o.outcome?images[o.outcome.id]:null,develop=o.outcome?(reduced?1:Math.min(1,Math.max(0,t-o.outcome.created_at)/2500)):0;
 if(o.stage!=='intent'){
  c.save();poly(c,a.body);c.clip();
  for(let i=0;i<3;i++){const x0=a.facets[i],x1=a.facets[i+1],filled=i<paneCount||(o.outcome&&image);
   c.fillStyle=filled?PAPER+alphaHex(o.outcome?(image?.28:.75):.55+glow*.3):'#12232a';c.fillRect(x0,a.bodyTop,x1-x0,a.bodyBottom-a.bodyTop);}
  if(image){c.globalAlpha=dim*develop;c.drawImage(image,-w*.5,a.bodyTop,w,a.bodyBottom-a.bodyTop);
   // Seen through paper: a warm veil that lifts as the picture develops.
   c.globalAlpha=dim*(.14+(1-develop)*.8);c.fillStyle=PAPER;c.fillRect(-w*.5,a.bodyTop,w,a.bodyBottom-a.bodyTop);c.globalAlpha=dim;}
  else if(o.outcome&&detail>=1){c.globalAlpha=dim*.9;glyph(c,o,0,(a.bodyTop+a.bodyBottom)/2,Math.min(w*.7,(a.bodyBottom-a.bodyTop)*.6),time,reduced,t-o.outcome.created_at);c.globalAlpha=dim;}
  c.restore();
  // Facet ribs
  c.strokeStyle=WIRE+'aa';c.lineWidth=1;for(const x of a.facets.slice(1,3)){c.beginPath();c.moveTo(x,a.bodyTop);c.lineTo(x,a.bodyBottom);c.stroke();}
 }
 poly(c,a.body);c.strokeStyle=o.stage==='intent'?WIRE+'99':ink;c.lineWidth=o.stage==='outcome'?1.8:1.2;if(o.stage==='intent')c.setLineDash([3,3]);c.stroke();c.setLineDash([]);
 // Cap with ribs; the ribs count the changed files beyond the three front panes.
 c.fillStyle='#1b2b32';c.strokeStyle=WIRE;c.lineWidth=1.2;c.beginPath();c.moveTo(-w*.3,a.capTop);c.lineTo(w*.3,a.capTop);c.lineTo(w*.4,a.capBottom);c.lineTo(-w*.4,a.capBottom);c.closePath();c.fill();c.stroke();
 if(detail>=1){c.strokeStyle='#f4c078';c.lineWidth=1.3;for(let i=0;i<Math.min(6,o.evidence.files.length);i++){const x=-w*.25+i*w*.1;c.beginPath();c.moveTo(x,a.capTop+2);c.lineTo(x+w*.03,a.capBottom-2);c.stroke();}}
 // Stem and seal tag (a hollow tag when no plan was recorded)
 c.strokeStyle=WIRE;c.lineWidth=1.2;c.beginPath();c.moveTo(0,a.capTop);c.lineTo(0,a.top+10);c.stroke();
 if(o.intent?.none){c.strokeStyle='#b9ad86';c.beginPath();c.arc(0,a.top+6,6,0,7);c.stroke();}
 else seal(c,0,a.top+7,7,o.seed,o.finished?'#b9ad86':'#e4cf9a');
 // Crack and seam on the front pane
 if(v.state==='failed'||v.state==='interrupted'){c.strokeStyle='#1a0f10';c.lineWidth=2.2;crackPath(c,w,a);c.stroke();c.strokeStyle=EMBER;c.lineWidth=1;if(v.state==='interrupted')c.setLineDash([3,3]);crackPath(c,w,a);c.stroke();c.setLineDash([]);}
 else if(v.state==='recovered'){c.globalAlpha=dim*Math.max(.45,1-Math.max(0,t-v.recoveredAt)/1800000*.55);c.strokeStyle=GOLD;c.lineWidth=2;crackPath(c,w,a);c.stroke();c.globalAlpha=dim;}
 // Running check: light sweeps around the base ring.
 if(v.state==='running'&&detail>=1){c.strokeStyle='#e9cc88';c.lineWidth=1.8;const p=reduced?0:time/500;c.beginPath();c.ellipse(0,a.baseBottom-4,w*.44,5,0,p,p+3.6);c.stroke();}
 // Closed without an artifact: a small hollow mark on the dark front.
 if(o.finished&&!o.outcome&&o.stage!=='intent'){c.strokeStyle='#8fa7a6';c.lineWidth=1.2;c.beginPath();c.arc(0,(a.bodyTop+a.bodyBottom)/2,6,0,7);c.stroke();c.beginPath();c.moveTo(-4,(a.bodyTop+a.bodyBottom)/2+4);c.lineTo(4,(a.bodyTop+a.bodyBottom)/2-4);c.stroke();}
 if(image&&o.outcome.type==='video'&&detail>=1){c.fillStyle='#0b1c23cc';c.beginPath();c.arc(w*.34,a.bodyBottom-11,7,0,7);c.fill();c.fillStyle='#f0e6c8';c.beginPath();c.moveTo(w*.34-2.5,a.bodyBottom-15);c.lineTo(w*.34+4,a.bodyBottom-11);c.lineTo(w*.34-2.5,a.bodyBottom-7);c.closePath();c.fill();}
 if(o.outcome&&o.outcome.count>1&&detail>=1){c.fillStyle='#e7e8dc';c.font='9px sans-serif';c.textAlign='right';c.textBaseline='alphabetic';c.fillText('+'+(o.outcome.count-1),w*.46,a.bodyTop-3);}
 // Unresolved checks after publication: an ember dot on the base, never hidden by success.
 if(v.unresolved?.length&&o.outcome){c.fillStyle=EMBER;c.beginPath();c.arc(-w*.3,a.bodyBottom+4,2.6,0,7);c.fill();}
 // Research sources gather into the lantern when the report is fresh.
 if(o.outcome?.type==='research'&&detail>=1){const n=Math.min(7,o.outcome.source_count||3),k=reduced?1:Math.min(1,Math.max(0,t-o.outcome.created_at)/4000);
  c.fillStyle='#c9c2ea';for(let i=0;i<n;i++){const ang=-2.9+i/(n-1||1)*2.6,r=w*(1.5-.7*k);c.globalAlpha=dim*.8;c.beginPath();c.arc(Math.cos(ang)*r,a.bodyTop+Math.sin(ang)*r*.6,1.6,0,7);c.fill();}c.globalAlpha=dim;}
 // Title beneath the base, only when there is room to read it.
 if(detail>=1){c.fillStyle='#e7e8dc';c.font='11px Georgia';c.textAlign='center';c.textBaseline='alphabetic';
  const max=detail>=2?26:w>80?18:w>60?14:11,title=o.title.length>max?o.title.slice(0,max-1)+'…':o.title;c.fillText(title,0,h/2+12);}
 c.restore();
 return {w,h:h+16};
};
