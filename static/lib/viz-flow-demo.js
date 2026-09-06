// Explicitly synthetic and entirely client-local; never enters the Host stream.
export function flowDemo(start){
  const repo='demo:checkout',file='demo:source',test='demo:tests',created='demo:new',deleted='demo:obsolete',remote='demo:remote';
  const entities=[{id:repo,label:'Workflow example',kind:'repository',path:'/example/project'},
    {id:'demo:directory',label:'src',kind:'directory',parent:repo,path:'/example/project/src'},
    ...[[file,'controller.py','Python source'],[test,'test_controller.py','Regression tests'],[created,'cache.py','New module'],[deleted,'legacy.py','Retired module']].map(([id,label,purpose])=>({id,label,purpose,kind:'file',parent:'demo:directory',path:'/example/project/src/'+label,extension:'.py'})),
    {id:'github',label:'GitHub',kind:'platform'},
    {id:'github:example-owner',label:'Example owner',kind:'organization',parent:'github'},
    {id:remote,label:'project',kind:'remote-repository',parent:'github:example-owner',url:'https://github.com/example-owner/project'}];
  const e=(id,seconds,action,target,duration,outcome='succeeded',agent='demo-builder')=>({id:'demo:'+id,ts:start+seconds*1000,finished_at:start+(seconds+duration)*1000,outcome,
    agent:agent==='demo-builder'?'Builder':'Reviewer',agent_id:agent,world_target:target,world_targets:[target],workspace_target:repo,action,verb:action,location_scope:'target',evidence:{path:entities.find(e=>e.id===target)?.path,raw:'Synthetic workflow example'}});
  return {host:'Workflow demo',entities,relations:[{from:repo,to:remote,kind:'remote',label:'origin'}],coverage_keys:[],events:[
    e(1,0,'read',file,4),e(2,1,'read',test,4,'succeeded','demo-reviewer'),
    e(3,5,'edit',file,4),e(4,10,'test',test,4,'failed','demo-reviewer'),
    e(5,15,'edit',file,4),e(6,20,'test',test,4,'succeeded','demo-reviewer'),
    e(7,24,'create',created,2),e(8,28,'delete',deleted,2),e(9,31,'commit',repo,2),
    {...e(10,34,'push',repo,4),remote_target:remote},
  ]};
}
