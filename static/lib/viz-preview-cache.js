// Host-owned artifact previews. Only recorded media routes are fetched, once,
// resized to a small thumbnail and handed to the sandbox as inert image blobs.
// Generated source never receives a URL or network access.
const MAX_BYTES=8*1024*1024;
const MEDIA_ROUTE=/^\/media\/[A-Za-z0-9_-]{4,80}$/;
export const PREVIEW_SIZE={width:192,height:144};
export class PreviewCache {
  constructor(onchange,fetcher=fetch,limit=24){this.entries=new Map();this.onchange=onchange;this.fetcher=(...args)=>fetcher(...args);this.limit=limit;}
  blobs(){return Object.fromEntries([...this.entries].filter(([,e])=>e.blob).map(([id,e])=>[id,e.blob]));}
  async update(previews){
    const wanted=previews.filter(p=>p&&typeof p.id==='string'&&typeof p.url==='string'&&MEDIA_ROUTE.test(p.url)).slice(-this.limit);
    const ids=new Set(wanted.map(p=>p.id));let removed=false;
    for(const id of this.entries.keys())if(!ids.has(id)){this.entries.delete(id);removed=true;}
    if(removed)this.onchange(this.blobs());
    await Promise.all(wanted.map(async preview=>{
      const old=this.entries.get(preview.id);
      if(old?.url===preview.url&&(old.blob||Date.now()<old.retryAt))return;
      const entry={url:preview.url,blob:null,retryAt:Date.now()+120000};this.entries.set(preview.id,entry);
      try{
        const response=await this.fetcher(preview.url,{credentials:'same-origin',signal:AbortSignal.timeout(10000)});
        if(!response.ok)throw Error('Preview unavailable');
        if(Number(response.headers?.get('content-length'))>MAX_BYTES)throw Error('Preview too large');
        let blob;
        if(response.body?.getReader){
          const reader=response.body.getReader(),chunks=[];let size=0;
          try{while(true){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;
            if(size>MAX_BYTES){await reader.cancel();throw Error('Preview too large');}chunks.push(value);}}
          finally{reader.releaseLock();}
          blob=new Blob(chunks,{type:response.headers.get('content-type')||''});
        }else blob=await response.blob();
        if(blob.size>MAX_BYTES)throw Error('Preview too large');
        if(!blob.type.startsWith('image/'))throw Error('Not an image');
        const image=await createImageBitmap(blob,{resizeWidth:384,resizeQuality:'medium'});
        try{entry.blob=await thumbnail(image);}finally{image.close();}
        if(this.entries.get(preview.id)===entry)this.onchange(this.blobs());
      }catch{/* The slate keeps its type glyph until a preview arrives or retries. */}
    }));
  }
}
export async function thumbnail(image){
  const {width,height}=PREVIEW_SIZE,canvas=new OffscreenCanvas(width,height),ctx=canvas.getContext('2d');
  // Cover-crop from the top: screenshots and video posters keep their heading.
  const scale=Math.max(width/image.width,height/image.height),w=image.width*scale,h=image.height*scale;
  ctx.drawImage(image,(width-w)/2,Math.min(0,(height-h)/2)*.35,w,h);
  return canvas.convertToBlob({type:'image/png'});
}
// A clearly synthetic stand-in for the client-local Workflow demo. It is
// generated here, never fetched, and reads as a placeholder at any scale.
export async function syntheticPreview(label='SYNTHETIC PREVIEW'){
  const {width,height}=PREVIEW_SIZE,canvas=new OffscreenCanvas(width,height),ctx=canvas.getContext('2d');
  ctx.fillStyle='#3a4a52';ctx.fillRect(0,0,width,height);ctx.strokeStyle='#5f7580';ctx.lineWidth=6;
  for(let x=-height;x<width+height;x+=26){ctx.beginPath();ctx.moveTo(x,height);ctx.lineTo(x+height,0);ctx.stroke();}
  ctx.fillStyle='#0f1c22cc';ctx.fillRect(0,height/2-18,width,36);
  ctx.fillStyle='#f0e6c8';ctx.font='700 15px sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(label,width/2,height/2);
  return canvas.convertToBlob({type:'image/png'});
}
