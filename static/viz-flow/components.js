// Component membership comes from recorded paths, never an agent's current cwd.
exports.relative=(root,path)=>{
 if(typeof root!=='string'||typeof path!=='string')return null;
 const prefix=root.replace(/\/$/,'')+'/';if(!path.startsWith(prefix))return null;
 const relative=path.slice(prefix.length);return relative.split('/').some(p=>p==='..')?null:relative;
};
exports.describe=(region,files)=>{
 const groups=new Map(),root=[];
 for(const file of files){const relative=exports.relative(region.path,file.path);if(relative===null)continue;
  const parts=relative.split('/');if(parts.length===1){root.push(file);continue;}
  const key=parts[0];if(!groups.has(key))groups.set(key,[]);groups.get(key).push(file);
 }
 return {groups:[...groups].sort(([a],[b])=>a.localeCompare(b)),root};
};
exports.layout=(region,description)=>{
 const areas=[],files=[],rows=Math.ceil(description.groups.length/2),top=region.y-region.ry+55;
 description.groups.forEach(([name,members],i)=>{
  const area={id:region.id+':component:'+name,workspace:region.id,name,path:region.path+'/'+name,x:region.x+(i%2?145:-145),y:top+110+Math.floor(i/2)*245,w:265,h:225,total:members.length};
  areas.push(area);
  members.sort((a,b)=>a.path.localeCompare(b.path)).slice(0,4).forEach((f,j)=>files.push({...f,component:area.id,workspace:region.id,x:area.x+(j%2?65:-35),y:area.y-35+Math.floor(j/2)*65}));
 });
 const rootY=top+rows*245+55;
 description.root.sort((a,b)=>a.path.localeCompare(b.path)).slice(0,4).forEach((f,i)=>files.push({...f,component:null,workspace:region.id,x:region.x-220+i*85,y:rootY}));
 return {areas,files,rootY,rootCount:description.root.length};
};
