const CACHE='clarp-stage-two-presentation-v1';
self.addEventListener('install',event=>{event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(['./','./index.html'])).then(()=>self.skipWaiting()));});
self.addEventListener('activate',event=>{event.waitUntil(self.clients.claim());});
self.addEventListener('fetch',event=>{
 const url=new URL(event.request.url),root=new URL('./',self.location).pathname;
 if(event.request.method!=='GET'||url.origin!==self.location.origin||![root,root+'index.html'].includes(url.pathname))return;
 event.respondWith(fetch(event.request).catch(()=>caches.open(CACHE).then(cache=>cache.match(event.request,{ignoreSearch:true}))));
});
