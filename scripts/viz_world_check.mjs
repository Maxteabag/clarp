#!/usr/bin/env node
// Exercise actual arbitrary source, camera interaction, playback and fault containment.
import {chromium} from '@playwright/test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const url=process.argv[2]||'http://127.0.0.1:7700/viz';
const out=process.argv[3]||'/var/tmp/fleet-world-check';await fs.mkdir(out,{recursive:true});
const browser=await chromium.launch();
const context=await browser.newContext({viewport:{width:1600,height:1000},recordVideo:{dir:out,size:{width:1600,height:1000}}});
const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
try{
 await page.goto(url);await page.waitForFunction(()=>window.fleetWorldSnapshot?.().frames>10);
 await page.locator('#fit').click();await page.waitForTimeout(300);
 let s=await page.evaluate(()=>window.fleetWorldSnapshot());
 assert(!s.failure,s.failure);assert(s.meta.territories>1);assert(s.meta.files>0);assert(s.meta.agents.length>0);
 assert(s.scene.relations.some(r=>r.kind==='remote'));
 await page.screenshot({path:out+'/world.png'});
 // Trace real history at 120x. This is labeled replay, never fake live activity.
 await page.locator('#t').evaluate(el=>{el.value=650;el.dispatchEvent(new Event('input'));});
 const before=await page.evaluate(()=>window.fleetWorldSnapshot());
 await page.locator('#play').click();await page.waitForTimeout(8000);
 const after=await page.evaluate(()=>window.fleetWorldSnapshot());
 assert(after.playhead>before.playhead);assert(after.frames>before.frames+30);
 assert(JSON.stringify(after.meta.agents)!==JSON.stringify(before.meta.agents));
 await page.screenshot({path:out+'/replay.png'});
 // Inspect a real file at a readable zoom.
 const file=s.meta.hits.find(h=>h.path&&/\.(py|js|md)$/.test(h.path));
 if(file){
  await page.mouse.move(800,500);await page.mouse.down();await page.mouse.move(950,600);await page.mouse.up();
  const pan=await page.evaluate(()=>window.fleetWorldSnapshot().camera);assert(pan.x!==s.camera.x);
 }
 assert.deepEqual(errors,[]);
 await fs.writeFile(out+'/verification.json',JSON.stringify({program:s.program,frames:after.frames,territories:s.meta.territories,
   files:s.meta.files,agents:s.meta.agents.length,realEvents:s.scene.events.length,sourceRevision:s.revision,playback:true,errors},null,2));
}finally{await context.close();await browser.close();}
// Fault tests in a separate recording-free browser preserve the user's demo.
const faultBrowser=await chromium.launch();const p=await faultBrowser.newPage({viewport:{width:1200,height:800}});
try{
 await p.goto(url);await p.waitForFunction(()=>window.fleetWorldSnapshot?.().frames>5);
 await p.evaluate(async()=>{
  const {SourceSandbox}=await import('/static/lib/viz-source-sandbox.js');
  window.sandboxChecks={};
  const run=(key,source)=>new Promise(resolve=>{
   const box=new SourceSandbox({entry:'x.js',files:{'x.js':source}},(bitmap,meta)=>{bitmap.close();box.destroy();window.sandboxChecks[key]=meta;resolve();},
    err=>{window.sandboxChecks[key]=err;resolve();});
   const t=setInterval(()=>{if(box.stopped){clearInterval(t);return;}box.draw({scene:{},time:0,playhead:0,width:100,height:100,camera:{x:0,y:0,k:1}});},20);
  });
  await run('throws','module.exports.render=()=>{throw Error("generated failure")};');
  await run('loops','module.exports.render=()=>{while(true){}};');
  await run('isolation','module.exports.render=()=>{let blocked=false;try{indexedDB.open("host-storage")}catch(e){blocked=true;}return {storageBlocked:blocked,origin:location.origin};};');
 });
 const checks=await p.evaluate(()=>window.sandboxChecks);assert.match(checks.throws,/generated failure/);assert.match(checks.loops,/deadline/);
 assert(checks.isolation.storageBlocked);assert.equal(checks.isolation.origin,'null');
 const f=await p.evaluate(()=>window.fleetWorldSnapshot().frames);await p.waitForTimeout(200);
 assert((await p.evaluate(()=>window.fleetWorldSnapshot().frames))>f);
 await fs.writeFile(out+'/sandbox.json',JSON.stringify(checks,null,2));
 console.log(JSON.stringify(checks));
}finally{await faultBrowser.close();}
