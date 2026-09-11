#!/usr/bin/env node
import {chromium} from '@playwright/test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const url=process.argv[2],out=process.argv[3];if(!url||!out)throw Error('Usage: node scripts/viz_heatmap_check.mjs URL OUTPUT_DIR');
await fs.mkdir(out,{recursive:true});const browser=await chromium.launch(),results=[];
try{
 for(const viewport of [{width:1440,height:900},{width:390,height:844}]){
  const context=await browser.newContext({viewport,deviceScaleFactor:viewport.width<500?2:1});
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  const target=new URL(url);target.searchParams.set('view','flow');await page.goto(target.href);
  await page.waitForFunction(()=>window.fleetWorldSnapshot?.().frames>5);
  if(await page.locator('#activity-feed').count())await page.locator('#activity-feed').evaluate(e=>e.open=false);
  const snap=()=>page.evaluate(()=>window.fleetWorldSnapshot());
  assert(!(await snap()).heat.enabled);
  await page.locator('#flow-demo').click();
  await page.locator('#t').evaluate(el=>{el.value=500;el.dispatchEvent(new Event('input'));});
  await page.waitForTimeout(600);const before=await snap();
  const plain=await page.locator('#c').screenshot();
  await page.locator('#heatmap').click();
  await page.waitForFunction(()=>window.fleetWorldSnapshot().heat.spots.length>0);
  const hot=await snap();assert.deepEqual(hot.camera,before.camera);
  const painted=await page.locator('#c').screenshot();assert(!plain.equals(painted),'heat changes actual canvas pixels');
  await page.screenshot({path:`${out}/${viewport.width}-heatmap.png`});
  await page.mouse.move(viewport.width/2,viewport.height/2);await page.mouse.wheel(0,100);
  await page.waitForTimeout(400);assert((await snap()).camera.k<hot.camera.k);
  assert((await snap()).heat.spots.length>0);
  await page.locator('#heatmap').click();await page.waitForTimeout(200);assert(!(await snap()).heat.enabled);
  assert(!(await page.locator('#heat-legend').isVisible()));
  await page.locator('#heatmap').click();await page.locator('#follow-activity').click();await page.waitForTimeout(400);
  assert((await snap()).heat.enabled&&(await snap()).follow.enabled);
  await page.locator('#catch-up').click();await page.locator('#recap-close').click();
  assert((await snap()).heat.enabled,'recap preserves heatmap mode');
  await page.reload();await page.waitForFunction(()=>window.fleetWorldSnapshot?.().frames>5);
  assert((await snap()).heat.enabled,'heat preference survives reload');
  assert.deepEqual(errors,[]);results.push({viewport,located:hot.heat.located,spots:hot.heat.spots.length,pixelsChanged:true,errors});await context.close();
 }
 await fs.writeFile(out+'/verification.json',JSON.stringify({evidence:'Labeled synthetic workflow, real browser rendering',results},null,2));
 console.log('Heatmap pixels, pan/zoom, follow/recap coexistence, desktop/phone and saved preference passed.');
}finally{await browser.close();}
