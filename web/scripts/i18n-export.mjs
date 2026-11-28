#!/usr/bin/env node
// Writes the web app's English catalog as JSONL for translation, one line per key in catalog
// order: {"key","screen","en","comment","placeholders","plural"}. For a plural key `en` is its
// "other" form and `plural` holds every English form ({"one","other"}); otherwise `plural` is null.
// `screen` is the key's first dotted part (the screen or group it belongs to), so translators work
// screen by screen. Every key needs a translator note in comments.json; a key without one fails
// the export.
//
//   node web/scripts/i18n-export.mjs <out.jsonl | out-dir/>
//
// A directory (or a path ending in /) gets en.jsonl inside it.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const dir = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'src', 'lib', 'i18n');
const read = f => JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));

let out = process.argv[2];
if (!out) {
  console.error('usage: i18n-export.mjs <out.jsonl | out-dir/>');
  process.exit(2);
}
if (out.endsWith('/') || (fs.existsSync(out) && fs.statSync(out).isDirectory())) out = path.join(out, 'en.jsonl');

const en = read('en.json');
const comments = fs.existsSync(path.join(dir, 'comments.json')) ? read('comments.json') : {};

const missing = Object.keys(en).filter(k => !String(comments[k] ?? '').trim());
if (missing.length) {
  console.error(`missing note in comments.json for ${missing.length} key(s):\n  ${missing.join('\n  ')}`);
  process.exit(1);
}

const placeholders = s => [...new Set([...s.matchAll(/\{(\w+)\}/g)].map(m => m[1]))];

const lines = Object.entries(en).map(([key, v]) => {
  const plural = typeof v === 'object' ? v : null;
  const text = plural ? (plural.other ?? '') : v;
  const all = plural ? Object.values(plural).join(' ') : v;
  return JSON.stringify({ key, screen: key.split('.')[0], en: text, comment: comments[key], placeholders: placeholders(all), plural });
});

fs.mkdirSync(path.dirname(path.resolve(out)), { recursive: true });
fs.writeFileSync(out, lines.join('\n') + (lines.length ? '\n' : ''));
console.log(`${lines.length} keys -> ${out}`);
