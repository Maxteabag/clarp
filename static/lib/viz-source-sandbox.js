// Arbitrary authored JavaScript runs in a worker inside an opaque-origin frame.
// The host accepts bitmap frames and inert metadata, never generated DOM/code.
const bootstrap = `
const workerSource = ${JSON.stringify(`
let render, canvas, ctx, clock=0, seed=1234;
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
self.onmessage=({data})=>{
 try{
  if(data.type==='init'){
   render=compile(data.program);if(typeof render!=='function')throw Error('Entry must export render');
   canvas=new OffscreenCanvas(1,1);ctx=canvas.getContext('2d');self.postMessage({type:'ready'});return;
  }
  clock=data.playhead;seed=1234;
  canvas.width=data.width;canvas.height=data.height;
  const meta=render({...data,ctx})||{};
  const bitmap=canvas.transferToImageBitmap();
  self.postMessage({type:'frame',request:data.request,bitmap,meta},[bitmap]);
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
    this.ready=false;this.pending=false;this.stopped=false;this.sequence=0;
    const fail=error=>{if(this.stopped)return;this.destroy();onerror(error);};
    this.listener=e=>{
      if(e.source!==this.frame.contentWindow||this.stopped)return;
      if(e.data.type==='boot')this.frame.contentWindow.postMessage({type:'init',program},'*');
      else if(e.data.type==='ready'){clearTimeout(this.timer);this.ready=true;}
      else if(e.data.type==='error')fail(e.data.error);
      else if(e.data.type==='frame'&&e.data.request===this.sequence){
        clearTimeout(this.timer);this.pending=false;
        if(!(e.data.bitmap instanceof ImageBitmap))return fail('Invalid frame');
        onframe(e.data.bitmap,e.data.meta);
      }
    };
    addEventListener('message',this.listener);
    // Opaque origin denies host storage; CSP denies network and nested content.
    this.frame.srcdoc='<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; script-src &#39;unsafe-inline&#39; &#39;unsafe-eval&#39;; worker-src blob:; connect-src &#39;none&#39;"><script>'+bootstrap.replaceAll('</script','<\\/script')+'</script>';
    document.body.append(this.frame);
    this.timer=setTimeout(()=>fail('Source initialization deadline exceeded'),2000);
    this.fail=fail;
  }
  draw(input){
    if(!this.ready||this.pending||this.stopped)return;
    this.pending=true;this.sequence++;
    this.frame.contentWindow.postMessage({...input,type:'frame',request:this.sequence},'*');
    this.timer=setTimeout(()=>this.fail('Source frame deadline exceeded'),150);
  }
  destroy(){this.stopped=true;clearTimeout(this.timer);removeEventListener('message',this.listener);this.frame.remove();}
}
