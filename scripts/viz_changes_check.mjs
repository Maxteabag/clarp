#!/usr/bin/env node
import {chromium} from '@playwright/test';import assert from 'node:assert/strict';import fs from 'node:fs/promises';
const url=process.argv[2],out=process.argv[3]||'/var/tmp/fleet-changes-proof';if(!url)throw Error('Pass explainer URL');await fs.mkdir(out,{recursive:true});const browser=await chromium.launch();const errors=[];
try{for(const width of [390,1280]){
 const context=await browser.newContext({viewport:{width,height:900},reducedMotion:'reduce'}),page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 await page.goto(url);await page.waitForFunction(()=>document.querySelector('#comparison').naturalWidth>0);
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:`${out}/${width}-intro.png`});
 const after=await page.locator('#comparison').getAttribute('src');await page.locator('#before').click();assert.notEqual(await page.locator('#comparison').getAttribute('src'),after);await page.locator('#now').click();assert.equal(await page.locator('#comparison').getAttribute('src'),after);
 await page.locator('#comparison').click();assert(await page.locator('#zoom').isVisible());await page.locator('#close').click();assert(await page.locator('#zoom').isHidden());
 await page.locator('#lantern').scrollIntoViewIfNeeded();
 const pixels=new Set();for(const state of ['intent','making','checking','interrupted','repaired','published','unresolved']){
  await page.locator(`button[data-state="${state}"]`).click();await page.waitForTimeout(150);assert.equal(await page.locator('#lantern-canvas').getAttribute('data-state'),state);pixels.add(await page.locator('#lantern-canvas').evaluate(c=>c.toDataURL()));
  if(width===390||state==='published')await page.locator('.guide').screenshot({path:`${out}/${width}-${state}.png`});
 }assert.equal(pixels.size,7,'states have visually distinct renderings');
 await page.locator('#truth').scrollIntoViewIfNeeded();await page.screenshot({path:`${out}/${width}-truth.png`});
 await page.locator('summary').last().click();assert(await page.getByText('The integrated version passed', {exact:false}).isVisible());
 const download=page.waitForEvent('download');await page.getByText('Save HTML ↓').click();await (await download).saveAs(`${out}/explainer-${width}.html`);const html=await fs.readFile(`${out}/explainer-${width}.html`,'utf8');assert(html.includes('data:image/jpeg;base64,')&&html.includes('const factories='));
 await context.close();
}assert.deepEqual(errors,[]);console.log('Explainer verified: phone/desktop, before/after, image enlargement, seven distinct renderer states, details and self-contained download.');await fs.writeFile(`${out}/verification.json`,JSON.stringify({widths:[390,1280],states:7,comparison:true,enlargement:true,download:true,errors},null,2));}finally{await browser.close();}
