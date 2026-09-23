import { describe, expect, it, vi } from 'vitest';
import { createCoalescedRefresh } from '../../static/lib/snapshot-refresh.js';

describe('coalesced snapshot refresh', () => {
  it('collapses a transcript burst into one bounded refresh', async () => {
    const timers = [];
    const refresh = vi.fn().mockResolvedValue(undefined);
    const coalesced = createCoalescedRefresh(refresh, {
      delayMs: 200,
      setTimeoutFn: fn => {
        timers.push(fn);
        return timers.length;
      },
    });

    coalesced.schedule();
    coalesced.schedule();
    coalesced.schedule();
    expect(refresh).not.toHaveBeenCalled();
    expect(timers).toHaveLength(1);

    await timers.shift()();
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(timers).toHaveLength(0);
  });

  it('queues one follow-up when a burst arrives during refresh', async () => {
    const timers = [];
    let release;
    let refreshCount = 0;
    const refresh = vi.fn(() => {
      if (refreshCount++ === 0) {
        return new Promise(resolve => { release = resolve; });
      }
      return Promise.resolve();
    });
    const coalesced = createCoalescedRefresh(refresh, {
      setTimeoutFn: fn => {
        timers.push(fn);
        return timers.length;
      },
    });

    coalesced.schedule();
    const first = timers.shift()();
    coalesced.schedule();
    coalesced.schedule();
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(timers).toHaveLength(0);

    release();
    await first;
    expect(timers).toHaveLength(1);
    await timers.shift()();
    expect(refresh).toHaveBeenCalledTimes(2);
  });
});
