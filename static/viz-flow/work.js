// Work objects: one persistent identity from declared intent through attributed
// evidence to a recorded outcome. Attribution is by agent identity and time
// overlap and is labeled as such; counts never become progress.
const {hash}=require('./model-util.js');
const journey=require('./journey.js');
const TAIL=120000; // publishing usually lands within two minutes of completion
const isValidation=e=>['test','build','lint'].includes(e.action)||!!e.evidence?.validation;
const isChange=e=>['edit','write','create','delete'].includes(e.action)&&e.location_scope==='target';
// Looking around is not yet evidence of work: the ticket stays hollow until
// something is changed, run, built, committed or otherwise acted upon.
const isMaterial=e=>!['read','search','media','status','is-active','network','unknown'].includes(e.action);
const finishedAt=e=>e.finished_at??e.ts;
exports.outcomeOf=(e,t)=>e.finished_at!=null&&t>=e.finished_at?(e.outcome||'unknown'):(e.finished_at!=null||e.outcome==='running'?'running':'unknown');

// Validation runs in time order become one legible state. A single command's
// failure is that check failing; a failed && chain or ; script is an interruption
// whose failing component is unknown. Only a later exact success of the same
// checks (every failed or interrupted command covered) resolves it; an unrelated
// passing check never erases a failure.
const commandsOf=r=>{const c=r.evidence?.validation_commands;return Array.isArray(c)&&c.length?c:[String(r.evidence?.raw||r.action)];};
exports.validationState=(runs,t)=>{
 const seen=runs.filter(r=>r.ts<=t).sort((a,b)=>a.ts-b.ts);
 if(!seen.length)return {state:'none',runs:0};
 let open=null,recoveredAt=null,okAt=null,running=null;
 for(const r of seen){
  const outcome=exports.outcomeOf(r,t),scope=r.evidence?.validation_scope||(r.evidence?.validation_exact===false?'script':'single'),cmds=commandsOf(r);
  if(outcome==='running'){running=r;continue;}
  if(outcome==='failed'){open={kind:open?.kind==='failed'||scope==='single'?'failed':'interrupted',at:open?.at??finishedAt(r),scope,pending:new Set([...(open?.pending||[]),...cmds])};recoveredAt=null;}
  else if(outcome==='succeeded'&&scope!=='script'){
   if(open){for(const c of cmds)open.pending.delete(c);if(!open.pending.size){recoveredAt=finishedAt(r);open=null;}}
   else okAt=finishedAt(r);
  }
 }
 const last=seen.at(-1);
 return {runs:seen.length,state:running?'running':open?open.kind:recoveredAt!=null?'recovered':okAt!=null?'ok':'unknown',
  failedAt:open?.at??null,recoveredAt,okAt,unresolved:open?[...open.pending]:[],scope:open?.scope||last.evidence?.validation_scope||'single',
  since:running?running.ts:(open?.at??recoveredAt??okAt??finishedAt(last)),kind:last.evidence?.validation||last.action,exact:last.evidence?.validation_exact!==false};
};

