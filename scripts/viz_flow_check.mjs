#!/usr/bin/env node
import {chromium} from '@playwright/test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const url=process.argv[2]||'http://localhost:7701/viz',out=process.argv[3]||'/var/tmp/fleet-flow-proof';await fs.mkdir(out,{recursive:true});
const browser=await chromium.launch();const context=await browser.newContext({viewport:{width:1600,height:1000},recordVideo:{dir:out,size:{width:1600,height:1000}}});
const page=await context.newPage(),errors=[],posts=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()==='POST')posts.push(r.url());});
try{
 await page.goto(url);await page.waitForFunction(()=>window.fleetWorldSnapshot?.().frames>5);
 await page.locator('#view-flow').click();await page.waitForFunction(()=>window.fleetWorldSnapshot().meta.title==='Flow · The Lantern Works');
 await page.waitForTimeout(500);const live=await page.evaluate(()=>window.fleetWorldSnapshot());
 assert.equal(live.view,'flow');assert(!live.demoEnabled);assert(!live.failure);
 assert(live.meta.ownerGroups.some(o=>o.children.length));
 await page.screenshot({path:out+'/flow-live.png'});
 await page.locator('#flow-demo').click();await page.waitForTimeout(2300);
 const demo=await page.evaluate(()=>window.fleetWorldSnapshot());assert(demo.demoEnabled);assert.equal(demo.scene.host,'Workflow demo');assert(!demo.actionLabels);
 assert.equal(demo.meta.agents.length,2);assert(demo.meta.visualActions.some(a=>a.action==='read'));
 const readingPositions=demo.meta.agents.map(a=>[a.id,a.x]);
 await page.screenshot({path:out+'/read.png'});
 async function seek(seconds){await page.locator('#t').evaluate((s,seconds)=>{s.value=seconds/41*1000;s.dispatchEvent(new Event('input'));},seconds);await page.waitForTimeout(350);return await page.evaluate(()=>window.fleetWorldSnapshot());}
 let state=await seek(7);assert(state.meta.visualActions.some(a=>a.action==='edit'));assert.deepEqual(state.meta.agents.map(a=>[a.id,a.x]),readingPositions);await page.screenshot({path:out+'/edit.png'});
 state=await seek(15);assert(state.meta.visualActions.some(a=>a.action==='test'&&a.state==='failed'));await page.screenshot({path:out+'/failure.png'});
 state=await seek(24.5);assert(state.meta.visualActions.some(a=>a.action==='test'&&a.state==='succeeded'));await page.screenshot({path:out+'/repaired.png'});
 state=await seek(27);assert(state.meta.visualActions.some(a=>a.action==='create'&&a.state==='succeeded'));await page.screenshot({path:out+'/create.png'});
 state=await seek(31);assert(state.meta.visualActions.some(a=>a.action==='delete'&&a.state==='succeeded'));await page.screenshot({path:out+'/delete.png'});
 state=await seek(36);assert(state.meta.visualActions.some(a=>a.action==='push'));await page.screenshot({path:out+'/push.png'});
 await page.locator('#flow-labels').click();assert((await page.evaluate(()=>window.fleetWorldSnapshot())).actionLabels);
 await page.locator('#live').click();await page.waitForFunction(()=>!window.fleetWorldSnapshot().demoEnabled&&window.fleetWorldSnapshot().scene.host!=='Workflow demo');
 await page.reload();await page.waitForFunction(()=>window.fleetWorldSnapshot?.().view==='flow'&&window.fleetWorldSnapshot().frames>5);
 await page.locator('#view-world').click();await page.waitForFunction(()=>window.fleetWorldSnapshot().meta.title==='The Lantern Works');
 await page.locator('#view-cabinets').click();await page.waitForFunction(()=>window.fleetWorldSnapshot().meta.title==='The Jacquard Observatory');
 assert.deepEqual(errors,[]);assert.deepEqual(posts,[]);
 await fs.writeFile(out+'/verification.json',JSON.stringify({liveEvents:live.scene.events.length,owners:live.meta.ownerGroups,realAvatars:live.meta.loadedAvatars.length,demonstration:'synthetic, client-local',actionLabelsInitiallyHidden:true,distinctActions:true,stableLocalAvatars:true,viewsPreserved:true,posts,errors},null,2));
 console.log('Flow, ownership, lifecycle encodings, demo isolation and all views passed.');
}finally{await context.close();await browser.close();}
