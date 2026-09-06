// Agent portraits replace boat markers; identity, routes and action colors stay.
module.exports.drawAgentAvatar=(c,x,y,name,col,phase,active,image)=>{
  c.save();c.translate(x,y);
  const r=27;
  c.shadowColor=col;c.shadowBlur=active?14:0;
  c.fillStyle='#102a32';c.beginPath();c.arc(0,0,r+3,0,Math.PI*2);c.fill();c.shadowBlur=0;
  c.save();c.beginPath();c.arc(0,0,r,0,Math.PI*2);c.clip();
  if(image)c.drawImage(image,-r,-r,r*2,r*2);
  else{
    c.fillStyle='#24434b';c.fillRect(-r,-r,r*2,r*2);
    const initials=String(name||'?').trim().split(/\s+/).map(s=>s[0]).slice(0,2).join('').toUpperCase();
    c.fillStyle='#e7e8dc';c.font='600 18px sans-serif';c.textAlign='center';c.textBaseline='middle';c.fillText(initials,0,0);
  }
  c.restore();
  c.strokeStyle=col;c.lineWidth=2.5;c.beginPath();c.arc(0,0,r+2,0,Math.PI*2);c.stroke();
  if(active){c.fillStyle=col;c.beginPath();c.arc(r-2,r-2,5,0,Math.PI*2);c.fill();c.strokeStyle='#102a32';c.lineWidth=2;c.stroke();}
  c.font='600 13px sans-serif';c.textBaseline='alphabetic';c.textAlign='left';
  const w=c.measureText(name).width+20;
  c.beginPath();c.roundRect(-w/2,-58,w,23,8);c.fillStyle='#0b1c23';c.fill();c.strokeStyle=col;c.lineWidth=1;c.stroke();
  c.fillStyle='#e7e8dc';c.fillText(name,-w/2+10,-42);c.restore();
};
