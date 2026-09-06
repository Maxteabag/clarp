// Browser-local observation memory, partitioned by Host. Structural edges and
// repeated touches are distinct; neither invents causal dependencies/handoffs.
export class FlowMemory {
  constructor(storage=globalThis.localStorage){this.storage=storage;this.host=null;this.state=null;}
  update(scene){
    const host=scene.host||'unknown';
    if(host!==this.host){
      this.host=host;this.state=null;
      try{this.state=JSON.parse(this.storage.getItem('clarp.flow.memory:'+host)||'null');}catch{}
      if(!this.state||this.state.version!==1||!Array.isArray(this.state.seen)||!['entities','relations','touches'].every(k=>this.state[k]&&typeof this.state[k]==='object'&&!Array.isArray(this.state[k])))this.state={version:1,entities:{},relations:{},touches:{},seen:[]};
    }
    const state=this.state,now=Date.now();
    const existing=new Map(scene.entities.map(e=>[e.id,e]));
    for(const e of scene.entities)if(['repository','remote-repository','organization','platform'].includes(e.kind))state.entities[e.id]={...e,lastObserved:now};
    for(const r of scene.relations||[]){const key=r.from+'\n'+r.to+'\n'+r.kind;state.relations[key]={...r,lastObserved:now};}
    const seen=new Set(state.seen);
    for(const e of scene.events){
      if(seen.has(String(e.id)))continue;seen.add(String(e.id));
      for(const target of e.world_targets||[e.world_target]){
        if(existing.get(target)?.kind!=='file')continue;
        const key=e.agent_id+'\n'+target;const old=state.touches[key];state.touches[key]={agent:e.agent_id,target,count:(old?.count||0)+1,lastObserved:e.ts};
      }
    }
    state.seen=[...seen].slice(-8000);
    const trim=(map,limit)=>Object.fromEntries(Object.entries(map).sort(([,a],[,b])=>b.lastObserved-a.lastObserved).slice(0,limit));
    state.entities=trim(state.entities,400);state.relations=trim(state.relations,400);state.touches=trim(state.touches,1000);
    try{this.storage.setItem('clarp.flow.memory:'+host,JSON.stringify(state));}catch{}
    const entities=[...scene.entities];for(const e of Object.values(state.entities))if(!existing.has(e.id))entities.push({...e,historical:true});
    const relations=Object.values(state.relations).filter(r=>state.entities[r.from]&&state.entities[r.to]);
    return {...scene,entities,relations,flowMemory:{relations,touches:Object.values(state.touches)}};
  }
}
