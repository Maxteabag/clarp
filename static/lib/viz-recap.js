import {periodRange,safeUrl,loadReview,markSeen,isUnread} from '/static/lib/viz-recap-model.js';
import {resolveAvatarUrl} from '/static/lib/avatar.js';
const dialog=document.getElementById('recap'),list=document.getElementById('recap-list'),status=document.getElementById('recap-status');
const period=document.getElementById('recap-period'),done=document.getElementById('recap-done'),reader=document.getElementById('recap-reader');
let storage;try{storage=localStorage;}catch{}
let review=loadReview(storage),data=null,sequence=0,controller=null,readerSequence=0;
reader.addEventListener('close',()=>readerSequence++);
const el=(tag,text,className)=>{const e=document.createElement(tag);if(text!=null)e.textContent=text;if(className)e.className=className;return e;};
const date=ts=>new Date(ts).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
function save(artifacts,until){
 review=markSeen(review,artifacts,until);
 try{storage.setItem('clarp.fleet.recap',JSON.stringify(review));}catch{document.getElementById('recap-storage').textContent='Review marks last only for this visit because browser storage is unavailable.';}
 render();
}
function link(label,url){const a=el('a',label);a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a;}
function contentText(text){
 if(window.marked&&window.DOMPurify){
  const section=el('div',null,'recap-document');
  section.innerHTML=window.DOMPurify.sanitize(window.marked.parse(text),{ALLOWED_TAGS:['p','h1','h2','h3','h4','strong','em','ul','ol','li','blockquote','pre','code','a','table','thead','tbody','tr','td','th','hr','br'],ALLOWED_ATTR:['href','title']});
  section.querySelectorAll('a').forEach(a=>{const url=safeUrl(a.getAttribute('href'));if(url){a.href=url;a.target='_blank';a.rel='noopener noreferrer';}else a.removeAttribute('href');});
  return section;
 }
 const section=el('div');section.append(el('pre',text,'recap-text'));
 // Keep recorded Markdown readable as text and expose its original safe links.
 const links=[...text.matchAll(/\[([^\]]+)\]\((https:\/\/[^\s)]+)\)/g)].slice(0,30);
 for(const [,label,url] of links){const safe=safeUrl(url);if(safe)section.append(link(label,safe));}
 return section;
}
async function readArtifact(a){
 const seq=++readerSequence;
 const loading=el('p','Loading artifact…');reader.replaceChildren(loading);reader.showModal();
 const close=el('button','Close artifact');close.onclick=()=>reader.close();reader.prepend(close);
 try{
  const response=await fetch('/viz/recap/artifact?id='+encodeURIComponent(a.id));if(!response.ok)throw Error('This artifact is no longer available.');
  const value=await response.json();if(!reader.open||seq!==readerSequence)return;
  loading.remove();reader.append(el('h2',value.title));
  if(value.type==='html_form'){
   reader.append(el('p','Read-only preview. Interactive submissions are disabled here.'));
   const frame=el('iframe');frame.title=value.title;frame.setAttribute('sandbox','');frame.referrerPolicy='no-referrer';
   const doc=new DOMParser().parseFromString(value.content,'text/html');
   doc.querySelectorAll('script,iframe,object,embed,base,meta[http-equiv],link').forEach(e=>e.remove());
   doc.querySelectorAll('[href],[action],[formaction]').forEach(e=>{e.removeAttribute('href');e.removeAttribute('action');e.removeAttribute('formaction');});
   frame.srcdoc='<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:; form-action \'none\'; base-uri \'none\'">'+doc.documentElement.outerHTML;
   reader.append(frame);
  }else reader.append(contentText(value.content||'No readable content was recorded.'));
  for(const source of value.sources||[]){const url=safeUrl(source.url);if(url)reader.append(link(source.title,url));}
  save([a]);
 }catch(error){if(reader.open&&seq===readerSequence)reader.append(el('p',error.message));}
}
function render(){
 if(!data)return;list.replaceChildren();
 const unread=data.groups.flatMap(g=>g.artifacts).filter(a=>isUnread(a,review)).length;
 document.getElementById('recap-summary').textContent=`${data.counts.agents} agents · ${data.counts.artifacts} recorded outputs · ${unread} unseen · ${data.counts.attention} decisions`;
 document.getElementById('recap-range').textContent=`${date(data.since)} — ${date(data.until)}`;
 for(const [index,g] of data.groups.entries()){
  const section=el('section',null,'recap-agent'),header=el('header',null,'recap-agent-heading');
  section.id='recap-agent-'+index;
  const portrait=el('span',g.name.split(/\s+/).map(w=>w[0]).slice(0,2).join(''),'recap-portrait');
  const avatar=g.avatar_url||resolveAvatarUrl({},g.name);
  if(/^\/(avatars|static\/avatars)\/[\w.-]+$/.test(avatar||'')){const img=el('img');img.src=avatar;img.alt='';img.loading='lazy';img.onerror=()=>img.remove();portrait.append(img);}
  const heading=el('div');heading.append(el('h2',g.name),el('p',g.archived?'Archived agent · work retained':g.session||'Recorded work'));
  header.append(portrait,heading);
  if(g.latest)header.append(link('See this period on the map',`/viz?view=flow&window=1800&until=${Math.min(data.generated_at,g.latest+10000)}`));
  section.append(header);
  if(g.request){const details=el('details',null,'recap-request');details.append(el('summary','You last asked · '+date(g.request.at)),el('blockquote',g.request.text));section.append(details);}
  const shelf=el('div',null,'recap-shelf');
  const builds=el('details',null,'recap-builds');builds.append(el('summary','Builds and checks'));
  const buildShelf=el('div',null,'recap-shelf');builds.append(buildShelf);
  for(const a of g.artifacts){
   const card=el('article',null,'recap-artifact'),label=el('div',null,'recap-artifact-label');
   label.append(el('span',a.type.replaceAll('_',' ')),el('span',isUnread(a,review)?'Unseen':'Reviewed'));
   card.append(label);
   if(a.preview?.url&&/^\/media\/[\w-]+$/.test(a.preview.url)){const img=el('img');img.src=a.preview.url;img.loading='lazy';img.alt=a.title;img.onerror=()=>img.remove();card.append(img);}
   card.append(el('h3',a.title),el('time',date(Math.max(a.created_at,a.updated_at))),el('p',a.summary||'Published by '+g.name));
   if(a.type==='workflow_run')card.append(el('small','Recorded result: '+(a.run?.conclusion||a.status)));
   const actions=el('div',null,'recap-actions'),url=safeUrl(a.link);
   if(a.readable){const read=el('button','Read artifact');read.onclick=()=>readArtifact(a);actions.append(read);}
   if(url){const open=link(a.type==='audio'?'Open audio':a.type==='video'?'Open video':'Open deliverable',url);open.onclick=()=>save([a]);actions.append(open);}
   if(!a.readable&&!url)actions.append(el('small','No openable content was recorded.'));
   if(isUnread(a,review)){const seen=el('button','Mark reviewed');seen.onclick=()=>save([a]);actions.append(seen);}
   card.append(actions);(a.type==='workflow_run'?buildShelf:shelf).append(card);
  }
  if(shelf.childElementCount)section.append(shelf);else section.append(el('p','No published deliverables in this period.','recap-empty'));
  if(buildShelf.childElementCount){builds.querySelector('summary').textContent=`Builds and checks (${buildShelf.childElementCount})`;section.append(builds);}
  if(g.attention.length){const attention=el('div',null,'recap-attention');attention.append(el('h3','Needs your attention now'));
   for(const d of g.attention)attention.append(el('p',d.title+(d.blocks_progress?' · marked as blocking':'')));
   attention.append(el('small','Return to this agent in Clarp to answer.'));section.append(attention);}
  if(g.plans.length){const details=el('div',null,'recap-plans');details.append(el('h3','Where you left off'));
   for(const p of g.plans){const row=el('p');row.append(el('span',p.status.replaceAll('_',' '),'recap-task-status'),el('strong',p.title));if(p.current)row.append(el('small','In progress: '+p.current));details.append(row);}
   details.append(el('small','Agent-declared status, not independent proof of completion.'));section.append(details);}
  list.append(section);
 }
 if(!data.groups.length)list.append(el('p','Nothing recorded for this period. Try Last 7 days.','recap-empty'));
 document.getElementById('recap-coverage').textContent=data.coverage;
 status.textContent=data.truncated.length?'Showing the most recent 200 records per category. Some '+data.truncated.join(', ')+' are omitted; choose a shorter period before marking everything reviewed.':'';
 done.disabled=!!data.truncated.length;
}
async function load(){
 const seq=++sequence;controller?.abort();controller=new AbortController();data=null;done.disabled=true;list.replaceChildren();status.textContent='Gathering your work…';
 document.getElementById('recap-summary').textContent='';document.getElementById('recap-range').textContent='';
 const range=periodRange(period.value,Date.now(),review.lastReview);
 try{
  const response=await fetch(`/viz/recap?since=${range.since}&until=${range.until}`,{signal:controller.signal});if(!response.ok)throw Error('Could not load the recap. Try again.');
  const value=await response.json();if(seq!==sequence)return;data=value;render();
 }catch(error){if(seq===sequence&&error.name!=='AbortError')status.textContent=error.message;}
}
document.getElementById('catch-up').onclick=()=>{dialog.showModal();document.dispatchEvent(new CustomEvent('fleet-recap',{detail:true}));load();};
document.getElementById('recap-close').onclick=()=>dialog.close();
dialog.addEventListener('close',()=>{sequence++;controller?.abort();document.dispatchEvent(new CustomEvent('fleet-recap',{detail:false}));document.getElementById('catch-up').focus();});
period.onchange=load;document.getElementById('recap-refresh').onclick=load;
done.onclick=()=>{if(data&&!done.disabled){save(data.groups.flatMap(g=>g.artifacts),data.until);status.textContent='Caught up through '+date(data.until)+'. Next time, choose Since last review.';}};
if(new URL(location.href).searchParams.get('recap')==='1')document.getElementById('catch-up').click();
