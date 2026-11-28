// When a Host does not answer (docs/design/host-not-answering.md): the classifier, the backoff,
// the copy, the private-data-free diagnostics, and that one Host's state never touches another's.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import { formatCountdown } from '../../web/src/lib/i18n/format.js';
import {
  BACKOFF_MS, EN, english, addressKind, answerFailure, attemptLabel, backoffDelay, blankReach, classify,
  diagnosticsText, fetchFailure, lastSeenAt, peerAttempt, reachFail, reachOk, reasonBody,
  reasonGist, reasonSeverity, reasonTitle, sha256Hex, statusLine,
} from '../../static/lib/host-reach.js';

vi.mock('../../web/src/lib/net.js', () => ({ clog: vi.fn() }));

describe('backoff', () => {
  it('waits 2 s, 5 s, 10 s, 30 s, then every 60 s', () => {
    expect([1, 2, 3, 4, 5, 6, 40].map(backoffDelay)).toEqual([2000, 5000, 10000, 30000, 60000, 60000, 60000]);
    expect(BACKOFF_MS).toEqual([2000, 5000, 10000, 30000, 60000]);
  });

  it('keeps the first failure as down_since and schedules from the failure count', () => {
    let r = reachFail(blankReach(), { kind: 'refused' }, 'server_stopped', 1000);
    r = reachFail(r, { kind: 'refused' }, 'server_stopped', 3000);
    expect(r).toMatchObject({ down: true, downSince: 1000, failures: 2, nextAt: 8000 });
    const back = reachOk(r, 9000, { hostVersion: '1.2', contract: 140 });
    expect(back).toMatchObject({ down: false, downSince: 0, failures: 0, lastOk: 9000, backUntil: 11000, contract: 140 });
  });

  it('keeps a down_since from before the last answer out', () => {
    expect(blankReach({ lastOk: 500, downSince: 400 }).downSince).toBe(0);
    expect(blankReach({ lastOk: 500, downSince: 600 }).downSince).toBe(600);
  });
});

describe('addresses', () => {
  it.each([
    ['http://100.64.0.123:7690', 'tailnet'], ['https://studio.tail1234.ts.net', 'tailnet'],
    ['http://[fd7a:115c:a1e0::1]:7690', 'tailnet'], ['http://100.128.0.1', 'public'],
    ['http://192.168.1.4:7690', 'lan'], ['http://10.0.0.2', 'lan'], ['http://studio.local', 'lan'],
    ['http://127.0.0.1:7699', 'loopback'], ['http://localhost:5174', 'loopback'],
    ['https://clarp.example.com', 'public'], ['not a url', 'unknown'],
  ])('%s is %s', (url, kind) => expect(addressKind(url)).toBe(kind));
});

