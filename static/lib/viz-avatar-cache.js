import {resolveAvatarUrl} from './avatar.js';

// Fetch selected app portraits outside rendering. Only decoded local image
// blobs cross into the sandbox; authored source never gets network access.
export class AvatarCache {
  constructor(onchange,fetcher=fetch){this.entries=new Map();this.onchange=onchange;this.fetcher=(...args)=>fetcher(...args);}
  blobs(){return Object.fromEntries([...this.entries].filter(([,e])=>e.blob).map(([id,e])=>[id,e.blob]));}
  async update(actors){
    const ids=new Set(actors.map(a=>a.id));let removed=false;
    for(const id of this.entries.keys())if(!ids.has(id)){this.entries.delete(id);removed=true;}
    if(removed)this.onchange(this.blobs());
    await Promise.all(actors.map(async actor=>{
      const url=resolveAvatarUrl({[actor.id]:{avatar_url:actor.avatar_url}},actor.label,actor.id);
      const old=this.entries.get(actor.id);
      if(old?.url===url && (old.blob || Date.now()<old.retryAt))return;
      if(!url){if(old){this.entries.delete(actor.id);this.onchange(this.blobs());}return;}
      // The backend and bundled resolver own this route, not model output.
      if(!url.startsWith('/avatars/')&&!url.startsWith('/static/avatars/')&&!url.startsWith('/viz/owner-avatar/'))return;
      const entry={url,blob:null,retryAt:Date.now()+60000};this.entries.set(actor.id,entry);
      try{
        const response=await this.fetcher(url,{credentials:'same-origin'});
        if(!response.ok)throw Error('Avatar unavailable');
        const blob=await response.blob();
        if(!blob.type.startsWith('image/'))throw Error('Not an image');
        // Resize once to a small transferable portrait. The original remains
        // in Clarp; the scene only needs a crisp thumbnail.
        const image=await createImageBitmap(blob);
        try{
          const canvas=new OffscreenCanvas(128,128),ctx=canvas.getContext('2d');
          const size=Math.min(image.width,image.height);
          ctx.drawImage(image,(image.width-size)/2,(image.height-size)/2,size,size,0,0,128,128);
          entry.blob=await canvas.convertToBlob({type:'image/png'});
        }finally{image.close();}
        if(this.entries.get(actor.id)===entry)this.onchange(this.blobs());
      }catch{/* Initials stay visible while absent or failed portraits retry. */}
    }));
  }
}
