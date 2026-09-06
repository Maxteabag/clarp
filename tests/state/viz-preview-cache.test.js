import {it,expect,vi,afterEach} from 'vitest';
import {PreviewCache} from '../../static/lib/viz-preview-cache.js';
afterEach(()=>vi.unstubAllGlobals());
it('rejects external and escaping media paths before fetching',async()=>{
 const fetcher=vi.fn();const cache=new PreviewCache(()=>{},fetcher);
 await cache.update(['https://other/a','/media/../private','/media/x?redirect=1','/media/'].map((url,i)=>({id:String(i),url})));
 expect(fetcher).not.toHaveBeenCalled();
});
it('refuses oversized previews before decoding and cancels an oversized stream',async()=>{
 const decode=vi.fn();vi.stubGlobal('createImageBitmap',decode);
 const large=new Response(null,{headers:{'content-length':String(9*1024*1024)}});
 const cancel=vi.fn();const reader={read:vi.fn().mockResolvedValue({done:false,value:new Uint8Array(9*1024*1024)}),cancel,releaseLock:vi.fn()};
 const stream={ok:true,headers:new Headers({'content-type':'image/png'}),body:{getReader:()=>reader}};
 const fetcher=vi.fn().mockResolvedValueOnce(large).mockResolvedValueOnce(stream);
 const cache=new PreviewCache(()=>{},fetcher);await cache.update([{id:'a',url:'/media/large'},{id:'b',url:'/media/stream'}]);
 expect(decode).not.toHaveBeenCalled();expect(cancel).toHaveBeenCalled();expect(cache.blobs()).toEqual({});
});
it('resizes and reuses a bounded preview outside frames',async()=>{
 const close=vi.fn(),decode=vi.fn(async()=>({width:384,height:288,close}));vi.stubGlobal('createImageBitmap',decode);
 vi.stubGlobal('OffscreenCanvas',class {getContext(){return {drawImage(){}};}async convertToBlob(){return new Blob(['thumb'],{type:'image/png'});}});
 const fetcher=vi.fn(async()=>new Response(new Blob(['image'],{type:'image/png'})));const cache=new PreviewCache(()=>{},fetcher);
 await cache.update([{id:'a',url:'/media/asset_a'}]);await cache.update([{id:'a',url:'/media/asset_a'}]);
 expect(fetcher).toHaveBeenCalledTimes(1);expect(decode.mock.calls[0][1].resizeWidth).toBe(384);expect(close).toHaveBeenCalled();expect(Object.keys(cache.blobs())).toEqual(['a']);
});