describe('classifier', () => {
  const tailnetDown = { address: 'tailnet', ok: false };
  const lanUp = { address: 'lan', ok: true };

  it('first match wins, in the spec order', () => {
    expect(classify({ kind: 'http', status: 401 }, { deviceOnline: false })).toBe('device_offline');
    expect(classify({ kind: 'http', status: 401 })).toBe('auth_expired');
    expect(classify({ kind: 'http', status: 403 })).toBe('auth_expired');
    expect(classify({ kind: 'dns' })).toBe('wrong_address');
    expect(classify({ kind: 'tls' })).toBe('wrong_address');
    expect(classify({ kind: 'not_this_host', status: 404 })).toBe('wrong_address');
    expect(classify({ kind: 'refused' })).toBe('server_stopped');
    expect(classify({ kind: 'http', status: 502 })).toBe('server_stopped');
    expect(classify({ kind: 'http', status: 500 })).toBe('unknown');
  });

  it('says tailscale_off only for a tailnet Host when no other tailnet Host answers', () => {
    const t = { kind: 'timeout', ms: 8000 };
    expect(classify(t, { address: 'tailnet', others: [tailnetDown] })).toBe('tailscale_off');
    expect(classify(t, { address: 'tailnet', others: [tailnetDown, { address: 'tailnet', ok: true }] })).toBe('host_asleep');
    expect(classify(t, { address: 'tailnet', deviceTunnel: false })).toBe('tailscale_off');
    // A page cannot see Tailscale; with nothing to compare against it does not guess.
    expect(classify(t, { address: 'tailnet', others: [] })).toBe('unknown');
    expect(classify(t, { address: 'lan', others: [tailnetDown] })).toBe('unknown');
  });

  it('says host_asleep when another Host answers', () => {
    expect(classify({ kind: 'timeout' }, { address: 'lan', others: [lanUp] })).toBe('host_asleep');
  });

  it('never blames this device\'s Tailscale for a peer the serving Host reads', () => {
    expect(classify({ kind: 'timeout' }, { address: 'tailnet', viaHost: true, others: [tailnetDown] })).toBe('host_asleep');
    expect(classify(peerAttempt('unreachable'), { address: 'tailnet', viaHost: true, others: [tailnetDown] })).toBe('unknown');
  });

  it('reads the serving Host\'s peer error codes', () => {
    expect(classify(peerAttempt('refused'))).toBe('auth_expired');
    expect(classify(peerAttempt('not_json'))).toBe('wrong_address');
    expect(classify(peerAttempt('http_404'))).toBe('wrong_address');
    expect(classify(peerAttempt('http_502'))).toBe('server_stopped');
    expect(classify(peerAttempt('connect_refused'))).toBe('server_stopped');
  });

  it('tells a page\'s failures apart only as far as a page can', () => {
    expect(fetchFailure({ timedOut: true, ms: 8000 })).toEqual({ kind: 'timeout', ms: 8000 });
    expect(fetchFailure({ sameOrigin: true }).kind).toBe('refused');
    expect(fetchFailure({ sameOrigin: false }).kind).toBe('network');
    // A *.ts.net name served by Tailscale answers 502 when Clarp is stopped; failing by itself
    // means the name is gone.
    expect(classify(fetchFailure({ sameOrigin: true, tailnetName: true }), { address: 'tailnet' })).toBe('tailscale_off');
    expect(classify(fetchFailure({ sameOrigin: false, tailnetName: true }), { address: 'tailnet' })).toBe('unknown');
    expect(answerFailure(200, { json: true })).toBeNull();
    expect(answerFailure(200, { json: false })).toEqual({ kind: 'not_this_host', status: 200 });
    expect(answerFailure(200, { json: true, serverId: 'b', expectedId: 'a' }).kind).toBe('not_this_host');
    expect(answerFailure(401)).toEqual({ kind: 'http', status: 401 });
  });
});

describe('copy', () => {
  it('uses the spec\'s keys and exact English', () => {
    expect(english('host_down.list_row', { host: 'Studio', when: '2 hours ago' })).toBe('Studio isn\'t answering · 2 hours ago');
    expect(english('host_down.read_only', { host: 'Studio' })).toBe('Showing saved messages. You can send again when Studio is back.');
    expect(english('host_down.banner')).toBe('This device is offline');
    expect(english('host_down.copy_diagnostics')).toBe('Copy diagnostics');
    expect(reasonTitle('auth_expired', 'Studio')).toBe('Signed out of Studio');
    expect(reasonBody('auth_expired', 'Studio')).toBe('This device is no longer signed in to Studio.');
    expect(reasonTitle('wrong_address', 'Studio')).toBe('Studio isn\'t at this address');
    expect(reasonBody('wrong_address', 'Studio')).toBe('The address may have changed.');
    expect(reasonTitle('server_stopped', 'Studio')).toBe('Studio isn\'t answering');
    expect(reasonBody('server_stopped', 'Studio')).toBe('The computer is on, but Clarp isn\'t running on it.');
    expect(reasonBody('nonsense', 'Studio')).toBe('Studio isn\'t responding right now.');
    expect(reasonBody('toString', 'Studio')).toBe('Studio isn\'t responding right now.');
  });

  it('keeps the web catalog\'s English the same as the rules\'', () => {
    const en = JSON.parse(fs.readFileSync(new URL('../../web/src/lib/i18n/en.json', import.meta.url), 'utf8'));
    for (const [key, text] of Object.entries(EN)) expect(en[key]).toBe(text);
    for (const id of ['device_offline', 'auth_expired', 'wrong_address', 'server_stopped', 'tailscale_off', 'host_asleep', 'unknown']) {
      expect(en[`host_down.${id}.title`]).toBeTruthy();
      expect(en[`host_down.${id}.body`]).toBeTruthy();
    }
  });

  it('is warning yellow only where the user must act, never red', () => {
    expect(reasonSeverity('auth_expired')).toBe('warning');
    expect(reasonSeverity('wrong_address')).toBe('warning');
    for (const id of ['device_offline', 'server_stopped', 'tailscale_off', 'host_asleep', 'unknown']) {
      expect(reasonSeverity(id)).toBe('info');
    }
  });

  it('takes a translator and the app\'s language', () => {
    const tr = (key, p) => `<${key}${p ? ':' + Object.values(p).join(',') : ''}>`;
    expect(reasonTitle('host_asleep', 'Studio', tr)).toBe('<host_down.host_asleep.title:Studio>');
    const down = { ...blankReach(), down: true, nextAt: 30_000 };
    expect(statusLine(down, 0, tr, ms => formatCountdown(ms, 'long', 'nb'))).toBe('<host_down.status.retry_in:om 30 sekunder>');
  });

  it('keeps the status line quiet and counting, by the relative formatter', () => {
    const down = { ...blankReach(), down: true, nextAt: 10_000 };
    expect(statusLine({ ...down, inFlight: true }, 0)).toBe('Reconnecting automatically…');
    const en = ms => formatCountdown(ms, 'long', 'en');
    expect(statusLine(down, 5_000, english, en)).toBe('Trying again in 5 seconds');
    expect(statusLine({ ...down, nextAt: 125_000 }, 5_000, english, en)).toBe('Trying again in 2 minutes');
    expect(statusLine({ ...blankReach(), backUntil: 2000 }, 1000)).toBe('Back online');
    expect(statusLine(blankReach(), 5000)).toBe('');
    expect(formatCountdown(1000, 'long', 'en')).toBe('in 1 second');
  });

  it('says when the Host last answered: last_ok, else down_since', () => {
    expect(lastSeenAt({ lastOk: 5, downSince: 9 })).toBe(5);
    expect(lastSeenAt({ lastOk: 0, downSince: 9 })).toBe(9);
    expect(lastSeenAt(null)).toBe(0);
  });
});

