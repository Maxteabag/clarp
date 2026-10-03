// Switching agents or re-rendering the transcript must not re-parse turns
// that did not change: markdown + sanitising is the expensive part of a turn,
// and a remount should be a string insert. Keyed by (id, revision).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

let parses;

beforeEach(() => {
  parses = 0;
  vi.stubGlobal('marked', { setOptions: vi.fn(), parse: s => { parses++; return `<p>${s}</p>`; } });
  vi.stubGlobal('DOMPurify', { sanitize: s => s });
});
afterEach(() => vi.unstubAllGlobals());

const load = async () => { vi.resetModules(); return import('../../web/src/lib/render.js'); };

describe('turn render cache', () => {
  it('parses a turn once however often it is rendered', async () => {
    const { renderTurnBodyCached } = await load();
    const turn = { id: 'm1', revision: 3, text: 'Hello' };
    const first = renderTurnBodyCached(turn);
    expect(renderTurnBodyCached({ ...turn })).toBe(first);
    expect(parses).toBe(1);
  });

  it('parses again when the revision moves', async () => {
    const { renderTurnBodyCached } = await load();
    renderTurnBodyCached({ id: 'm1', revision: 3, text: 'Hello' });
    expect(renderTurnBodyCached({ id: 'm1', revision: 4, text: 'Hello there' })).toContain('Hello there');
    expect(parses).toBe(2);
  });

  it('does not cache a turn without an id or revision', async () => {
    const { renderTurnBodyCached } = await load();
    renderTurnBodyCached({ text: 'a' });
    renderTurnBodyCached({ text: 'a' });
    expect(parses).toBe(2);
  });

  it('sets the markdown options once, not per render', async () => {
    const { renderText } = await load();
    renderText('a'); renderText('b');
    expect(globalThis.marked.setOptions).toHaveBeenCalledTimes(1);
  });
});
