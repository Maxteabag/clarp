import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  applyLiveEvent, applyLiveSnapshot, blankLive, liveItems,
} from '../../static/lib/live-items.js';

// The recorded live streams in contract/live are the shared test of
// docs/live-items.md. The Host's reference reducer (server/lib/live_items.py)
// is checked against them by tests/contract/test_live_fixtures.py; the web
// client feeds the same steps through its own reducer and must reach the
// same state.

const here = path.dirname(fileURLToPath(import.meta.url));
const dir = path.join(here, '..', '..', 'contract', 'live');
const fixtures = fs.readdirSync(dir).filter(f => f.endsWith('.json')).sort()
  .map(f => ({ name: f, body: JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8')) }));

function subset(actual, expected, where) {
  if (expected && typeof expected === 'object' && !Array.isArray(expected)) {
    expect(actual, `${where} is an object`).toBeTypeOf('object');
    for (const [key, value] of Object.entries(expected)) {
      expect(actual, `${where}.${key} present`).toHaveProperty([key]);
      subset(actual[key], value, `${where}.${key}`);
    }
  } else {
    expect(actual, where).toEqual(expected);
  }
}

describe('live fixtures', () => {
  it('covers the streams every client must replay', () => {
    const names = fixtures.map(f => f.name.replace(/\.json$/, ''));
    expect(names).toEqual(expect.arrayContaining(
      ['turn-full', 'gap-needs-snapshot', 'gap-recovers-from-snapshot']));
  });

  for (const { name, body } of fixtures) {
    it(`${name}: ${body.title}`, () => {
      let live = blankLive();
      const effects = [];
      for (const step of body.steps) {
        if (step.snapshot) {
          live = applyLiveSnapshot(live, step.snapshot);
        } else {
          const r = applyLiveEvent(live, step.event);
          live = r.state;
          effects.push(...r.effects);
        }
      }
      const want = body.expect;
      expect(effects).toEqual(want.effects);
      expect(live.lseq).toBe(want.lseq);
      subset(live.activity, want.activity, '$.activity');
      subset(live.turn, want.turn, '$.turn');
      const items = liveItems(live);
      expect(items.map(i => i.id)).toEqual(want.items.map(i => i.id));
      want.items.forEach((w, n) => subset(items[n], w, `$.items[${w.id}]`));
    });
  }
});
