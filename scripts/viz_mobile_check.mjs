#!/usr/bin/env node
import {chromium} from '@playwright/test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const url=process.argv[2]||'http://localhost:7701/viz',out=process.argv[3]||'/var/tmp/fleet-mobile-proof';
await fs.mkdir(out,{recursive:true});
const browser=await chromium.launch();const errors=[],results=[];
try{
 for(const viewport of [{width:390,height:844},{width:320,height:568},{width:844,height:390}]){
  const context=await browser.newContext({viewport,isMobile:true,hasTouch:true,deviceScaleFactor:2});
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  await page.goto(url+'?view=flow');await page.waitForFunction(()=>window.fleetWorldSnapshot?.().frames>5);
  for(const view of ['flow','world','cabinets']){
   await page.locator('#view-'+view).click();await page.waitForFunction(v=>window.fleetWorldSnapshot().view===v&&!window.fleetWorldSnapshot().failure,view);
   await page.waitForTimeout(700);await page.locator('#fit').click();
   await page.waitForTimeout(250);
   const s=await page.evaluate(()=>window.fleetWorldSnapshot());
   assert(s.frames>0&&!s.failure);assert(s.meta.hits.length>0);
   assert(await page.locator('#c').isVisible());
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   const bounds=await page.evaluate(()=>({top:document.querySelector('#hud').getBoundingClientRect().bottom,bottom:document.querySelector('#bar').getBoundingClientRect().top}));
   assert(bounds.bottom-bounds.top>100,'canvas has usable space');
   const c=s.camera,b=s.meta.bounds;
   assert(b.x*c.k+c.x>=-1&& (b.x+b.w)*c.k+c.x<=viewport.width+1);
   await page.screenshot({path:`${out}/${viewport.width}-${view}.png`});
   results.push({viewport,view,frames:s.frames,hits:s.meta.hits.length});
  }
  await page.locator('#view-flow').click();await page.waitForTimeout(500);await page.locator('#fit').click();
  const cdp=await context.newCDPSession(page);
  const box=await page.evaluate(()=>({top:document.querySelector('#hud').getBoundingClientRect().bottom,bottom:document.querySelector('#bar').getBoundingClientRect().top}));
  const x=viewport.width/2,y=(box.top+box.bottom)/2;
  const touch=(id,x,y)=>({id,x,y});
  const start=await page.evaluate(()=>window.fleetWorldSnapshot().camera);
  await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[touch(1,x,y)]});
  await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[touch(1,x+30,y+20)]});
  await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  const pan=await page.evaluate(()=>window.fleetWorldSnapshot().camera);assert(Math.abs(pan.x-start.x-30)<2);assert(Math.abs(pan.y-start.y-20)<2);
  assert(await page.locator('#inspector').isHidden());
  await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[touch(1,x-25,y),touch(2,x+25,y)]});
  await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[touch(1,x-75,y),touch(2,x+75,y)]});
  await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  const zoom=await page.evaluate(()=>window.fleetWorldSnapshot().camera);assert(zoom.k>pan.k*2.8);assert(zoom.k<pan.k*3.2);
  assert(Math.abs((x-zoom.x)/zoom.k-(x-pan.x)/pan.k)<2);assert(await page.locator('#inspector').isHidden());
  await page.screenshot({path:`${out}/${viewport.width}-zoom.png`});
  await page.locator('#fit').click();await page.waitForTimeout(200);
  const target=await page.evaluate(()=>{
   const s=window.fleetWorldSnapshot(),c=s.camera,h=s.meta.hits.find(h=>h.purpose==='Workspace'||h.purpose.startsWith('Working copy'));
   return {x:(h.x+h.w*.4)*c.k+c.x,y:(h.y+35)*c.k+c.y};
  });
  await page.touchscreen.tap(target.x,target.y);assert(await page.locator('#inspector').isVisible());
  const inspector=await page.locator('#inspector').boundingBox();assert(inspector.x>=0&&inspector.x+inspector.width<=viewport.width);
  await page.screenshot({path:`${out}/${viewport.width}-details.png`});await page.locator('#close-inspector').click();
  for(const id of ['flow-demo','flow-labels']){
   await page.locator('#'+id).scrollIntoViewIfNeeded();const b=await page.locator('#'+id).boundingBox();assert(b.height>=44);assert(b.x>=0&&b.x+b.width<=viewport.width);
  }
  await context.close();
 }
 assert.deepEqual(errors,[]);await fs.writeFile(out+'/verification.json',JSON.stringify({results,touchPan:true,pinchAnchor:true,tapInspection:true,errors},null,2));
 console.log('Mobile portrait, small phone, landscape, all views, real touch pan/pinch/tap passed.');
}finally{await browser.close();}
