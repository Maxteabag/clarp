// Opening a chat the device has seen before paints it from the device's copy
// at once, then asks /log only for what changed after the cached cursor.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const rows = new Map();
vi.mock('../../web/src/lib/idb-transcripts.js', () => ({
  idbTranscriptStorage: () => ({
    get: async s => rows.get(s) || null,
    put: async r => { rows.set(r.session, r); },
    delete: async s => { rows.delete(s); },
  }),
}));
vi.mock('../../web/src/lib/net.js', () => ({ clog: vi.fn(), instanceId: () => 'x' }));

let requests;
let release;

beforeEach(() => {
  rows.clear();
  requests = [];
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {} });
  vi.stubGlobal('fetch', vi.fn(url => {
    requests.push(String(url));
    return new Promise(resolve => {
      release = body => resolve({ ok: true, status: 200, json: async () => body });
    });
  }));
});
afterEach(() => vi.unstubAllGlobals());

const load = async () => { vi.resetModules(); return import('../../web/src/stores/conversations.svelte.js'); };
const tick = () => new Promise(r => setTimeout(r, 0));

describe('cache-first open', () => {
  it('paints the cached page before the Host answers, then fetches only the changes', async () => {
    rows.set('rachel', { session: 'rachel', conversation_id: 'c1', latest_revision: 40, has_more: true,
      latest_ts: '', cwd: '/w', turns: [{ id: 'm1', role: 'assistant', text: 'cached', revision: 40 }] });
    const store = await load();
    store.ensureLoaded('rachel');
    await tick(); await tick();
    const conv = store.conversation('rachel');
    expect(conv.status).toBe('ready');
    expect(conv.turns.map(t => t.text)).toEqual(['cached']);
    expect(requests).toHaveLength(1);
    expect(requests[0]).toContain('after_revision=40');
    release({ conversation_id: 'c1', latest_revision: 41, has_more: false,
      turns: [{ id: 'm2', role: 'user', text: 'new', revision: 41 }] });
    await tick(); await tick();
    expect(store.conversation('rachel').turns.map(t => t.text)).toEqual(['cached', 'new']);
  });

  it('loads the newest page as before when the device has no copy', async () => {
    const store = await load();
    store.ensureLoaded('mike');
    await tick(); await tick();
    expect(requests).toHaveLength(1);
    expect(requests[0]).not.toContain('after_revision');
  });

  it('drops the copy when the Host says the conversation was replaced', async () => {
    rows.set('rachel', { session: 'rachel', conversation_id: 'c1', latest_revision: 40, has_more: false,
      latest_ts: '', cwd: '', turns: [{ id: 'm1', role: 'assistant', text: 'old life', revision: 40 }] });
    const store = await load();
    store.ensureLoaded('rachel');
    await tick(); await tick();
    release({ conversation_id: 'c2', latest_revision: 2, turns: [] });
    await tick(); await tick();
    expect(requests[1]).not.toContain('after_revision');
    release({ conversation_id: 'c2', latest_revision: 2, has_more: false,
      turns: [{ id: 'n1', role: 'user', text: 'fresh', revision: 2 }] });
    await tick(); await tick();
    expect(store.conversation('rachel').turns.map(t => t.text)).toEqual(['fresh']);
  });
});