describe('diagnostics', () => {
  const secrets = {
    url: 'http://studio.tail1234.ts.net:7690', key: 'srv-9f2c1e-secret-id', token: 'cld_supersecret',
  };

  it('is at most 15 lines and carries no address, name, id or token', () => {
    let rec = reachFail(blankReach(), { kind: 'timeout', ms: 8000 }, 'tailscale_off', Date.UTC(2026, 10, 28, 8, 0));
    rec = { ...rec, hostVersion: '0.9.1', contract: 141 };
    const text = diagnosticsText({
      version: '0.5.3', build: 'claude-pwa-v9', hostKey: secrets.key, url: secrets.url, rec,
      othersOk: 0, othersTotal: 1, deviceOnline: true, network: 'wifi', tunnel: null,
      now: Date.UTC(2026, 10, 28, 8, 5),
    });
    const lines = text.split('\n');
    expect(lines.length).toBeLessThanOrEqual(15);
    for (const s of [secrets.url, 'studio', 'tail1234', 'ts.net', '7690', secrets.key, secrets.token]) {
      expect(text).not.toContain(s);
    }
    expect(lines[0]).toBe('Clarp diagnostics');
    expect(text).toContain(`Host: #${sha256Hex(secrets.key).slice(0, 6)} · address kind: tailnet · scheme: http`);
    expect(text).toContain('State: not answering since 2026-11-28T08:00:00Z (5 min)');
    expect(text).toContain('Likely reason: tailscale_off — Tailscale doesn\'t seem to be connected on this device.');
    expect(text).toContain('Last attempt: timeout after 8 s');
    expect(text).toContain('Other Hosts answering: 0 of 1');
    expect(text).toContain('Device network: online (wifi) · Tailscale/VPN on device: unknown');
    expect(text).toContain('Host version: 0.9.1 · contract 141');
  });

  it('names no Host in the reason sentence', () => {
    const rec = reachFail(blankReach(), { kind: 'http', status: 401 }, 'auth_expired', 0);
    expect(diagnosticsText({ url: 'http://x', rec, now: 1000 }))
      .toContain('Likely reason: auth_expired — This device is no longer signed in to the Host.');
    expect(reasonGist('wrong_address')).toBe('The Host isn\'t at this address.');
    expect(attemptLabel({ kind: 'http', status: 503 })).toBe('HTTP 503');
  });

  it('hashes with sha256', () => {
    expect(sha256Hex('')).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
    expect(sha256Hex('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
    expect(sha256Hex('a'.repeat(1000))).toBe('41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3');
  });
});

describe('per-Host store', () => {
  let store;
  beforeEach(async () => {
    vi.resetModules();
    vi.useFakeTimers();
    const mem = {};
    vi.stubGlobal('localStorage', {
      getItem: k => (k in mem ? mem[k] : null), setItem: (k, v) => { mem[k] = String(v); },
    });
    store = await import('../../web/src/stores/reach.svelte.js');
  });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it('keeps one Host\'s failure off every other Host', () => {
    store.trackHost('a', { label: 'Studio', url: 'http://100.64.0.1:7690', via: 'direct', probe: async () => {} });
    store.trackHost('b', { label: 'Laptop', url: 'http://100.64.0.2:7690', via: 'direct', probe: async () => {} });
    store.noteOk('b');
    store.noteFail('a', { kind: 'refused' });
    expect(store.isDown('a')).toBe(true);
    expect(store.isDown('b')).toBe(false);
    expect(store.isDown(store.PRIMARY)).toBe(false);
    expect(store.downHosts(['primary', 'a', 'b']).map(h => h.id)).toEqual(['a']);
    expect(store.othersAnswering('a')).toEqual({ ok: 1, total: 1 });
  });

  it('asks a down Host again on its backoff, and recovers on an answer', async () => {
    let answer = false;
    const probe = vi.fn(async () => { if (answer) store.noteOk('a'); else store.noteFail('a', { kind: 'refused' }); });
    const recovered = vi.fn();
    store.trackHost('a', { label: 'Studio', url: 'http://10.0.0.5', via: 'direct', probe });
    store.onRecover('a', recovered);
    store.noteFail('a', { kind: 'refused' });
    await vi.advanceTimersByTimeAsync(2000);
    expect(probe).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(4999);
    expect(probe).toHaveBeenCalledTimes(1);
    answer = true;
    await vi.advanceTimersByTimeAsync(1);
    expect(probe).toHaveBeenCalledTimes(2);
    expect(store.isDown('a')).toBe(false);
    expect(recovered).toHaveBeenCalledTimes(1);
    expect(store.panelShown('a')).toBe(true); // "Back online" for two seconds
    await vi.advanceTimersByTimeAsync(2100);
    expect(store.panelShown('a')).toBe(false);
  });

  it('reclassifies a tailnet Host once the other tailnet Host fails too', () => {
    store.trackHost('a', { label: 'A', url: 'http://100.64.0.1', via: 'direct', probe: async () => {} });
    store.trackHost('b', { label: 'B', url: 'http://100.64.0.2', via: 'direct', probe: async () => {} });
    store.noteFail('a', { kind: 'timeout' });
    expect(store.reachOf('a').reason).toBe('unknown');
    store.noteFail('b', { kind: 'timeout' });
    expect(store.reachOf('a').reason).toBe('tailscale_off');
    expect(store.reachOf('b').reason).toBe('tailscale_off');
  });

  it('never asks again at once when a probe learns nothing', async () => {
    const probe = vi.fn(async () => {});
    store.trackHost('p', { label: 'P', url: 'http://10.0.0.9', via: 'host', probe });
    store.noteFail('p', { kind: 'network', via: 'host' });
    await vi.advanceTimersByTimeAsync(2000);
    expect(probe).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(100);
    expect(probe).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1900);
    expect(probe).toHaveBeenCalledTimes(2);
  });

  it('stops asking a Host that is no longer read', async () => {
    const probe = vi.fn(async () => {});
    store.trackHost('a', { label: 'A', url: 'http://10.0.0.1', via: 'direct', probe });
    store.noteFail('a', { kind: 'refused' });
    store.untrackOthers();
    await vi.advanceTimersByTimeAsync(120000);
    expect(probe).not.toHaveBeenCalled();
    expect(store.reachOf('a')).toBeNull();
    expect(store.reachOf(store.PRIMARY)).not.toBeNull();
  });

  it('keeps auth_expired when the Host\'s lockout answers 429', () => {
    store.trackHost('a', { label: 'A', url: 'http://10.0.0.1', via: 'direct', probe: async () => {} });
    store.noteFail('a', { kind: 'http', status: 401 });
    store.noteFail('a', { kind: 'http', status: 429 });
    expect(store.reachOf('a').reason).toBe('auth_expired');
  });

  it('shows no Host panel while the device is offline', () => {
    store.trackHost('a', { label: 'A', url: 'http://10.0.0.1', via: 'direct', probe: async () => {} });
    store.noteFail('a', { kind: 'refused' });
    store.reach.deviceOnline = false;
    expect(store.isDown('a')).toBe(false);
    expect(store.panelShown('a')).toBe(false);
  });

  it('keeps since across a reload', async () => {
    store.trackHost('a', { label: 'A', url: 'http://10.0.0.1', key: 'srv-a', via: 'direct', probe: async () => {} });
    store.noteFail('a', { kind: 'refused' });
    const since = store.reachOf('a').downSince;
    vi.resetModules();
    vi.setSystemTime(Date.now() + 60_000);
    const again = await import('../../web/src/stores/reach.svelte.js');
    again.trackHost('a', { label: 'A', url: 'http://10.0.0.1', key: 'srv-a', via: 'direct', probe: async () => {} });
    again.noteFail('a', { kind: 'refused' });
    expect(again.reachOf('a').downSince).toBe(since);
  });
});
