exports.color=action=>({read:'#83d9ee',search:'#83d9ee',edit:'#f4c078',write:'#f4c078',create:'#99e1b3',delete:'#ed9eb0',test:'#b6c9ff',build:'#b6c9ff',push:'#c7acf3',commit:'#e9d29b',network:'#83d9ee'}[action]||'#a5bec6');
exports.path=(c,a,b,bend=35)=>{c.beginPath();c.moveTo(a.x,a.y);c.bezierCurveTo(a.x+(b.x-a.x)*.35,a.y-bend,b.x-(b.x-a.x)*.25,b.y-bend,b.x,b.y);};
exports.point=(a,b,t,bend=35)=>{const q=1-t;return{x:q*q*q*a.x+3*q*q*t*(a.x+(b.x-a.x)*.35)+3*q*t*t*(b.x-(b.x-a.x)*.25)+t*t*t*b.x,y:q*q*q*a.y+3*q*q*t*(a.y-bend)+3*q*t*t*(b.y-bend)+t*t*t*b.y};};
exports.region=(c,x,y,rx,ry,phase=0)=>{
 c.beginPath();for(let i=0;i<=90;i++){const a=i/90*Math.PI*2,r=1+.035*Math.sin(a*3+phase)+.022*Math.cos(a*5-phase*.5);const xx=x+Math.cos(a)*rx*r,yy=y+Math.sin(a)*ry*r;i?c.lineTo(xx,yy):c.moveTo(xx,yy);}c.closePath();
};
exports.beam=(c,a,b,action,phase,amount=1,failed=false,reduced=false)=>{
 const color=failed?'#f0879f':exports.color(action);c.save();c.strokeStyle=color;c.lineWidth=failed?1.5:2;c.globalAlpha=.12*amount;exports.path(c,a,b);c.stroke();
 if(failed){c.globalAlpha=.8;c.setLineDash([7,12]);exports.path(c,a,b);c.stroke();c.setLineDash([]);const p=exports.point(a,b,.6);c.beginPath();c.moveTo(p.x-6,p.y-6);c.lineTo(p.x+6,p.y+6);c.moveTo(p.x+6,p.y-6);c.lineTo(p.x-6,p.y+6);c.stroke();}
 else for(let j=0;j<4;j++){
  let t=reduced?(j+1)/5:(phase+j*.22)%1;if(['read','search'].includes(action))t=1-t;
  const p=exports.point(a,b,t);c.globalAlpha=(.3+.7*Math.sin(Math.PI*t))*amount;c.fillStyle=color;c.shadowColor=color;c.shadowBlur=9;
  c.beginPath();c.ellipse(p.x,p.y,action==='read'?3:4,action==='read'?3:2,0,0,7);c.fill();
 }c.restore();
};
exports.action=(c,file,e,time,reduced=false)=>{
 if(!e)return;
 const state=e.finished_at!=null&&time>=e.finished_at?e.outcome:(e.finished_at!=null||e.outcome==='running'?'running':'unknown');
 const running=state==='running',age=time-(e.finished_at??e.ts),recent=running||age<8000;
 if(!recent)return;
 const p=reduced?.5:(time-e.ts)/1200%1,color=state==='failed'?'#f0879f':exports.color(e.action);
 c.save();c.strokeStyle=color;c.fillStyle=color;c.lineWidth=2;
 if(state==='failed'){
  c.beginPath();c.arc(file.x,file.y,49,Math.PI*.12,Math.PI*1.85);c.stroke();c.beginPath();c.moveTo(file.x+34,file.y-39);c.lineTo(file.x+46,file.y-27);c.moveTo(file.x+46,file.y-39);c.lineTo(file.x+34,file.y-27);c.stroke();
 }else if(e.action==='read'||e.action==='search'){
  c.globalAlpha=running?.7:Math.max(0,1-age/8000)*.5;const y=file.y-29+p*58;c.fillRect(file.x-34,y,66,2);c.globalAlpha*=.12;c.fillRect(file.x-34,y-8,66,16);
 }else if(['edit','write'].includes(e.action)){
  for(let i=0;i<4;i++){const x=file.x-25+i*15;c.beginPath();c.moveTo(x,file.y+25);c.lineTo(x+7,file.y+18-(running?Math.sin(p*6+i)*3:0));c.stroke();}
 }else if(e.action==='test'||e.action==='build'){
  c.beginPath();c.arc(file.x,file.y,49,reduced?0:p*6.28,(reduced?0:p*6.28)+(running?4.4:6.28));c.stroke();
  if(state==='succeeded'){c.strokeStyle='#99e1b3';c.beginPath();c.moveTo(file.x+29,file.y-29);c.lineTo(file.x+36,file.y-22);c.lineTo(file.x+48,file.y-38);c.stroke();}
 }else if(e.action==='commit'){
  for(let i=0;i<3;i++){c.beginPath();c.roundRect(file.x-24+i*8,file.y-18+i*12,40,11,3);c.stroke();}
 }else if(e.action==='create'&&state==='succeeded'){
  for(let i=0;i<8;i++){const a=i/8*6.28,r=reduced?45:45+Math.min(age/15,60);c.globalAlpha=Math.max(0,1-age/2500);c.beginPath();c.arc(file.x+Math.cos(a)*r,file.y+Math.sin(a)*r,2,0,7);c.fill();}
 }else if(e.action==='delete'&&state==='succeeded'){
  for(let i=0;i<10;i++){c.globalAlpha=Math.max(0,1-age/3500);const a=i/10*6.28,r=20+(reduced?10:age/50);c.fillRect(file.x+Math.cos(a)*r,file.y+Math.sin(a)*r,3,4);}
 }
 c.restore();
};
