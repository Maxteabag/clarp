#!/usr/bin/env node
import {chromium} from '@playwright/test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const url=process.argv[2],out=process.argv[3]||'/var/tmp/fleet-stage-two-proof';if(!url)throw Error('Pass the private stage-two presentation URL');
await fs.mkdir(out,{recursive:true});const b=await chromium.launch();const errors=[];
try{
 for(const width of [390,1280]){
  const ctx=await b.newContext({viewport:{width,height:900},acceptDownloads:true}),p=await ctx.newPage();p.on('pageerror',e=>errors.push(e.message));
  await p.goto(url);await p.evaluate(()=>document.fonts.ready);
  assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await p.screenshot({path:`${out}/${width}-intro.png`});
  await p.locator('#concept').scrollIntoViewIfNeeded();
  for(let n=0;n<6;n++){await p.locator(`[data-step="${n}"]`).click();assert.equal(await p.locator('#demo').getAttribute('data-stage'),String(n));}
  await p.waitForTimeout(850);
  assert.equal(await p.locator('.preview').evaluate(el=>getComputedStyle(el).opacity),'1');
  await p.locator('#demo').screenshot({path:`${out}/${width}-concept.png`});
  await p.locator('#play').click();await p.waitForTimeout(2800);assert.equal(await p.locator('#demo').getAttribute('data-stage'),'1');await p.locator('#play').click();
  await p.locator('#delivery').scrollIntoViewIfNeeded();await p.screenshot({path:`${out}/${width}-tasks.png`});
  await p.locator('#notes').fill('More expressive recovery, less explanatory text.');await p.reload();assert.equal(await p.locator('#notes').inputValue(),'More expressive recovery, less explanatory text.');
  const download=p.waitForEvent('download');await p.locator('#export').click();const d=await download;await d.saveAs(`${out}/${width}-notes.txt`);assert((await fs.readFile(`${out}/${width}-notes.txt`,'utf8')).includes('More expressive recovery'));
  await p.evaluate(()=>navigator.serviceWorker.ready);await p.reload();await p.waitForFunction(()=>!!navigator.serviceWorker.controller);
  await ctx.setOffline(true);await p.reload();assert.equal(await p.locator('#notes').inputValue(),'More expressive recovery, less explanatory text.');await ctx.setOffline(false);
  await p.locator('#direction').scrollIntoViewIfNeeded();await p.screenshot({path:`${out}/${width}-notes.png`});await ctx.close();
 }
 const ctx=await b.newContext({viewport:{width:390,height:844}});await ctx.addInitScript(()=>{Storage.prototype.setItem=()=>{throw Error('Storage disabled');};});const p=await ctx.newPage();await p.goto(url);await p.locator('#notes').fill('Still exportable');assert((await p.locator('#save-status').textContent()).includes('Storage unavailable'));await ctx.close();
 assert.deepEqual(errors,[]);await fs.writeFile(`${out}/verification.json`,JSON.stringify({phone:true,desktop:true,conceptStages:6,playback:true,localDraftRestore:true,notesExport:true,offlineReopen:true,storageFailure:true,errors},null,2));console.log('Presentation: phone/desktop layout, six stages, playback, draft restore, export, offline and storage failure passed.');
}finally{await b.close();}