// A remote run's conclusion exists only once its completion is evidenced at
// the playhead; before that it is running, whatever the database says now.
const runAt=(a,t)=>{
 const terminal=['completed','failed','cancelled'].includes(a.status),done=a.completed_at??(terminal?a.updated_at:null);
 const finished=done!=null&&done<=t;
 return {id:a.id,remote:a.remote_target,conclusion:finished?(a.run?.conclusion||a.status):'',status:finished?a.status:'active',created_at:a.created_at,completed_at:finished?done:null,agent_id:a.agent_id,branch:a.run?.branch||'',workflow:a.run?.workflow_name||a.title};
};
exports.assemble=(scene,t,history,groupFor,actorWorkspace)=>{
 const work=scene.work||{};const plans=(work.plans||[]).filter(p=>p.created_at<=t);
 const artifacts=(work.artifacts||[]).filter(a=>a.created_at<=t),messages=(work.messages||[]).filter(m=>m.ts<=t);
 const objects=[];const claimed=new Set();
 for(const plan of plans){
  const end=plan.completed_at!=null&&plan.completed_at<=t?plan.completed_at:null;
  const windowEnd=end!=null?end+TAIL:t;
  const events=(history.get(plan.agent_id)||[]).filter(e=>e.ts>=plan.created_at&&e.ts<=windowEnd);
  for(const e of events)claimed.add(e.id);
  const changes=events.filter(isChange),runs=events.filter(isValidation);
  const files=[...new Set(changes.flatMap(e=>e.world_targets||[e.world_target]))];
  const tally=new Map();
  for(const e of events){const g=groupFor(e);tally.set(g,(tally.get(g)||0)+(e.location_scope==='target'?2:1));}
  const workspace=[...tally.entries()].sort((a,b)=>b[1]-a[1])[0]?.[0]||actorWorkspace(plan.agent_id)||'unlocated';
  const own=artifacts.filter(a=>a.agent_id===plan.agent_id&&a.session===plan.session&&a.created_at>=plan.created_at&&a.created_at<=windowEnd);
  const outputs=own.filter(a=>a.type!=='workflow_run'),runsRemote=own.filter(a=>a.type==='workflow_run').map(a=>runAt(a,t));
  const primary=[...outputs].reverse().find(a=>a.preview)||outputs.at(-1)||null;
  // A message naming this plan is a reference; a transfer needs an explicit handoff record.
  const handoffs=messages.filter(m=>m.plan_ids?.includes(plan.id)).map(m=>({id:m.id,from:m.from_agent_id,fromName:m.from,to:m.to_agent_id,toName:m.to,ts:m.ts,excerpt:m.excerpt,link:m.link==='transfer'?'transfer':'reference'}));
  const items=plan.items||[],done=items.filter(i=>i.status==='completed'&&(i.completed_at==null||i.completed_at<=t)).length;
  const stage=primary?'outcome':(events.some(isMaterial)?'evidence':'intent');
  const finished=end!=null||['completed','cancelled','failed'].includes(plan.status)&&plan.updated_at<=t;
  const lastActivity=Math.max(plan.created_at,...events.map(finishedAt),...own.map(a=>a.created_at));
  objects.push({id:plan.id,title:plan.title,agent:plan.agent,agent_id:plan.agent_id,status:finished?(plan.status==='active'?'completed':plan.status):'active',
   created_at:plan.created_at,completed_at:end,workspace,stage,seed:hash(plan.id),
   intent:{declared:items.length,total:plan.item_total||items.length,done,current:items.find(i=>i.status==='in_progress')?.title||null},
   evidence:{running:events.some(e=>exports.outcomeOf(e,t)==='running'),events:events.length,changes:changes.length,files:files.slice(0,6),validation:exports.validationState(runs,t),basis:'same agent, inside the plan window'},
   outcome:primary?{id:primary.id,type:primary.type,title:primary.title,created_at:primary.created_at,preview:!!primary.preview,link:primary.run?.run_url||primary.media_url||null,sources:primary.sources||null,source_count:primary.source_count||0,media:primary.media||null,count:outputs.length}:null,
   remoteRuns:runsRemote,
   handoffs,age:Math.max(0,t-lastActivity),finished});
 }
 // Artifacts nobody's plan claims still happened: outcomes without declared
 // intent. They become outcome-only slates with an empty intent cell rather
 // than being hidden or given an invented plan.
 const orphanArtifacts=artifacts.filter(a=>a.type!=='workflow_run'&&!objects.some(o=>o.agent_id===a.agent_id&&a.created_at>=o.created_at&&a.created_at<=(o.completed_at!=null?o.completed_at+TAIL:t)));
 for(const a of orphanArtifacts){
  const workspace=actorWorkspace(a.agent_id)||'unlocated';
  objects.push({id:'artifact:'+a.id,kind:'artifact',title:a.title,agent:a.agent,agent_id:a.agent_id,status:'published',created_at:a.created_at,completed_at:a.created_at,workspace,stage:'outcome',seed:hash(a.id),
   intent:{declared:0,total:0,done:0,current:null,none:true},evidence:{events:0,changes:0,files:[],validation:{state:'none',runs:0},basis:'no plan window; artifact only'},
   outcome:{id:a.id,type:a.type,title:a.title,created_at:a.created_at,preview:!!a.preview,link:a.media_url||null,sources:a.sources||null,source_count:a.source_count||0,media:a.media||null,count:1},
   remoteRuns:[],handoffs:[],age:Math.max(0,t-a.created_at),finished:true});
 }
 // Waits: recorded jobs and pending decisions at the playhead. A wait that
 // started inside a plan's window by the same agent is attributed to that work.
 const waits=[...(work.jobs||[]).map(j=>journey.waitAt(j,t)),...(work.decisions||[]).map(d=>journey.decisionAt(d,t))].filter(Boolean);
 for(const wt of waits){
  const owner=objects.find(o=>!o.kind&&o.agent_id===wt.agent_id&&wt.since>=o.created_at&&wt.since<=(o.completed_at!=null?o.completed_at+TAIL:t));
  if(owner){wt.work=owner.id;wt.attributed=owner.title;(owner.waits=owner.waits||[]).push(wt);}
 }
 const remoteRuns=artifacts.filter(a=>a.type==='workflow_run'&&a.remote_target).map(a=>runAt(a,t));
 const threads=messages.map(m=>({id:m.id,from:m.from_agent_id,fromName:m.from,to:m.to_agent_id,toName:m.to,ts:m.ts,plan:m.plan_ids?.[0]||null,link:m.plan_ids?.length?(m.link==='transfer'?'transfer':'reference'):null,excerpt:m.excerpt}));
 return {objects,claimed,orphanArtifacts,remoteRuns,threads,waits,contract:work.contract||null,synthetic:!!work.synthetic,available:work.available!==false};
};

// Per-workspace validation from every recorded run there, attributed or not.
exports.workspaceValidation=(events,t,groupFor)=>{
 const byWorkspace=new Map();
 for(const e of events){if(!isValidation(e))continue;const g=groupFor(e);const list=byWorkspace.get(g)||[];list.push(e);byWorkspace.set(g,list);}
 return new Map([...byWorkspace].map(([id,runs])=>[id,exports.validationState(runs,t)]));
};

// A project's character comes from what it actually produced, never a random skin.
exports.character=(objects,orphanArtifacts,regionIds)=>{
 const score={media:0,docs:0,code:0};
 const bump=type=>{if(['video','image','image_gallery','audio'].includes(type))score.media++;else if(['document','research','file','html_form'].includes(type))score.docs++;else if(['workflow_run','release','deployment','code_change'].includes(type))score.code++;};
 for(const o of objects)if(regionIds.has(o.workspace)){if(o.outcome)bump(o.outcome.type);for(const r of o.remoteRuns)bump('workflow_run');}
 const best=Object.entries(score).sort((a,b)=>b[1]-a[1])[0];
 return best&&best[1]>0?best[0]:null;
};
