#!/usr/bin/env node
// Run against scripts/viz_preview.py; screenshots plus observable canvas checks.
import {chromium} from '@playwright/test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
const url=process.argv[2]||'http://127.0.0.1:7699/viz';
const output=process.argv[3]||'/var/tmp/fleet-map-test';
await fs.mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1440,height:900}});
const errors=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
try{
  await page.goto(url,{waitUntil:'networkidle'});
  await page.waitForFunction(()=>window.fleetMapSnapshot?.().nodes.length>0);
  await page.waitForTimeout(1500);
  const live=await page.evaluate(()=>window.fleetMapSnapshot());
  assert(live.nodes.some(n=>n.agent));
  assert(live.nodes.some(n=>!n.agent));
  assert(!live.nodes.some(n=>n.id==='repo:null'));
  assert.equal(new Set(live.nodes.map(n=>n.label)).size,live.nodes.length,'ambiguous node labels');
  await page.screenshot({path:path.join(output,'fleet-live.png')});
  await page.locator('#fit').click();
  await page.locator('#t').evaluate(s=>{s.value=500;s.dispatchEvent(new Event('input'));});
  await page.waitForTimeout(600);
  const scrub=await page.evaluate(()=>window.fleetMapSnapshot());
  assert(!scrub.live && scrub.cursor>0 && scrub.cursor<live.cursor);
  await page.screenshot({path:path.join(output,'fleet-replay.png')});
  // The map is a desktop world, not a phone layout. Verify the minimum-size
  // gate, then pan/zoom independently of world geometry at a supported size.
  await page.setViewportSize({width:390,height:844});
  assert(await page.locator('#small-screen').isVisible());
  assert(!(await page.locator('#c').isVisible()));
  await page.setViewportSize({width:1440,height:900});
  const cameraBefore=await page.evaluate(()=>window.fleetMapSnapshot().view);
  await page.mouse.move(700,450);await page.mouse.down();await page.mouse.move(900,550);await page.mouse.up();
  const cameraAfter=await page.evaluate(()=>window.fleetMapSnapshot().view);
  assert.equal(cameraAfter.x-cameraBefore.x,200);
  assert.equal(cameraAfter.y-cameraBefore.y,100);
  await page.mouse.wheel(0,-100);
  await page.waitForTimeout(100);
  assert((await page.evaluate(()=>window.fleetMapSnapshot().view.k))>cameraAfter.k);
  const now=Date.now();
  const fixture={archetypes:{pulse:{travel:.03,decay:.94,persist:false,weight:1,trail:0}},
    library_revision:1,events:[{agent:'Nadia',agent_id:'nadia',target:'unknown:novel',verb:'unknown',archetype:'pulse',ts:now-1000}],
    nodes:[{id:'unknown:novel',provisional:true}],coverage:{events:1,specific_pct:0}};
  await page.route('**/viz/events?*',route=>route.fulfill({json:fixture}));
  await page.reload({waitUntil:'networkidle'});
  await page.waitForFunction(()=>window.fleetMapSnapshot?.().nodes.some(n=>n.provisional));
  const pending=await page.evaluate(()=>window.fleetMapSnapshot());
  assert(pending.nodes.some(n=>n.id==='unknown:novel'));
  // A throwing program must fail in its worker, with frames and other nodes intact.
  fixture.events[0].target='toolchain:novel';
  fixture.nodes=[{id:'toolchain:novel',shape:'diamond',logic:[{op:'throw',args:[]}]}];
  fixture.library_revision=2;
  fixture.resolved={'unknown:novel':'toolchain:novel'};
  await page.waitForFunction(()=>window.fleetMapSnapshot?.().nodes.some(n=>n.drawing==='failed'),{},{timeout:10000});
  const before=await page.evaluate(()=>window.fleetMapSnapshot().frames);
  await page.waitForTimeout(300);
  assert((await page.evaluate(()=>window.fleetMapSnapshot().frames))>before+5);
  const failed=await page.evaluate(()=>window.fleetMapSnapshot());
  assert(failed.nodes.some(n=>n.agent));
  assert(!failed.nodes.some(n=>n.id==='unknown:novel'));
  await page.screenshot({path:path.join(output,'fleet-fallback.png')});
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({liveNodes:live.nodes.length,liveEvents:live.cursor,replayEvents:scrub.cursor,
    minimumSizeGate:true,panAndZoom:true,placeholder:true,throwFallback:true,consoleErrors:errors},null,2));
}finally{await browser.close();}
