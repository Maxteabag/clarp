// Fetch uses a stored Bearer token; EventSource and media use a cookie. The
// two credentials must not silently acquire different lifetimes.
import { beforeEach, describe, expect, it, vi } from 'vitest';

const AUTH_KEY = 'claude-pwa.auth-token';
const TOKEN = 'test-device-token';

function browser({ href = 'https://clarp.test/', stored = '' } = {}) {
  const values = new Map(stored ? [[AUTH_KEY, stored]] : []);
  const cookies = [];
  vi.stubGlobal('window', { location: { href, origin: 'https://clarp.test' } });
  vi.stubGlobal('location', window.location);
  vi.stubGlobal('localStorage', {
    getItem: key => values.get(key) || null,
    setItem: (key, value) => values.set(key, String(value)),
  });
  vi.stubGlobal('document', {
    get cookie() { return cookies.join('; '); },
    set cookie(value) { cookies.push(String(value)); },
  });
  vi.stubGlobal('history', { replaceState() {} });
  return cookies;
}

async function net() {
  vi.resetModules();
  return import('../../web/src/lib/net.js');
}

beforeEach(() => vi.unstubAllGlobals());

describe('headerless authentication continuity', () => {
  it('re-seeds the EventSource cookie from durable storage on every load', async () => {
    const cookies = browser({ stored: TOKEN });
    const module = await net();

    module.bootstrapAuth();

    expect(cookies.join('; ')).toContain(`claude_pwa_token=${TOKEN}`);
  });

});
