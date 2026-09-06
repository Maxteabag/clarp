#!/usr/bin/env node
// Real-evidence proof for work objects. Opens Flow paused at a recorded instant
// and verifies that a plan-backed slate reached its outcome with a host-loaded
// preview, that a failed validation earlier in the window cracked it and a later
// success sealed it, and that the inspector states the evidence contract.
//   node scripts/viz_work_check.mjs URL OUT_DIR UNTIL_EPOCH_MS [WINDOW_SECONDS] [PLAN_ID_FRAGMENT]
import {chromium} from '@playwright/test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const [url='http://localhost:7701/viz',out='/var/tmp/fleet-work-proof',untilArg='',windowArg='1800',fragment='']=process.argv.slice(2);
await fs.mkdir(out,{recursive:true});
const until=Number(untilArg)||0,windowSeconds=Number(windowArg)||1800;
const browser=await chromium.launch();const context=await browser.newContext({viewport:{width:1600,height:1000}});
const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
const snapshot=()=>page.evaluate(()=>window.fleetWorldSnapshot());
try{
 await page.goto(url+'?view=flow&window='+windowSeconds+(until?'&until='+until:''));
 await page.waitForFunction(()=>window.fleetWorldSnapshot?.().frames>5,null,{timeout:60000});await page.waitForTimeout(2500);
 let s=await snapshot();assert(!s.failure);assert(s.meta.workEvidence.available&&!s.meta.workEvidence.synthetic);
 if(until)assert(!s.live&&s.playhead===until,'the map opens paused at the requested instant');
 await page.screenshot({path:out+'/work-overview.png'});
 const candidates=s.meta.workObjects.filter(w=>!w.id.startsWith('artifact:')&&w.stage==='outcome'&&(!fragment||w.id.includes(fragment)));
 const target=candidates.find(w=>w.preview)||candidates[0];
 const result={until,windowSeconds,workObjects:s.meta.workObjects,relations:s.meta.relations,previews:s.previews,waits:s.meta.waits,posts:s.meta.posts,errors};
 const recordedWaits=new Set([...(s.scene.work?.jobs||[]).map(j=>'job:'+j.id),...(s.scene.work?.decisions||[]).map(d=>'decision:'+d.id)]);
 for(const wt of s.meta.waits)assert(recordedWaits.has(wt.id),'every drawn wait is a recorded job or decision');
 if(!target){
  result.verdict='no plan-backed work object reached an outcome in this window; nothing was invented';
  await fs.writeFile(out+'/verification.json',JSON.stringify(result,null,2));console.log(result.verdict);process.exit(0);
 }
 assert(s.meta.workspaces.some(w=>w.id===target.workspace));
 if(target.preview)assert(s.meta.loadedImages.includes(target.artifact),'the preview image is the recorded artifact asset');
 const sx=target.x*s.camera.k+s.camera.x,sy=target.y*s.camera.k+s.camera.y;
 await page.mouse.move(sx,sy);for(let i=0;i<11;i++){await page.mouse.wheel(0,-100);await page.waitForTimeout(30);}await page.waitForTimeout(500);
 await page.screenshot({path:out+'/work-outcome-zoom.png'});
 s=await snapshot();await page.mouse.click(target.x*s.camera.k+s.camera.x,target.y*s.camera.k+s.camera.y);await page.waitForTimeout(400);
 const detail=await page.locator('#node-detail').textContent();
 for(const word of ['Intent:','Evidence:','Outcome:','attributed by'])assert(detail.includes(word),'inspector states '+word);
 if(target.preview)assert(detail.includes('preview from recorded media'));
 await page.screenshot({path:out+'/work-inspect.png'});
 result.inspector=detail;result.target=target;
 // Scrub back through the same window: the slate must exist earlier at a lower
 // stage, and if its validation recovered, a failed state must precede it.
 const plan=s.scene.work.plans.find(p=>p.id===target.id);
 const stages=[];
 for(const frac of [.15,.35,.55,.75,.95]){
  const t=plan.created_at+(Math.min(until||s.playhead,plan.completed_at||s.playhead)-plan.created_at)*frac;
  await page.evaluate(({t})=>{const st=window.fleetWorldSnapshot();const el=document.getElementById('t');el.value=1000*(t-st.timeline.since)/(st.timeline.until-st.timeline.since);el.dispatchEvent(new Event('input'));},{t});
  await page.waitForTimeout(450);const st=await snapshot();const w=st.meta.workObjects.find(w=>w.id===target.id);
  const detailsNow=await page.locator('#node-detail').textContent();
  if(w?.stage!=='outcome'){assert(!detailsNow.includes('preview from recorded media'),'inspector must follow the replay playhead');assert(await page.locator('#node-link').isHidden(),'future artifact link must be hidden');}
  stages.push({t,stage:w?.stage,validation:w?.validation,workspaceValidation:st.meta.workspaces.find(x=>x.id===w?.workspace)?.validation});
  const broken=v=>v==='failed'||v==='interrupted';
  if(broken(w?.validation)&&!stages.some((x,i)=>i<stages.length-1&&broken(x.validation))){
   // Layout can shift while scrubbing; re-center on the slate so the crack is in frame at normal scale.
   await page.locator('#fit').click();await page.waitForTimeout(250);const f=await snapshot();
   const fx=w.x*f.camera.k+f.camera.x,fy=w.y*f.camera.k+f.camera.y;await page.mouse.move(fx,fy);for(let i=0;i<11;i++){await page.mouse.wheel(0,-100);await page.waitForTimeout(30);}
   await page.waitForTimeout(500);await page.screenshot({path:out+'/work-interrupted.png'});}
 }
 result.stages=stages;
 assert(stages.every(x=>x.stage),'the object persists across the whole window');
 assert(stages[0].stage!=='outcome'||stages[0].validation!==target.validation,'earlier frames show an earlier state');
 if(target.validation==='recovered')assert(stages.some(x=>['failed','interrupted'].includes(x.validation)),'recovery follows a visible failure or interruption');
 assert.deepEqual(errors,[]);
 result.verdict='real work object followed from intent through evidence to a recorded outcome';
 await fs.writeFile(out+'/verification.json',JSON.stringify(result,null,2));
 console.log(result.verdict,JSON.stringify(stages));
}finally{await context.close();await browser.close();}
