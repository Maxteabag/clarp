import { cloudflareTest } from '@cloudflare/vitest-plugin';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [cloudflareTest({
    wrangler: { configPath: './test/wrangler.toml' },
    miniflare: { bindings: { RELAY_SECRET: 'test-only-relay-secret' } },
  })],
  test: { testTimeout: 5000 },
});
