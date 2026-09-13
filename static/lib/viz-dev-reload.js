// Injected only by viz_preview.py --reload. No writes or desktop automation.
const baseline=document.getElementById('viz-dev-revision');
let revision=JSON.parse(baseline.textContent),stopped=false;
async function check(){
 if(stopped)return;
 try{
  const response=await fetch('/viz/dev-revision',{cache:'no-store'});
  if(response.ok){
   const next=await response.json();
   if(next.code!==revision.code){
    const url=new URL(location.href);
    if(document.getElementById('recap')?.open)url.searchParams.set('recap','1');else url.searchParams.delete('recap');
    stopped=true;location.replace(url.href);return;
   }
   if(next.styles!==revision.styles){
    const loads=[];
    for(const old of document.querySelectorAll('link[rel="stylesheet"]')){
     const href=new URL(old.href);if(href.origin!==location.origin)continue;
     href.searchParams.set('dev-revision',next.styles);
     const fresh=old.cloneNode();fresh.href=href.href;
     loads.push(new Promise(resolve=>{fresh.onload=()=>{old.remove();resolve(true);};fresh.onerror=()=>{fresh.remove();resolve(false);};}));old.after(fresh);
    }
    if((await Promise.all(loads)).some(ok=>!ok)){setTimeout(check,1000);return;}
   }
   revision=next;
  }
 }catch{} // Keep the page usable while the preview is restarting.
 setTimeout(check,1000);
}
setTimeout(check,1000);
