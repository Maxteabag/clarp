import { beforeAll, describe, expect, it, vi } from 'vitest';
vi.mock('../../web/src/lib/net.js', () => ({ clog() {} }));
vi.mock('../../web/src/stores/conversations.svelte.js', () => ({ ensureLoaded() {}, reconcileWithSnapshot() {} }));
let app, bannerFor;
beforeAll(async () => {
  vi.stubGlobal('localStorage', { getItem: () => null });
  vi.stubGlobal('document', { documentElement: { classList: { contains: () => false } } });
  ({ app, bannerFor } = await import('../../web/src/stores/app.svelte.js'));
});
describe('agent recovery banner', () => {
  it('shows recovery from snapshot and clears on resumed thinking or done', () => {
    app.status.test = { latest_state: 'thinking', activity: {
      action: 'reconnecting', summary: 'Reconnecting… Your message is saved.' } };
    expect(bannerFor('test')).toMatchObject({ spinner: true, msg: 'Reconnecting… Your message is saved.' });
    app.status.test = { latest_state: 'thinking', activity: { action: 'thinking' } };
    expect(bannerFor('test')).toBeNull();
    app.status.test = { latest_state: 'done' };
    expect(bannerFor('test')).toBeNull();
  });
  it('shows a failed reply without claiming the whole account has no quota', () => {
    app.status.test = { latest_state: 'interrupted', activity_summary: 'Codex could not complete this request. Try again. Your message is saved.' };
    expect(bannerFor('test').msg).toContain('Your message is saved');
    expect(bannerFor('test').msg).not.toContain('Usage limit');
  });
});
