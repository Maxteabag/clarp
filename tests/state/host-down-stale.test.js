// A Host that does not answer: what it last said is not happening now. Its agents' rows keep
// their name and last activity but show nothing working, so no spinner or timer runs on them,
// and the live row comes back with the Host (docs/design/host-not-answering.md).

import { beforeAll, describe, expect, it, vi } from 'vitest';

vi.mock('../../web/src/lib/net.js', () => ({ clog() {} }));
vi.mock('../../web/src/stores/conversations.svelte.js', () => ({ ensureLoaded() {}, reconcileWithSnapshot() {} }));

let app, bannerFor, statusFor, reach;
beforeAll(async () => {
  vi.stubGlobal('localStorage', { getItem: () => null, setItem() {} });
  vi.stubGlobal('document', { documentElement: { classList: { contains: () => false } } });
  ({ app, bannerFor, statusFor } = await import('../../web/src/stores/app.svelte.js'));
  reach = await import('../../web/src/stores/reach.svelte.js');
});

describe('stale live state while the serving Host is down', () => {
  it('shows nothing working, and the live row again when it answers', () => {
    app.status.iris = {
      name: 'Iris', busy: true, latest_state: 'compacting', last_activity: 123,
      activity: { summary: 'Compacting context' }, turn_started_at: 1000,
    };
    expect(statusFor('iris').busy).toBe(true);
    expect(bannerFor('iris')).toMatchObject({ spinner: true });

    reach.noteFail(reach.PRIMARY, { kind: 'refused' });
    const quiet = statusFor('iris');
    expect(quiet).toMatchObject({ name: 'Iris', busy: false, latest_state: '', last_activity: 123, turn_started_at: null });
    expect(quiet.activity).toBeNull();
    expect(bannerFor('iris')).toBeNull();
    // The row itself is untouched: the live state comes back with the Host.
    expect(app.status.iris.busy).toBe(true);

    reach.noteOk(reach.PRIMARY);
    expect(statusFor('iris').busy).toBe(true);
    expect(bannerFor('iris')).toMatchObject({ spinner: true });
  });

  it('is not quieted by another Host being down', () => {
    app.status.iris = { name: 'Iris', busy: true, latest_state: 'thinking' };
    reach.trackHost('peer:x', { label: 'Laptop', url: 'http://10.0.0.2', via: 'host', probe: async () => {} });
    reach.noteFail('peer:x', { kind: 'network', via: 'host' });
    expect(statusFor('iris').busy).toBe(true);
  });
});
