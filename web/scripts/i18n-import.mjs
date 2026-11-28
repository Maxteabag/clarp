#!/usr/bin/env node
// Reads translations from <lang>.jsonl files and writes them into the web app's catalogs
// (web/src/lib/i18n/<lang>.json). Each line is {"key","text"} or {"key","plural":{"one",..,"other"}}.
// The language comes from the file name (nb.jsonl, zh-Hans.jsonl) or --lang.
//
//   node web/scripts/i18n-import.mjs [--lang nb] <nb.jsonl> [<fr.jsonl> ...]
//
// Keys a file does not mention keep their current translation. Keys English no longer has are
// kept at the end and listed as stale. Output is in English catalog order, so a rerun with the
// same input writes the same file.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const dir = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'src', 'lib', 'i18n');
const LANGUAGES = ['nb', 'zh-Hans', 'hi', 'es', 'fr'];
const readJson = f => (fs.existsSync(f) ? JSON.parse(fs.readFileSync(f, 'utf8')) : {});

const args = process.argv.slice(2);
let forced = null;
const files = [];
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--lang') forced = args[++i];
  else files.push(args[i]);
}
if (!files.length) {
  console.error('usage: i18n-import.mjs [--lang <lang>] <lang>.jsonl ...');
  process.exit(2);
}

const en = readJson(path.join(dir, 'en.json'));
const placeholders = v => [...new Set([...(typeof v === 'object' ? Object.values(v).join(' ') : v).matchAll(/\{(\w+)\}/g)].map(m => m[1]))].sort().join(',');
let failed = false;

for (const file of files) {
  const lang = forced || path.basename(file).replace(/\.jsonl$/, '');
  if (!LANGUAGES.includes(lang)) {
    console.error(`${file}: '${lang}' is not a translated language (${LANGUAGES.join(', ')})`);
    failed = true;
    continue;
  }
  const target = path.join(dir, `${lang}.json`);
  const current = readJson(target);
  let n = 0;
  fs.readFileSync(file, 'utf8').split('\n').forEach((line, i) => {
    if (!line.trim()) return;
    let row;
    try { row = JSON.parse(line); } catch (e) {
      console.error(`${file}:${i + 1}: not JSON (${e.message})`); failed = true; return;
    }
    const value = row.plural ?? row.text;
    if (!row.key || value == null) { console.error(`${file}:${i + 1}: needs key and text or plural`); failed = true; return; }
    if (!(row.key in en)) { console.warn(`${file}:${i + 1}: ${row.key} is not in en.json, skipped`); return; }
    if (typeof value === 'object' && (typeof value.other !== 'string')) {
      console.error(`${file}:${i + 1}: ${row.key} plural needs an "other" form`); failed = true; return;
    }
    if (placeholders(value) !== placeholders(en[row.key])) {
      console.warn(`${file}:${i + 1}: ${row.key} placeholders {${placeholders(value)}} differ from English {${placeholders(en[row.key])}}`);
    }
    current[row.key] = value;
    n++;
  });
  const out = {};
  for (const k of Object.keys(en)) if (k in current) out[k] = current[k];
  const stale = Object.keys(current).filter(k => !(k in en)).sort();
  for (const k of stale) out[k] = current[k];
  fs.writeFileSync(target, JSON.stringify(out, null, 2) + '\n');
  const missing = Object.keys(en).filter(k => !(k in out)).length;
  console.log(`${lang}: ${n} imported, ${missing} still English${stale.length ? `, stale: ${stale.join(' ')}` : ''}`);
}
process.exit(failed ? 1 : 0);
