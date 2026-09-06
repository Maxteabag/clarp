#!/usr/bin/env node
import {chromium} from '@playwright/test';
import assert from 'node:assert/strict';
const url=process.argv[2]||'http://localhost:7701/viz';
const browser=await chromium.launch();
const page=await browser.newPage({viewport:{width:1600,height:1000}});
try{
 await page.goto(url);await page.waitForFunction(()=>window.fleetWorldSnapshot?.().frames>10);
 const before=await page.evaluate(()=>window.fleetWorldSnapshot().frames);
 const cdp=await page.context().newCDPSession(page);
 await cdp.send('Emulation.setCPUThrottlingRate',{rate:10});await page.waitForTimeout(4000);
 const after=await page.evaluate(()=>window.fleetWorldSnapshot());
 assert(after.frames>before+10);assert.equal(after.failure,'');
 await cdp.send('Emulation.setCPUThrottlingRate',{rate:1});
 // A cheap renderer's response delayed 400 ms must not be declared runaway.
 const timing=await page.evaluate(async()=>{
  const {SourceSandbox}=await import('/static/lib/viz-source-sandbox.js');
  return await new Promise((resolve,reject)=>{
   const program={entry:'x.js',files:{'x.js':"const send=self.postMessage.bind(self);self.postMessage=(data,ports)=>setTimeout(()=>send(data,ports),400);module.exports.render=({ctx})=>{ctx.fillRect(0,0,10,10);return {};};"}};
   const box=new SourceSandbox(program,(bitmap,meta,timing)=>{bitmap.close();box.destroy();clearInterval(timer);resolve(timing);},err=>{clearInterval(timer);reject(Error(err));});
   const timer=setInterval(()=>box.draw({scene:{},width:100,height:100,time:0,playhead:0,camera:{x:0,y:0,k:1}}),20);
  });
 });
 assert(timing.deliveryMs>=350);assert(timing.executionMs<150);
 // Inject a renderer fault through the real sandbox interface after a good
 // frame, including the baseline fallback. Canvas pixels must remain visible.
 const sourceBefore=await page.screenshot();
 await page.evaluate(async()=>{
  const {SourceSandbox}=await import('/static/lib/viz-source-sandbox.js');
  window.savedFleetDraw=SourceSandbox.prototype.draw;
  SourceSandbox.prototype.draw=function(){this.fail('Test generated failure','source');};
 });
 await page.waitForFunction(()=>window.fleetWorldSnapshot().failure==='Test generated failure');
 assert(await page.locator('#retry-render').isVisible());
 const retained=await page.evaluate(()=>{
  const c=document.getElementById('c');const data=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
  let visible=0;for(let i=3;i<data.length;i+=4)if(data[i])visible++;return visible;
 });
 assert(retained>100000,'last good canvas must not be erased');
 await page.evaluate(async()=>{
  const {SourceSandbox}=await import('/static/lib/viz-source-sandbox.js');SourceSandbox.prototype.draw=window.savedFleetDraw;
 });
 const paused=await page.evaluate(()=>window.fleetWorldSnapshot().frames);
 await page.locator('#retry-render').click();
 await page.waitForFunction(n=>window.fleetWorldSnapshot().frames>n+3,paused);
 const snap=await page.evaluate(()=>window.fleetWorldSnapshot());assert(!snap.failure);
 await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));});
 const hiddenFrame=await page.evaluate(()=>window.fleetWorldSnapshot().frames);
 await page.waitForTimeout(300);
 assert.equal(await page.evaluate(()=>window.fleetWorldSnapshot().frames),hiddenFrame);
 await page.evaluate(()=>{delete document.hidden;document.dispatchEvent(new Event('visibilitychange'));});
 await page.waitForFunction(n=>window.fleetWorldSnapshot().frames>n+3,hiddenFrame);


 console.log(JSON.stringify({frames:after.frames,throttledTimings:after.frameTimings,delayedCheapFrame:timing,retainedCanvasBytes:sourceBefore.length,resumeRendering:true,hiddenTabResume:true}));
}finally{await browser.close();}
