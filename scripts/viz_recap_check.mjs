#!/usr/bin/env node
import {chromium} from '@playwright/test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const url=process.argv[2],out=process.argv[3];if(!url||!out)throw Error('Usage: node scripts/viz_recap_check.mjs URL OUTPUT_DIR');
await fs.mkdir(out,{recursive:true});const browser=await chromium.launch();const results=[];
try{
 for(const viewport of [{width:1440,height:1000},{width:390,height:844}]){
  const context=await browser.newContext({viewport}),page=await context.newPage(),errors=[],posts=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()==='POST')posts.push(r.url());});
  await page.goto(url);await page.locator('#catch-up').click();
  await page.locator('#recap-period').selectOption('week');
  await page.waitForFunction(()=>!document.querySelector('#recap-done').disabled);
  const cards=await page.locator('.recap-artifact').count();assert(cards>0,'representative real history must contain artifacts');
  assert(await page.locator('.recap-agent').count()>0);
  assert(await page.locator('#recap').evaluate(e=>e.scrollWidth<=e.clientWidth+1),'no horizontal overflow');
  await page.screenshot({path:`${out}/${viewport.width}-recap.png`});
  const readable=page.getByRole('button',{name:'Read artifact'}).first();await readable.click();
  await page.waitForFunction(()=>document.querySelector('#recap-reader h2'));
  await page.screenshot({path:`${out}/${viewport.width}-reader.png`});
  const frame=page.locator('#recap-reader iframe');
  if(await frame.count())assert.equal(await frame.getAttribute('sandbox'),'');
  await page.getByRole('button',{name:'Close artifact',exact:true}).click();
  const htmlCard=page.locator('.recap-artifact').filter({hasText:'html form'}).first();
  if(await htmlCard.count()){
   await htmlCard.getByRole('button',{name:'Read artifact'}).click();
   await page.waitForFunction(()=>document.querySelector('#recap-reader iframe'));
   assert.equal(await page.locator('#recap-reader iframe').getAttribute('sandbox'),'');
   await page.waitForTimeout(300);await page.screenshot({path:`${out}/${viewport.width}-html-preview.png`});
   await page.getByRole('button',{name:'Close artifact',exact:true}).click();
  }
  const media=page.locator('.recap-artifact a[href^="/media/"]').first();
  if(await media.count()){
   const response=await page.request.get(new URL(await media.getAttribute('href'),url).href,{headers:{Range:'bytes=0-31'}});
   assert.equal(response.status(),206);assert.equal((await response.body()).length,32);
  }
  await page.locator('#recap-done').click();
  assert((await page.locator('#recap-summary').textContent()).includes('0 unseen'));
  await page.locator('#recap-close').click();assert(!(await page.locator('#recap').isVisible()));
  await page.reload();await page.locator('#catch-up').click();await page.locator('#recap-period').selectOption('week');
  await page.waitForFunction(()=>!document.querySelector('#recap-done').disabled);
  assert((await page.locator('#recap-summary').textContent()).includes('0 unseen'),'review versions survive reload');
  assert.deepEqual(errors,[]);assert.deepEqual(posts,[]);
  results.push({viewport,realArtifacts:cards,persistence:true,errors,posts});await context.close();
 }
 // Fault handling is exercised with labeled injected responses, separately
 // from the real-history screenshots above.
 const context=await browser.newContext(),page=await context.newPage();
 await page.addInitScript(()=>{Object.defineProperty(window,'localStorage',{get(){throw Error('denied');}});});
 await page.route('**/viz/recap?*',r=>r.fulfill({status:500,body:'failure'}));
 const deepLink=new URL(url);deepLink.searchParams.set('recap','1');await page.goto(deepLink.href);
 await page.waitForFunction(()=>document.querySelector('#recap-status').textContent.includes('Could not'));
 assert(await page.locator('#recap-done').isDisabled());
 await page.unroute('**/viz/recap?*');await page.locator('#recap-period').selectOption('week');
 await page.waitForFunction(()=>!document.querySelector('#recap-done').disabled);await page.locator('#recap-done').click();
 assert((await page.locator('#recap-storage').textContent()).includes('only for this visit'));
 await page.route('**/viz/recap?*',r=>r.fulfill({json:{since:1,until:2,generated_at:3,groups:[],truncated:['artifacts'],counts:{agents:0,artifacts:0,attention:0},coverage:'fixture'}}));
 await page.locator('#recap-refresh').click();await page.waitForFunction(()=>document.querySelector('#recap-status').textContent.includes('omitted'));
 assert(await page.locator('#recap-done').isDisabled());
 await page.unroute('**/viz/recap?*');
 await page.route('**/viz/recap?*',r=>r.fulfill({json:{since:1,until:2,generated_at:3,groups:[],truncated:[],counts:{agents:0,artifacts:0,attention:0},coverage:'fixture'}}));
 await page.locator('#recap-refresh').click();await page.waitForFunction(()=>document.querySelector('#recap-list').textContent.includes('Nothing recorded'));
 await context.close();
 await fs.writeFile(out+'/verification.json',JSON.stringify({realHistory:results,injectedFaults:['HTTP failure','storage denied','truncation','empty period']},null,2));
 console.log('Real-history recap, artifact reader, desktop/mobile layout, persistence, no POSTs, empty and failure states passed.');
}finally{await browser.close();}
