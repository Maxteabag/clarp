import {FrameDeadline} from './viz-deadline.js';
// Arbitrary authored JavaScript runs in a worker inside an opaque-origin frame.
// The host accepts bitmap frames and inert metadata, never generated DOM/code.
const bootstrap = `
const workerSource = ${JSON.stringify(`
let render, canvas, ctx, clock=0, seed=1234;
let avatars={},avatarVersion=0,images={},imageVersion=0,scene={entities:[],relations:[],events:[]};
const monotonic=performance.now.bind(performance);
Math.random=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};
Date.now=()=>clock;
self.Worker=undefined;self.SharedWorker=undefined;
function compile(program){
 const modules={};
 function load(path,from=''){
  const parts=(path.startsWith('.')?from.split('/').slice(0,-1).concat(path.split('/')):path.split('/'));
  const clean=[];for(const p of parts){if(p==='..')clean.pop();else if(p&&p!=='.')clean.push(p);}
  const key=clean.join('/');if(!Object.hasOwn(program.files,key))throw Error('Missing module '+key);
  if(modules[key])return modules[key].exports;
  const module={exports:{}};modules[key]=module;
  new Function('module','exports','require',program.files[key])(module,module.exports,p=>load(p,key));
  return module.exports;
 }
 return load(program.entry).render;
}
self.onmessage=async({data})=>{
 try{
  if(data.type==='init'){
   render=compile(data.program);if(typeof render!=='function')throw Error('Entry must export render');
   canvas=new OffscreenCanvas(1,1);ctx=canvas.getContext('2d');self.postMessage({type:'ready'});return;
  }
  if(data.type==='scene'){scene=data.scene;return;}
  if(data.type==='avatars'){
   const version=++avatarVersion;
   const next={};
   for(const [id,blob] of Object.entries(data.avatars||{})){
    try{next[id]=await createImageBitmap(blob);}catch{}
   }
   if(version!==avatarVersion){for(const image of Object.values(next))image.close();return;}
   for(const image of Object.values(avatars))image.close();
   avatars=next;return;
  }
  if(data.type==='images'){
   // Host-owned artifact previews: decoded once, keyed by artifact id.
   const version=++imageVersion;
   const next={};
   for(const [id,blob] of Object.entries(data.images||{})){
    try{next[id]=await createImageBitmap(blob);}catch{}
   }
   if(version!==imageVersion){for(const image of Object.values(next))image.close();return;}
   for(const image of Object.values(images))image.close();
   images=next;return;
  }
  const started=monotonic();
  clock=data.playhead;seed=1234;
  canvas.width=data.width;canvas.height=data.height;
  const meta=render({...data,scene,ctx,avatars,images})||{};
  meta.loadedAvatars=Object.keys(avatars);meta.loadedImages=Object.keys(images);
  const bitmap=canvas.transferToImageBitmap();
  self.postMessage({type:'frame',request:data.request,bitmap,meta,executionMs:monotonic()-started},[bitmap]);
 }catch(e){self.postMessage({type:'error',error:String(e.message||e).slice(0,500)});}
};
`)};
let worker;
addEventListener('message',e=>{
 if(e.source!==parent)return;
 if(e.data.type==='init'){
  worker?.terminate();
  worker=new Worker(URL.createObjectURL(new Blob([workerSource],{type:'text/javascript'})));
  worker.onmessage=({data})=>parent.postMessage(data,'*',data.bitmap?[data.bitmap]:[]);
  worker.onerror=()=>parent.postMessage({type:'error',error:'Source worker failed'},'*');
 }
 worker?.postMessage(e.data);
});
parent.postMessage({type:'boot'},'*');
`;
export class SourceSandbox {
  constructor(program,onframe,onerror){
    this.frame=document.createElement('iframe');this.frame.hidden=true;
    this.frame.sandbox='allow-scripts';
    this.avatars={};this.images={};this.frameTimings={};this.ready=false;this.pending=false;this.stopped=false;this.sequence=0;
    const fail=(error,kind='source')=>{if(this.stopped)return;this.destroy();onerror(error,kind);};
    this.deadline=new FrameDeadline(()=>fail('The render worker stopped responding','delivery'));
    this.initDeadline=new FrameDeadline(()=>fail('The render worker could not start','startup'),{deliveryMs:10000});
    this.visibility=()=>{
      // Suspend the worker itself, not just its watchdog. A runaway program
      // cannot consume CPU indefinitely while this tab is hidden.
      if(document.hidden){this.destroy();return;}
      this.deadline.visibilityChanged();this.initDeadline.visibilityChanged();
    };
    document.addEventListener('visibilitychange',this.visibility);
    this.listener=e=>{
      if(e.source!==this.frame.contentWindow||this.stopped)return;
      if(e.data.type==='boot')this.frame.contentWindow.postMessage({type:'init',program},'*');
      else if(e.data.type==='ready'){this.initDeadline.clear();this.ready=true;this.setAvatars(this.avatars);this.setImages(this.images);}
      else if(e.data.type==='error')fail(e.data.error);
      else if(e.data.type==='frame'&&e.data.request===this.sequence){
        this.deadline.clear();this.pending=false;
        this.frameTimings={executionMs:e.data.executionMs,deliveryMs:performance.now()-this.sentAt};
        // A slow completed frame is usable; pace subsequent requests instead
        // of mistaking load for an infinite loop. The external fuse still
        // terminates source that never returns.
        this.nextFrameAt=performance.now()+Math.max(0,(e.data.executionMs||0)-33);
        if(!(e.data.bitmap instanceof ImageBitmap))return fail('Invalid frame');
        onframe(e.data.bitmap,e.data.meta,this.frameTimings);
      }
    };
    addEventListener('message',this.listener);
    // Opaque origin denies host storage; CSP denies network and nested content.
    this.frame.srcdoc='<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; script-src &#39;unsafe-inline&#39; &#39;unsafe-eval&#39;; worker-src blob:; connect-src &#39;none&#39;"><script>'+bootstrap.replaceAll('</script','<\\/script')+'</script>';
    document.body.append(this.frame);
    this.initDeadline.arm(0);
    this.fail=fail;
  }
  setAvatars(avatars){
    this.avatars=avatars;
    if(this.ready&&!this.stopped)this.frame.contentWindow.postMessage({type:'avatars',avatars},'*');
  }
  setImages(images){
    this.images=images;
    if(this.ready&&!this.stopped)this.frame.contentWindow.postMessage({type:'images',images},'*');
  }
  draw(input){
    if(!this.ready||this.pending||this.stopped||document.hidden||performance.now()<(this.nextFrameAt||0))return;
    this.pending=true;this.sequence++;
    this.sentAt=performance.now();
    const {scene,...frame}=input;
    if(scene!==this.scene){this.scene=scene;this.frame.contentWindow.postMessage({type:'scene',scene},'*');}
    this.frame.contentWindow.postMessage({...frame,type:'frame',request:this.sequence},'*');
    this.deadline.arm(this.sequence);
  }
  destroy(){this.stopped=true;this.initDeadline.clear();this.deadline.clear();document.removeEventListener('visibilitychange',this.visibility);removeEventListener('message',this.listener);this.frame.remove();}
}
