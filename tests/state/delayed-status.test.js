// The status line must not flash: a status that clears within 400 ms is
// never shown, and one that is shown stays at least 400 ms (T3's
// createDelayedStatus). A change of headline inside the same state shows at
// once; a change of state waits like a new status.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createDelayedStatus } from '@core/delayed-status.js';

const thinking = (text = 'Thinking') => ({ key: 'thinking:', text });
const tool = (id, text) => ({ key: `tool:${id}`, text });

describe('delayed status', () => {
  let shown;
  let status;
  beforeEach(() => {
    vi.useFakeTimers();
    shown = [];
    status = createDelayedStatus({ onChange: v => shown.push(v && v.text) });
  });
  afterEach(() => { status.dispose(); vi.useRealTimers(); });

  it('never shows a status that clears within 400 ms', () => {
    status.set(tool('a', 'Running ls'));
    vi.advanceTimersByTime(250);
    status.set(null);
    vi.advanceTimersByTime(2000);
    expect(shown).toEqual([]);
    expect(status.current()).toBe(null);
  });

  it('shows a status that lasts 400 ms', () => {
    status.set(thinking());
    vi.advanceTimersByTime(399);
    expect(shown).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(shown).toEqual(['Thinking']);
  });

  it('keeps a shown status at least 400 ms', () => {
    status.set(thinking());
    vi.advanceTimersByTime(400);
    status.set(null);
    vi.advanceTimersByTime(399);
    expect(status.current().text).toBe('Thinking');
    vi.advanceTimersByTime(1);
    expect(status.current()).toBe(null);
  });

  it('updates the headline of the shown state at once', () => {
    status.set(thinking());
    vi.advanceTimersByTime(400);
    status.set(thinking('Thinking: Finding the flaky test'));
    expect(status.current().text).toBe('Thinking: Finding the flaky test');
  });

  it('skips a tool that finished before it would have shown', () => {
    status.set(thinking());
    vi.advanceTimersByTime(1000);
    status.set(tool('a', 'Running ls'));
    vi.advanceTimersByTime(100);
    status.set(thinking());
    vi.advanceTimersByTime(2000);
    expect(shown).toEqual(['Thinking']);
  });

  it('moves to a tool that keeps running', () => {
    status.set(thinking());
    vi.advanceTimersByTime(1000);
    status.set(tool('a', 'Running npm test'));
    vi.advanceTimersByTime(400);
    expect(shown).toEqual(['Thinking', 'Running npm test']);
  });

  it('replaces a stale status within 400 ms even while the new one keeps changing', () => {
    status.set(tool('a', 'Running npm test'));
    vi.advanceTimersByTime(1000);
    for (let i = 0; i < 10; i++) {
      status.set(i % 2 ? thinking() : { key: 'responding:', text: 'Responding' });
      vi.advanceTimersByTime(150);
    }
    expect(status.current().text).not.toBe('Running npm test');
  });
});
