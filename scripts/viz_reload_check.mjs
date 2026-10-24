#!/usr/bin/env node
// Exercise real saves only in a disposable fixture, never in the user's site.
import {chromium} from '@playwright/test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const root=process.cwd(),fixture=await fs.mkdtemp(path.join(os.tmpdir(),'viz-reload-check-'));
await fs.mkdir(path.join(fixture,'static/lib'),{recursive:true});
const html=path.join(fixture,'static/viz.html'),css=path.join(fixture,'static/lib/viz-test.css'),js=path.join(fixture,'static/lib/viz-test.js');
await fs.writeFile(html,'<link rel="stylesheet" href="/static/lib/viz-test.css"><p id="test">Before</p><script type="module" src="/static/lib/viz-test.js"></script>');
await fs.writeFile(css,'body{color:rgb(255,0,0)}');await fs.writeFile(js,'window.probeVersion=1;');
await fs.copyFile('static/lib/viz-dev-reload.js',path.join(fixture,'static/lib/viz-dev-reload.js'));
const child=spawn(path.join(root,'.venv/bin/python'),['-u','-c','import sys,pathlib;sys.path.insert(0,sys.argv.pop(1));import viz_preview as p;p.ROOT=pathlib.Path(sys.argv.pop(1));p.main()',path.join(root,'scripts'),fixture,'--db',path.join(fixture,'unused.sqlite'),'--port','0','--reload'],{cwd:root});
let output='';child.stdout.on('data',d=>output+=d);child.stderr.on('data',d=>output+=d);
const browser=await chromium.launch();
try{
 let port;for(let i=0;i<100;i++){port=output.match(/Preview http:\/\/127.0.0.1:(\d+)/)?.[1];if(port)break;await new Promise(r=>setTimeout(r,50));}
 assert(port,output);
 const page=await browser.newPage();await page.goto(`http://127.0.0.1:${port}/viz?view=flow`);
 await page.waitForFunction(()=>window.probeVersion===1);const original=await page.evaluate(()=>performance.timeOrigin);
 await fs.writeFile(css,'body{color:rgb(0,255,0)}');
 await page.waitForFunction(()=>getComputedStyle(document.body).color==='rgb(0, 255, 0)');
 assert.equal(await page.evaluate(()=>performance.timeOrigin),original,'CSS is replaced without navigation');
 await fs.writeFile(js,'window.probeVersion=2;');await page.waitForFunction(()=>window.probeVersion===2);
 assert.notEqual(await page.evaluate(()=>performance.timeOrigin),original,'JavaScript save reloads the page');
 assert(page.url().includes('view=flow'));
 await fs.writeFile(html,'<p id="test">After</p>');await page.waitForFunction(()=>document.querySelector('#test')?.textContent==='After');
 console.log('Real saves verified: CSS changes in place; JavaScript and HTML reload automatically; URL preserved.');
}finally{await browser.close();child.kill('SIGTERM');await new Promise(r=>child.exitCode!==null?r():child.once('exit',r));await fs.rm(fixture,{recursive:true,force:true});}
