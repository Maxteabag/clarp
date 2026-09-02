// @ts-check
import { defineConfig, devices } from '@playwright/test';

// The browser suite drives a real server. `make e2e` starts a throwaway
// Docker node and sets CLARP_BASE_URL for you; set it yourself only to aim at
// a server you own. There is deliberately no default: an unset value once
// pointed the suite at the developer's live install.
const baseURL = process.env.CLARP_BASE_URL;
if (!baseURL) {
  throw new Error(
    'CLARP_BASE_URL is not set. Run `make e2e` (Docker) or export the URL of a '
    + 'disposable Clarp server before running Playwright.');
}

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  reporter: 'list',
  use: {
    baseURL,
    // The throwaway node requires a bearer token; send it on every request,
    // including the `request` fixture and the page's own fetches.
    extraHTTPHeaders: process.env.CLARP_E2E_TOKEN
      ? { Authorization: `Bearer ${process.env.CLARP_E2E_TOKEN}` }
      : {},
    trace: 'on-first-retry',
    // Service-worker activation can reload a page mid-assertion and make
    // unrelated UI checks flaky.
    serviceWorkers: 'block',
    // Browser tests intentionally ignore incidental console noise.
    ignoreHTTPSErrors: true,
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        launchOptions: {
          args: [
            '--use-fake-ui-for-media-stream',
            '--use-fake-device-for-media-stream',
          ],
        },
      },
    },
  ],
});
