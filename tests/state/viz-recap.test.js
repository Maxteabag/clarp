import {it,expect} from 'vitest';
import {periodRange,safeUrl,loadReview,markSeen,isUnread} from '../../static/lib/viz-recap-model.js';
it('uses local calendar boundaries for yesterday',()=>{
 const now=+new Date(2026,8,11,8,30),range=periodRange('yesterday',now,0);
 expect(range).toEqual({since:+new Date(2026,8,10),until:+new Date(2026,8,11)});
 expect(periodRange('since',now,now-60000).since).toBe(now-60000);
 expect(periodRange('since',now,now+60000).since).toBe(range.since);
 expect(periodRange('since',now,1).since).toBe(now-90*86400000);
});
it('records reviewed versions without marking later revisions seen',()=>{
 const a={id:'a',created_at:10,updated_at:20};const state=loadReview({getItem:()=>null});
 expect(isUnread(a,state)).toBe(true);
 const next=markSeen(state,[a],100);expect(isUnread(a,next)).toBe(false);
 expect(isUnread({...a,updated_at:21},next)).toBe(true);
 expect(state.seen).toEqual({});expect(markSeen(next,[],90).lastReview).toBe(100);
});
it('handles denied or corrupted storage and bounds reviewed history',()=>{
 expect(loadReview({getItem(){throw Error('denied');}})).toEqual({lastReview:0,seen:{}});
 expect(loadReview({getItem:()=>'{broken'}).seen).toEqual({});
 expect(Object.keys(markSeen({lastReview:0,seen:{}},Array.from({length:600},(_,i)=>({id:String(i),updated_at:i}))).seen)).toHaveLength(500);
 expect(isUnread({id:'toString',created_at:10},{seen:{}})).toBe(true);
});
it('allows recorded HTTPS and media links, never script or filesystem URLs',()=>{
 for(const value of ['javascript:alert(1)','file:///etc/passwd','//evil/x','/media/../secret','https://user:pw@site.test','https:\n//site.test'])expect(safeUrl(value)).toBeNull();
 expect(safeUrl('/media/asset_123')).toBe('/media/asset_123');
 expect(safeUrl('https://example.com/report')).toBe('https://example.com/report');
});
