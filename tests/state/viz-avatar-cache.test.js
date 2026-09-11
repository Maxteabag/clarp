import {it,expect,vi,afterEach} from 'vitest';
import {AvatarCache} from '../../static/lib/viz-avatar-cache.js';
afterEach(()=>vi.unstubAllGlobals());
function decoding(){
 vi.stubGlobal('createImageBitmap',async()=>({width:200,height:100,close(){}}));
 vi.stubGlobal('OffscreenCanvas',class {getContext(){return {drawImage(){}};}async convertToBlob(){return new Blob(['thumbnail'],{type:'image/png'});}});
}
it('uses exact agent portraits, keeps same-name agents separate, and caches them',async()=>{
 decoding();const fetcher=vi.fn(async()=>({ok:true,blob:async()=>new Blob(['photo'],{type:'image/jpeg'})}));
 const cache=new AvatarCache(()=>{},fetcher);
 const actors=[{id:'a',label:'Nadia',avatar_url:'/avatars/a?v=1'},{id:'b',label:'Nadia',avatar_url:'/avatars/b?v=1'}];
 await cache.update(actors);await cache.update(actors);
 expect(fetcher.mock.calls.map(c=>c[0])).toEqual(['/avatars/a?v=1','/avatars/b?v=1']);
 expect(Object.keys(cache.blobs())).toEqual(['a','b']);
 await cache.update([{...actors[0],avatar_url:'/avatars/a?v=2'}]);
 expect(fetcher).toHaveBeenCalledTimes(3);expect(Object.keys(cache.blobs())).toEqual(['a']);
});
it('uses the shared bundled portrait resolver when no uploaded image exists',async()=>{
 decoding();const fetcher=vi.fn(async()=>({ok:true,blob:async()=>new Blob(['photo'],{type:'image/png'})}));
 await new AvatarCache(()=>{},fetcher).update([{id:'a',label:'Nadia',avatar_url:''}]);
 expect(fetcher.mock.calls[0][0]).toBe('/static/avatars/nadia.png');
});
it('leaves missing or failed images to initials without repeating failed requests each poll',async()=>{
 const fetcher=vi.fn(async()=>({ok:false}));const cache=new AvatarCache(()=>{},fetcher);
 const actors=[{id:'a',label:'Custom Agent',avatar_url:''},{id:'b',label:'Other',avatar_url:'/avatars/b?v=1'}];
 await cache.update(actors);await cache.update(actors);
 expect(fetcher).toHaveBeenCalledTimes(1);expect(cache.blobs()).toEqual({});
});
it('never fetches a model-supplied external avatar',async()=>{
 const fetcher=vi.fn();await new AvatarCache(()=>{},fetcher).update([{id:'a',label:'Custom',avatar_url:'https://untrusted.test/portrait'}]);
 expect(fetcher).not.toHaveBeenCalled();
});
