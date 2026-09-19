#!/usr/bin/env node
import {chromium} from '@playwright/test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const url=process.argv[2],out=process.argv[3];
if(!url||!out)throw Error('Usage: node scripts/viz_follow_check.mjs URL OUTPUT');
await fs.mkdir(out,{recursive:true});
const browser=await chromium.launch(),results=[];
try{
 for(const viewport of [{width:1440,height:900},{width:390,height:844}]){
  const context=await browser.newContext({viewport,recordVideo:{dir:out,size:viewport},isMobile:viewport.width<500,hasTouch:true});
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  const target=new URL(url);target.searchParams.set('view','flow');
  await page.goto(target.href);await page.waitForFunction(()=>window.fleetWorldSnapshot?.().frames>5);
  const snap=()=>page.evaluate(()=>window.fleetWorldSnapshot());
  assert.equal((await snap()).follow.enabled,false);
  await page.locator('#flow-demo').click();
  await page.waitForFunction(()=>window.fleetWorldSnapshot().demoEnabled);
  await page.locator('#follow-activity').click();
  await page.waitForFunction(()=>window.fleetWorldSnapshot().follow.phase==='focus');
  await page.waitForTimeout(3000);const close=await snap();
  await page.screenshot({path:`${out}/${viewport.width}-close.png`});
  await page.waitForFunction(()=>window.fleetWorldSnapshot().follow.phase==='overview');
  await page.waitForTimeout(3000);const wide=await snap();
  assert(wide.camera.k<close.camera.k,'overview zooms out from activity');
  await page.screenshot({path:`${out}/${viewport.width}-overview.png`});
  await page.mouse.move(viewport.width/2,viewport.height/2);await page.mouse.wheel(0,-100);
  assert((await snap()).follow.paused);
  const paused=await snap();await page.waitForTimeout(600);
  assert.deepEqual((await snap()).camera,paused.camera,'manual camera stays put');
  await page.locator('#follow-activity').click();assert(!(await snap()).follow.paused);
  await page.locator('#t').evaluate(el=>{el.value=200;el.dispatchEvent(new Event('input'));});
  assert((await snap()).follow.paused,'scrubbing pauses camera');
  await page.locator('#follow-activity').click();
  await page.mouse.move(viewport.width/2,viewport.height/2);await page.mouse.down();await page.mouse.move(viewport.width/2+20,viewport.height/2+20);await page.mouse.up();
  assert((await snap()).follow.paused,'dragging pauses camera');
  await page.reload();await page.waitForFunction(()=>window.fleetWorldSnapshot?.().frames>5);
  assert((await snap()).follow.enabled,'opt-in survives reload');
  if(await page.locator('#catch-up').count()){
   await page.locator('#catch-up').click();await page.waitForTimeout(200);
   const reading=await snap();await page.waitForTimeout(600);
   assert.deepEqual((await snap()).camera,reading.camera,'recap holds the camera still');
   await page.locator('#recap-close').click();
   assert((await snap()).follow.enabled,'returning from recap retains follow mode');
  }
  await page.locator('#follow-activity').click();assert(!(await snap()).follow.enabled);
  assert.deepEqual(errors,[]);
  results.push({viewport,close:close.camera,overview:wide.camera,manualPause:true,persistence:true,errors});
  await context.close();
 }
 await fs.writeFile(out+'/verification.json',JSON.stringify({evidence:'Synthetic workflow demo; real browser camera movement',results},null,2));
 console.log('Desktop and phone: focus, overview, manual pause, resume, scrubbing and preference persistence passed.');
}finally{await browser.close();}
