#!/usr/bin/env node
import {chromium} from '@playwright/test';
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
const url=process.argv[2],out=process.argv[3];if(!url||!out)throw Error('Usage: node scripts/viz_explore_check.mjs URL OUTPUT');
await fs.mkdir(out,{recursive:true});const browser=await chromium.launch();const results=[];
try{
 for(const viewport of [{width:1440,height:1000},{width:390,height:844}]){
  const context=await browser.newContext({viewport}),page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  const target=new URL(url);target.searchParams.set('view','flow');await page.goto(target.href);
  await page.locator('#activity-feed').evaluate(e=>e.open=true);
  await page.waitForFunction(()=>document.querySelectorAll('#activity-items li').length>0);
  const liveCount=await page.locator('#activity-items li').count();assert(liveCount<=400);
  await page.locator('#activity-pause').click();const paused=await page.locator('#activity-items').textContent();await page.waitForTimeout(1200);assert.equal(await page.locator('#activity-items').textContent(),paused);
  await page.screenshot({path:`${out}/${viewport.width}-real-activity.png`});
  const now=Date.now(),repo={id:'repo',kind:'repository',path:'/work/clarp',main_path:'/work/clarp',label:'Clarp'},ios={id:'ios',kind:'repository',path:'/work/clarp-ios',main_path:'/work/clarp-ios',label:'iOS'};
  const files=['desktop/main.cpp','server/api.py','README.md'].map((path,i)=>({id:'f'+i,kind:'file',parent:'repo',path:repo.path+'/'+path,label:path.split('/').at(-1)}));
  files.push({id:'swift',kind:'file',parent:'ios',path:ios.path+'/App.swift',label:'App.swift'});
  const events=files.map((f,i)=>({id:'event'+i,ts:now-1000,agent_id:'agent'+i,agent:['C++ fixture','Server fixture','Root fixture','iOS fixture'][i],world_target:f.id,workspace_target:f.parent,action:'edit',outcome:'running',evidence:{path:f.path,raw:'Synthetic recorded edit'}}));
  const remotes=[{id:'github',kind:'platform',label:'GitHub'},{id:'github:Maxteabag',kind:'organization',label:'Maxteabag',parent:'github'},...['clarp','clarp-ios'].map(name=>({id:'github:Maxteabag/'+name,kind:'remote-repository',label:name,parent:'github:Maxteabag'}))];
  const scene={host:'Synthetic component fixture',entities:[repo,ios,...files,...remotes],events,relations:[{from:'repo',to:'github:Maxteabag/clarp',kind:'remote',label:'origin'},{from:'ios',to:'github:Maxteabag/clarp-ios',kind:'remote',label:'origin'}],work:{available:true,synthetic:true,plans:[],artifacts:[],messages:[]}};
  await page.route('**/viz/events?*',r=>r.fulfill({json:{world:scene,actors:[],library_revision:1,learning:{enabled:false}}}));
  await page.reload();await page.waitForFunction(()=>window.fleetWorldSnapshot?.().frames>5);
  await page.locator('#activity-feed').evaluate(e=>e.open=false);
  await page.locator('#learning').evaluate(e=>e.textContent='Synthetic multi-component fixture');
  const snap=()=>page.evaluate(()=>window.fleetWorldSnapshot());
  async function zoom(wanted){
   for(let i=0;i<65;i++){const s=await snap();if(Math.abs(Math.log(s.camera.k/wanted))<.08)return s;
    const repo=s.meta.workspaces.find(w=>w.id==='repo');const x=Math.max(20,Math.min(viewport.width-20,repo.x*s.camera.k+s.camera.x)),y=Math.max(180,Math.min(viewport.height-180,repo.y*s.camera.k+s.camera.y));
    await page.mouse.move(x,y);await page.mouse.wheel(0,s.camera.k<wanted?-100:100);await page.waitForTimeout(60);
   }throw Error('zoom did not reach target');
  }
  await zoom(.3);await page.waitForTimeout(200);const overview=await snap();assert.equal(overview.meta.components.length,0);
  assert.equal(overview.meta.projects.length,1,'Clarp and iOS retain separate repo boundaries within one project');
  await zoom(.7);await page.waitForTimeout(200);const middle=await snap();assert.deepEqual(middle.meta.components.map(c=>c.name),['desktop','server']);
  assert.deepEqual(middle.meta.workspaces,overview.meta.workspaces,'zoom never rearranges repositories');
  await page.screenshot({path:`${out}/${viewport.width}-components.png`});
  await zoom(1.3);await page.waitForTimeout(200);const close=await snap();assert(close.meta.files>middle.meta.files);
  assert.deepEqual(close.meta.workspaces,middle.meta.workspaces);
  await page.screenshot({path:`${out}/${viewport.width}-files.png`});
  await page.locator('#heatmap').click();await page.locator('#heat-style').click();assert.equal(await page.locator('#heat-style').textContent(),'Pixelated heat');
  await page.waitForTimeout(250);await page.screenshot({path:`${out}/${viewport.width}-pixelated.png`});
  await page.reload();await page.waitForFunction(()=>window.fleetWorldSnapshot?.().frames>5);assert.equal(await page.locator('#heat-style').textContent(),'Pixelated heat');
  assert.deepEqual(errors,[]);results.push({viewport,liveCount,components:middle.meta.components.map(c=>c.name),layoutStable:true,pixelPreference:true,errors});await context.close();
 }
 await fs.writeFile(out+'/verification.json',JSON.stringify({real:'Activity feed screenshots use recorded Clarp events',synthetic:'Component/zoom screenshots use an explicit multi-repository fixture',results},null,2));
 console.log('Real activity feed pause, component reveal, stable layout, file detail and pixelated heat preference passed on desktop/phone.');
}finally{await browser.close();}
