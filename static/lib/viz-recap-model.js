const DAY=86400000;
export function periodRange(mode,now,lastReview){
 const today=new Date(now);today.setHours(0,0,0,0);
 const yesterday=new Date(today);yesterday.setDate(yesterday.getDate()-1);
 if(mode==='yesterday')return {since:+yesterday,until:+today};
 if(mode==='week')return {since:now-7*DAY,until:now};
 return {since:Math.max(now-90*DAY,Number.isFinite(lastReview)&&lastReview>0&&lastReview<now?lastReview:+yesterday),until:now};
}
export function safeUrl(value){
 if(typeof value!=='string'||/[\u0000-\u0020]/.test(value))return null;
 if(/^\/media\/[\w-]+$/.test(value))return value;
 try{const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password?u.href:null;}catch{return null;}
}
export function loadReview(storage){
 try{const v=JSON.parse(storage.getItem('clarp.fleet.recap')||'{}');return {lastReview:Number.isFinite(v.lastReview)?v.lastReview:0,seen:v.seen&&typeof v.seen==='object'&&!Array.isArray(v.seen)?v.seen:{}};}catch{return {lastReview:0,seen:{}};}
}
export function artifactVersion(a){return Math.max(a.created_at||0,a.updated_at||0);}
export function isUnread(a,state){return !(Object.hasOwn(state.seen,a.id)&&state.seen[a.id]>=artifactVersion(a));}
export function markSeen(state,artifacts,until){
 const seen={...state.seen};for(const a of artifacts)Object.defineProperty(seen,a.id,{value:artifactVersion(a),enumerable:true,configurable:true,writable:true});
 return {lastReview:Math.max(state.lastReview,until||0),seen:Object.fromEntries(Object.entries(seen).sort((a,b)=>b[1]-a[1]).slice(0,500))};
}
