import { beforeAll, describe, expect, it, vi } from 'vitest';
vi.mock('../../web/src/lib/net.js', () => ({ clog() {} }));
vi.mock('../../web/src/stores/conversations.svelte.js', () => ({ ensureLoaded() {}, reconcileWithSnapshot() {} }));
let app, bannerFor, quotaMessage;
beforeAll(async () => {
  vi.stubGlobal('localStorage', { getItem: () => null });
  vi.stubGlobal('document', { documentElement: { classList: { contains: () => false } } });
  ({ app, bannerFor, quotaMessage } = await import('../../web/src/stores/app.svelte.js'));
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
  it('warns before a send when the backend is out of quota, not while a turn runs', () => {
    const quota = { state: 'exhausted', provider_id: 'codex', resets_at: null, fallback_model: null };
    app.status.test = { latest_state: 'interrupted', backend_quota: quota };
    expect(bannerFor('test')).toMatchObject({ cls: 'quota', msg: 'Codex is out of quota. A message sent now will likely fail.' });
    app.status.test = { latest_state: 'thinking', busy: true, backend_quota: quota };
    expect(bannerFor('test')).toBeNull();
    app.status.test = { latest_state: 'done', backend_quota: null };
    expect(bannerFor('test')).toBeNull();
  });
  it('names the reset time and the fallback that will take the message', () => {
    const now = new Date('2026-09-17T06:00:00Z');
    const msg = quotaMessage({ provider_id: 'codex', resets_at: '2026-09-22T11:08:49Z',
      fallback_model: 'claude-sonnet-4-6' }, now);
    expect(msg).toMatch(/^Codex is out of quota until .*22.*\. Messages will run on claude-sonnet-4-6 instead\.$/);
    expect(quotaMessage({ provider_id: 'codex', resets_at: '2026-09-01T00:00:00Z' }, now))
      .toBe('Codex is out of quota. A message sent now will likely fail.');
  });
  it('says credits are gone rather than promising a reset will fix it', () => {
    const now = new Date('2026-09-17T06:00:00Z');
    const msg = quotaMessage({ provider_id: 'codex', reason: 'credits_depleted',
      resets_at: '2026-09-22T11:08:49Z', fallback_model: null }, now);
    expect(msg).toMatch(/^Codex workspace is out of credits · included usage resets .*22.*\. A message sent now will likely fail\.$/);
    expect(quotaMessage({ provider_id: 'codex', reason: 'credits_depleted' }, now))
      .toBe('Codex workspace is out of credits. A message sent now will likely fail.');
  });
});
