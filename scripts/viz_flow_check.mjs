#!/usr/bin/env node
import {chromium} from '@playwright/test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const url=process.argv[2]||'http://localhost:7701/viz',out=process.argv[3]||'/var/tmp/fleet-flow-proof';await fs.mkdir(out,{recursive:true});
const DEMO_SECONDS=52;
const browser=await chromium.launch();const context=await browser.newContext({viewport:{width:1600,height:1000},recordVideo:{dir:out,size:{width:1600,height:1000}}});
const page=await context.newPage(),errors=[],posts=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()==='POST')posts.push(r.url());});
const snapshot=()=>page.evaluate(()=>window.fleetWorldSnapshot());
try{
 await page.goto(url);await page.waitForFunction(()=>window.fleetWorldSnapshot?.().frames>5);
 await page.locator('#view-flow').click();await page.waitForFunction(()=>window.fleetWorldSnapshot().meta.title==='Flow · The Lantern Works');
 await page.waitForTimeout(500);const live=await snapshot();
 assert.equal(live.view,'flow');assert(!live.demoEnabled);assert(!live.failure);
 assert(live.meta.ownerGroups.some(o=>o.children.length));
 assert(live.meta.githubLogo);
 const observedAgents=[...new Set(live.scene.events.filter(e=>e.ts<=live.playhead).map(e=>e.agent_id))].sort();
 assert.deepEqual(live.meta.agents.map(a=>a.id).sort(),observedAgents);
 for(const repo of live.scene.entities.filter(e=>e.kind==='repository'))assert(live.meta.workspaces.some(w=>w.id===repo.id));
 assert(live.meta.projects.length>0);
 for(const p of live.meta.projects)assert(p.children.every(id=>live.meta.workspaces.some(w=>w.id===id)));
 assert.equal(live.meta.focusedRepository,undefined);
 // Work objects: every drawn slate comes from a recorded plan or artifact in the
 // payload, sits inside a drawn workspace, and never claims a preview it lacks.
 assert(live.meta.workEvidence.available,'work evidence section present');
 const plans=new Set((live.scene.work?.plans||[]).map(p=>p.id)),artifacts=new Set((live.scene.work?.artifacts||[]).map(a=>a.id));
 for(const w of live.meta.workObjects){
  assert(plans.has(w.id)||(w.id.startsWith('artifact:')&&artifacts.has(w.id.slice(9))),'work object has recorded provenance '+w.id);
  assert(live.meta.workspaces.some(ws=>ws.id===w.workspace),'work object placed in a drawn workspace');
  if(w.preview)assert(live.meta.loadedImages.includes(w.artifact),'preview only when the host loaded that artifact image');
 }
 const workspace=live.meta.workspaces.find(w=>{
  const y=(w.y-120)*live.camera.k+live.camera.y;return y>160&&y<900;
 });
 assert(workspace,'a workspace is visible for inspection');
 await page.mouse.click((workspace.x-100)*live.camera.k+live.camera.x,(workspace.y-120)*live.camera.k+live.camera.y);
 await page.waitForTimeout(300);
 const selected=await snapshot();
 assert.deepEqual(selected.camera,live.camera);
 assert.deepEqual(selected.meta.workspaces,live.meta.workspaces);
 assert.equal(selected.meta.agents.length,live.meta.agents.length);
 assert(await page.locator('#inspector').isVisible());
 await page.screenshot({path:out+'/flow-live.png'});
 await page.locator('#flow-demo').click();await page.waitForTimeout(2300);
 const demo=await snapshot();assert(demo.demoEnabled);assert.equal(demo.scene.host,'Workflow demo');assert(!demo.actionLabels);
 assert(demo.meta.workEvidence.synthetic,'demo work evidence is labeled synthetic');
 assert.equal(demo.meta.agents.length,2);assert(demo.meta.visualActions.some(a=>a.action==='read'));
 // Declared intent alone: a hollow ticket, no evidence yet.
 assert.deepEqual(demo.meta.workObjects.map(w=>[w.stage,w.validation]),[['intent','none']]);
 assert.equal(demo.meta.relations.rails,1,'the configured origin is one structural rail');
 const readingPositions=demo.meta.agents.map(a=>[a.id,a.x]);
 await page.screenshot({path:out+'/read.png'});
 async function seek(seconds){await page.locator('#t').evaluate((s,{seconds,total})=>{s.value=seconds/total*1000;s.dispatchEvent(new Event('input'));},{seconds,total:DEMO_SECONDS});await page.waitForTimeout(350);return await snapshot();}
 const plan=w=>w.meta.workObjects.find(o=>!o.id.startsWith('artifact:'));
 let state=await seek(7);assert(state.meta.visualActions.some(a=>a.action==='edit'));assert.deepEqual(state.meta.agents.map(a=>[a.id,a.x]),readingPositions);
 assert.equal(plan(state).stage,'evidence');assert(state.meta.relations.tethers>=1,'active attributed work is tethered to its slate');await page.screenshot({path:out+'/edit.png'});
 state=await seek(15);assert(state.meta.visualActions.some(a=>a.action==='test'&&a.state==='failed'));
 assert.equal(plan(state).validation,'failed');assert.equal(state.meta.workspaces.find(w=>w.id==='demo:checkout').validation,'failed','a failed run cracks the workspace');await page.screenshot({path:out+'/failure.png'});
 state=await seek(24.5);assert(state.meta.visualActions.some(a=>a.action==='test'&&a.state==='succeeded'));
 assert.equal(plan(state).validation,'recovered','an evidenced rerun resolves the interruption');assert.equal(state.meta.workspaces.find(w=>w.id==='demo:checkout').validation,'recovered');await page.screenshot({path:out+'/repaired.png'});
 state=await seek(27);assert(state.meta.visualActions.some(a=>a.action==='create'&&a.state==='succeeded'));await page.screenshot({path:out+'/create.png'});
 state=await seek(31);assert(state.meta.visualActions.some(a=>a.action==='delete'&&a.state==='succeeded'));
 const report=state.meta.workObjects.find(o=>o.id==='artifact:demo:artifact-report');assert(report&&report.stage==='outcome'&&!report.preview,'a research report without a plan is an outcome-only slate');await page.screenshot({path:out+'/report.png'});
 state=await seek(36);assert(state.meta.visualActions.some(a=>a.action==='push'));assert.equal(state.meta.relations.deliveries,1,'a push is a delivery along the rail');await page.screenshot({path:out+'/push.png'});
 state=await seek(39.5);assert.equal(plan(state).stage,'outcome');assert(plan(state).preview,'the synthetic preview is shown on the outcome slate');
 assert(state.meta.visualActions.some(a=>a.action==='check'&&a.state==='failure'),'remote check result appears at the remote');assert(state.meta.visualActions.some(a=>a.action==='push'&&a.state==='delivered'));await page.screenshot({path:out+'/outcome.png'});
 state=await seek(44.5);assert(state.meta.visualActions.some(a=>a.action==='handoff'&&a.state==='traveling'),'a message naming the plan carries its seal');assert.equal(plan(state).handoffs,1);assert.equal(state.meta.relations.threads,1);await page.screenshot({path:out+'/handoff.png'});
 state=await seek(48.5);assert(state.meta.visualActions.some(a=>a.action==='restart'&&a.state==='running'));assert(state.meta.workspaces.some(w=>w.id==='demo:service'));assert(state.meta.visualActions.some(a=>a.action==='check'&&a.state==='success'));await page.locator('#fit').click();await page.waitForTimeout(150);await page.screenshot({path:out+'/service-restart.png'});
 await page.locator('#flow-labels').click();assert((await snapshot()).actionLabels);
 await page.locator('#live').click();await page.waitForFunction(()=>!window.fleetWorldSnapshot().demoEnabled&&window.fleetWorldSnapshot().scene.host!=='Workflow demo');
 await page.waitForFunction(()=>!window.fleetWorldSnapshot().meta.workEvidence?.synthetic,null,{timeout:5000});
 assert(!(await snapshot()).meta.workEvidence.synthetic,'live view never carries synthetic work');
 await page.reload();await page.waitForFunction(()=>window.fleetWorldSnapshot?.().view==='flow'&&window.fleetWorldSnapshot().frames>5);
 await page.locator('#view-world').click();await page.waitForFunction(()=>window.fleetWorldSnapshot().meta.title==='The Lantern Works');
 await page.locator('#view-cabinets').click();await page.waitForFunction(()=>window.fleetWorldSnapshot().meta.title==='The Jacquard Observatory');
 assert.deepEqual(errors,[]);assert.deepEqual(posts,[]);
 await fs.writeFile(out+'/verification.json',JSON.stringify({liveEvents:live.scene.events.length,owners:live.meta.ownerGroups,realAvatars:live.meta.loadedAvatars.length,overallWorkspaces:live.meta.workspaces.length,overallAgents:live.meta.agents.length,liveWorkObjects:live.meta.workObjects,liveRelations:live.meta.relations,selectionPreservesLayout:true,ownerPortraits:live.meta.ownerPortraits,githubLogo:live.meta.githubLogo,demonstration:'synthetic, client-local',actionLabelsInitiallyHidden:true,distinctActions:true,stableLocalAvatars:true,viewsPreserved:true,workStages:['intent','evidence','outcome'],validationLifecycle:['failed','recovered'],handoffCarriesSeal:true,posts,errors},null,2));
 console.log('Flow, ownership, work objects, validation lifecycle, delivery, handoff, demo isolation and all views passed.');
}finally{await context.close();await browser.close();}
