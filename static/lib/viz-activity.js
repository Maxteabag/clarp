const panel=document.getElementById('activity-feed'),list=document.getElementById('activity-items'),pause=document.getElementById('activity-pause'),status=document.getElementById('activity-status');
let items=[],cursor=0,paused=false,reading=false,busy=false,previousTime=0,previousMode='',generation=0;
if(innerWidth<900||innerHeight<600)panel.open=false;
function placeFeed(){
 const bar=document.getElementById('bar').getBoundingClientRect(),hud=document.getElementById('hud').getBoundingClientRect();
 panel.style.bottom=(bar.height+12)+'px';list.style.maxHeight=Math.max(50,Math.min(235,innerHeight-bar.height-hud.bottom-105))+'px';
 document.documentElement.style.setProperty('--viz-toolbar-height',bar.height+'px');
}
new ResizeObserver(placeFeed).observe(document.getElementById('bar'));addEventListener('resize',placeFeed);placeFeed();
document.addEventListener('fleet-recap',e=>{reading=!!e.detail;});
const el=(tag,text)=>{const node=document.createElement(tag);if(text!=null)node.textContent=text;return node;};
function color(id){let n=0;for(const c of id)n=(n*31+c.charCodeAt(0))>>>0;return `hsl(${n%360} 62% 74%)`;}
function render(){
 const follow=list.scrollHeight-list.scrollTop-list.clientHeight<35;
 list.replaceChildren();
 for(const item of items){const row=el('li'),head=el('header'),name=el('strong',item.agent),time=el('time',new Date(item.ts).toLocaleTimeString());
  row.style.setProperty('--agent-color',color(item.agent_id));head.append(name,time);time.title=new Date(item.ts).toLocaleString();row.append(head,el('small',item.kind+(item.status?' · '+item.status:'')));
  if(item.explanation||item.text!==item.kind)row.append(el('p',item.explanation||item.text));
  if(item.explanation||item.details){const details=el('details');details.append(el('summary',item.explanation?'Tool call':'Recorded details'),el('pre',item.explanation?item.text+'\n'+item.details:item.details));row.append(details);}
  list.append(row);
 }
 if(follow&&!paused)list.scrollTop=list.scrollHeight;
}
function setPaused(value){if(value&&!paused)generation++;paused=value;pause.textContent=value?'Resume':'Pause';}
pause.onclick=()=>{setPaused(!paused);if(!paused){cursor=0;items=[];generation++;poll();}};
list.addEventListener('wheel',()=>setPaused(true),{passive:true});
list.addEventListener('touchstart',()=>setPaused(true),{passive:true});
list.addEventListener('click',e=>{if(e.target.closest('details'))setPaused(true);});
async function poll(earlier=false){
 if(busy){if(earlier)setTimeout(()=>poll(true),100);return;}
 if(reading||document.hidden||!panel.open||(paused&&!earlier))return;
 const snapshot=window.fleetWorldSnapshot?.();if(!snapshot)return;
 const mode=snapshot.demoEnabled?'demo':snapshot.live?'live':'replay';
 if(mode!==previousMode||snapshot.playhead<previousTime){items=[];cursor=0;generation++;}previousMode=mode;previousTime=snapshot.playhead;
 if(mode==='demo'){
  items=snapshot.scene.events.filter(e=>e.ts<=snapshot.playhead).slice(-400).map(e=>({id:e.id,agent:e.agent,agent_id:e.agent_id,ts:e.ts,kind:e.action,status:e.outcome||'observed',text:e.evidence?.raw||e.action,details:''}));
  render();status.textContent='Synthetic demo activity';return;
 }
 busy=true;const seq=generation;
 try{
  const anchor=mode==='live'?Date.now():Math.floor(snapshot.playhead);
  const query=earlier&&items.length?'&before='+items[0].id:mode==='live'&&cursor?'&after='+cursor:'';
  const response=await fetch('/viz/activity?until='+anchor+query);if(!response.ok)throw Error('Activity unavailable; retrying');
  const data=await response.json();if(seq!==generation)return;
  const merged=new Map((mode==='replay'&&!earlier?[]:items).map(i=>[i.id,i]));for(const item of data.items)merged.set(item.id,item);
  const ordered=[...merged.values()].sort((a,b)=>a.id-b.id);items=earlier?ordered.slice(0,400):ordered.slice(-400);
  if(!earlier)cursor=data.cursor;render();status.textContent=`${mode==='live'?'Live':'Replay'} · ${items.length} retained records · explanations when cached`;
  if(earlier){setPaused(true);list.scrollTop=0;}
  if(data.more&&query.includes('after'))setTimeout(()=>poll(),50);
 }catch(error){status.textContent=error.message;}finally{busy=false;}
}
document.getElementById('activity-earlier').onclick=()=>{setPaused(true);poll(true);};
panel.addEventListener('toggle',()=>{if(panel.open)poll();});setInterval(()=>poll(),1000);poll();
