// A request that never settles must not leave an optimistic message looking
// pending forever. Drive the real send and delivery stores with a hung fetch.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DeliveryState } from '@core/delivery.js';

const flash = vi.fn();
const chime = vi.fn();

vi.mock('../../web/src/lib/net.js', () => ({ clog() {}, trace: { id: '' } }));
vi.mock('../../web/src/stores/app.svelte.js', () => ({
  app: { session: 'lena' }, flash, setSession: async () => {},
}));
vi.mock('../../web/src/stores/audio.svelte.js', () => ({
  audio: { muted: true }, chime,
  machine: { send() {}, settle() {} },
  playerAdapter: { interrupt() {} }, scheduler: { silence: () => 0 },
}));
vi.mock('../../web/src/stores/conversations.svelte.js', () => ({
  addOptimisticTurn() {},
}));

async function stores() {
  vi.resetModules();
  const delivery = await import('../../web/src/stores/delivery.svelte.js');
  const send = await import('../../web/src/stores/send.svelte.js');
  return { ...delivery, ...send };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('send request lifetime', () => {
  it('times out and marks a fetch that never answers as failed', async () => {
    const store = await stores();
    void store.sendText('is anyone there');
    await Promise.resolve();
    expect(store.delivery.entries[0].state).toBe(DeliveryState.PENDING);

    await vi.advanceTimersByTimeAsync(180_000);

    expect(store.delivery.entries[0].state).toBe(DeliveryState.FAILED);
  });
});
