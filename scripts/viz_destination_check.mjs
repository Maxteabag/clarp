#!/usr/bin/env node
// Deterministic, labeled lifecycle scenarios; no fixture events enter live state.
import {chromium} from '@playwright/test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const url=process.argv[2]||'http://localhost:7701/viz',out=process.argv[3]||'/var/tmp/fleet-destination-proof';await fs.mkdir(out,{recursive:true});
const browser=await chromium.launch();const p=await browser.newPage({viewport:{width:1600,height:1000}});
const start=Date.now()-60000;
const entities=[{id:'r',label:'Clarp',kind:'repository',path:'/repo',parent:null},{id:'d',label:'server',kind:'directory',path:'/repo/server',parent:'r'},
{id:'f',label:'api.py',kind:'file',path:'/repo/server/api.py',parent:'d',extension:'.py',purpose:'API source'},
{id:'g',label:'test_api.py',kind:'file',path:'/repo/server/test_api.py',parent:'d',extension:'.py',purpose:'API tests'},
{id:'created',label:'new.py',kind:'file',path:'/repo/server/new.py',parent:'d',extension:'.py',purpose:'New file'},
{id:'remote',label:'org/clarp',kind:'remote-repository',url:'https://github.com/org/clarp',parent:'github'},
{id:'unknown',label:'Unknown',kind:'unresolved',parent:null}];
const event=(id,ts,target,action,end,outcome)=>({id,ts:start+ts,finished_at:end===null?null:start+end,outcome,agent:'Nadia',agent_id:'n',world_target:target,world_targets:[target],workspace_target:target==='unknown'?null:'r',action,verb:action,location_scope:target==='unknown'?'unknown':'target',evidence:{path:entities.find(e=>e.id===target)?.path,raw:action}});
const events=[event(1,0,'f','read',10000,'succeeded'),event(2,12000,'f','edit',18000,'succeeded'),event(3,20000,'g','delete',24000,'failed'),event(4,28000,'r','commit',32000,'succeeded'),{...event(5,34000,'r','push',43000,'succeeded'),remote_target:'remote'},event(6,50000,'unknown','read',51000,'unknown'),event(7,54000,'created','create',57000,'succeeded')];
const payload={world:{host:'Test fixture',entities,relations:[{from:'r',to:'remote',kind:'remote'}],events,coverage_keys:[]},actors:[],library_revision:0,learning:{enabled:false}};
try{
 await p.route('**/viz/events?*',r=>r.fulfill({json:payload}));
 await p.goto(url);await p.waitForFunction(()=>window.fleetWorldSnapshot?.().frames>5);
 async function at(ms){await p.locator('#t').evaluate((s,ts)=>{const w=window.fleetWorldSnapshot();s.value=1000*(ts-w.scene.events[0].ts)/(w.timeline.until-w.timeline.since);s.dispatchEvent(new Event('input'));},start+ms);await p.waitForTimeout(250);return (await p.evaluate(()=>window.fleetWorldSnapshot())).meta.agents[0];}
 let a=await at(5000);assert.equal(a.target,'f');assert.equal(a.status,'running');assert.match(a.label,/Reading/);
 const snap=await p.evaluate(()=>window.fleetWorldSnapshot());const file=snap.meta.hits.find(h=>h.id==='f');
 assert(a.x>=file.x&&a.x<=file.x+file.w&&a.y>=file.y&&a.y<=file.y+file.h);
 await p.screenshot({path:out+'/reading-at-file.png'});
 a=await at(25000);assert.equal(a.status,'failed');assert.match(a.label,/failed/);
 await p.screenshot({path:out+'/failed-delete.png'});
 a=await at(30000);assert.equal(a.target,'r');assert.match(a.label,/Committing/);
 a=await at(38000);assert.equal(a.target,'r');assert.match(a.label,/Pushing/);
 await p.screenshot({path:out+'/push-from-checkout.png'});
 a=await at(52000);assert.equal(a.target,'unlocated');assert.match(a.label,/location unknown/);assert.notEqual(a.status,'running');
 await p.screenshot({path:out+'/unknown-location.png'});
 assert(!(await p.evaluate(()=>window.fleetWorldSnapshot().meta.hits)).some(h=>h.id==='created'));
 a=await at(55000);assert.equal(a.status,'running');assert.match(a.label,/Creating/);
 a=await at(58000);assert.equal(a.status,'succeeded');assert.match(a.label,/Created/);
 assert((await p.evaluate(()=>window.fleetWorldSnapshot().meta.hits)).some(h=>h.id==='created'));
 await p.screenshot({path:out+'/created-file.png'});
 console.log(JSON.stringify({readingAtFile:true,failedDelete:true,commitAtRepo:true,pushAtRepo:true,unknownLocation:true,creationLifecycle:true}));
}finally{await browser.close();}
