// Explicitly synthetic and entirely client-local; never enters the Host stream.
// The sequence exercises one work object from declared intent through edits,
// a failed and repaired validation, delivery, a published outcome, a research
// report with sources and a verified handoff between the two roles.
export const DEMO_LENGTH_MS=52000;
export function flowDemo(start){
  const repo='demo:checkout',file='demo:source',test='demo:tests',created='demo:new',deleted='demo:obsolete',remote='demo:remote';
  const plan='dead0000beef0000:reload-lab:0badf00d',builder='demo-builder',reviewer='demo-reviewer';
  const entities=[{id:repo,label:'Workflow example',kind:'repository',path:'/example/project'},
    {id:'demo:directory',label:'src',kind:'directory',parent:repo,path:'/example/project/src'},
    ...[[file,'controller.py','Python source'],[test,'test_controller.py','Regression tests'],[created,'cache.py','New module'],[deleted,'legacy.py','Retired module']].map(([id,label,purpose])=>({id,label,purpose,kind:'file',parent:'demo:directory',path:'/example/project/src/'+label,extension:'.py'})),
    {id:'demo:service',label:'Preview server',kind:'service',unit:'preview.service',scope:'user',purpose:'Synthetic service'},
    {id:'github',label:'GitHub',kind:'platform'},
    {id:'github:example-owner',label:'Example owner',kind:'organization',parent:'github'},
    {id:remote,label:'project',kind:'remote-repository',parent:'github:example-owner',url:'https://github.com/example-owner/project'}];
  const at=seconds=>start+seconds*1000;
  const e=(id,seconds,action,target,duration,outcome='succeeded',agent=builder,extra={})=>({id:'demo:'+id,ts:at(seconds),finished_at:at(seconds+duration),outcome,
    agent:agent===builder?'Builder':'Reviewer',agent_id:agent,world_target:target,world_targets:[target],workspace_target:repo,action,verb:action,location_scope:'target',
    evidence:{path:entities.find(e=>e.id===target)?.path,raw:'Synthetic workflow example',...extra}});
  const events=[
    e(1,0,'read',file,4),e(2,1,'read',test,4,'succeeded',reviewer),
    e(3,5,'edit',file,4),e(4,10,'test',test,4,'failed',builder,{validation:'test',validation_exact:true,validation_scope:'single',validation_commands:['npm test']}),
    e(5,15,'edit',file,4),e(6,20,'test',test,4,'succeeded',builder,{validation:'test',validation_exact:true,validation_scope:'single',validation_commands:['npm test']}),
    e(7,24,'create',created,2),e(8,28,'delete',deleted,2),e(9,31,'commit',repo,2),
    {...e(10,34,'push',repo,4),remote_target:remote},
    e(11,26,'read','demo:directory',3,'succeeded',reviewer),
    e(12,47,'restart','demo:service',2,'succeeded',reviewer),
  ];
  const work={synthetic:true,available:true,
    contract:{intent:'Synthetic plan declared by the Builder role.',evidence:'Synthetic events attributed by role and time.',outcome:'Synthetic artifacts with a generated placeholder preview.',handoff:'Synthetic explicit handoff record; live messages that name a plan are references only.'},
    plans:[{id:plan,title:'Ship the reload lab',status:'completed',agent_id:builder,agent:'Builder',session:'demo-builder',created_at:at(0),updated_at:at(38.5),completed_at:at(38.5),item_total:3,
      items:[{id:'implement',title:'Implement the reload profile',status:'completed',position:0,started_at:at(0),completed_at:at(9)},
             {id:'verify',title:'Run the regression suite',status:'completed',position:1,started_at:at(9),completed_at:at(24)},
             {id:'deliver',title:'Record and publish the comparison',status:'completed',position:2,started_at:at(24),completed_at:at(38.5)}]}],
    artifacts:[
      {id:'demo:artifact-report',type:'research',title:'Reload timing survey',status:'ready',agent_id:reviewer,agent:'Reviewer',session:'demo-reviewer',created_at:at(30),source_count:5,
       sources:[1,2,3,4,5].map(i=>({title:'Source '+i,url:'https://example.invalid/'+i}))},
      {id:'demo:artifact-run-1',type:'workflow_run',title:'Checks',status:'failed',agent_id:builder,agent:'Builder',session:'demo-builder',created_at:at(37),completed_at:at(38),
       updated_at:at(38),run:{workflow_name:'Checks',repository:'example-owner/project',branch:'main',conclusion:'failure',github_status:'completed'},remote_target:remote},
      {id:'demo:artifact-video',type:'video',title:'Reload lab · comparison',status:'ready',agent_id:builder,agent:'Builder',session:'demo-builder',created_at:at(38.5),
       preview:{url:'demo:synthetic',mime:'image/png',width:192,height:144},media_url:'https://example.invalid/demo-comparison.mp4',media:{mime_type:'video/mp4',file_name:'comparison.mp4',duration_ms:12000}},
      {id:'demo:artifact-run-2',type:'workflow_run',title:'Checks',status:'completed',agent_id:builder,agent:'Builder',session:'demo-builder',created_at:at(40),completed_at:at(41.5),
       updated_at:at(41.5),run:{workflow_name:'Checks',repository:'example-owner/project',branch:'main',conclusion:'success',github_status:'completed'},remote_target:remote},
    ],
    // The demo's message carries a synthetic explicit handoff record so the
    // transfer visual can be exercised; live messages only ever reference plans.
    messages:[{id:'demo:message-1',from_agent_id:builder,from:'Builder',to_agent_id:reviewer,to:'Reviewer',session:'demo-reviewer',ts:at(43),plan_ids:[plan],link:'transfer',
      excerpt:'Handing you the reload lab plan for the service restart and a final look. (synthetic explicit handoff record)'}]};
  return {host:'Workflow demo',entities,relations:[{from:repo,to:remote,kind:'remote',label:'origin'}],coverage_keys:[],events,work};
}
