// @ts-check
import { test, expect } from '@playwright/test';

const PIXEL = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
  'base64');

test('restores headerless avatar auth and falls back cleanly on image failure', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('claude-pwa.auth-token', 'remembered-token');
  });

  let avatarCookie = '';
  await page.route('**/avatars/agent-1?*', route => {
    avatarCookie = route.request().headers().cookie || '';
    return route.fulfill({ status: 200, contentType: 'image/png', body: PIXEL });
  });
  await page.route('**/avatars/agent-2?*', route => route.fulfill({ status: 404, body: 'missing' }));
  await page.route('**/agents/snapshot', route => route.fulfill({ json: {
    focus: 'agent-1', roster: ['Nova', 'Atlas'], personas: [],
    agents: [
      { agent_id: 'agent-1', persona: 'Nova', name: 'Nova', session: 'nova-one',
        alive: true, focused: true, avatar_url: '/avatars/agent-1?v=one' },
      { agent_id: 'agent-2', persona: 'Atlas', name: 'Atlas', session: 'atlas-two',
        alive: true, focused: false, avatar_url: '/avatars/agent-2?v=one' },
    ],
  }}));
  await page.route('**/sessions', route => route.fulfill({
    json: { sessions: ['nova-one', 'atlas-two'], default: 'nova-one' },
  }));
  await page.route('**/select', route => route.fulfill({ json: { ok: true } }));
  await page.route('**/clog', route => route.fulfill({ json: { ok: true } }));
  await page.route('**/log?*', route => route.fulfill({
    json: { turns: [], latest_revision: 0 },
  }));
  await page.route('**/events*', route => route.fulfill({
    status: 200, contentType: 'text/event-stream', body: '',
  }));

  await page.goto('/', { waitUntil: 'domcontentloaded' });

  const nova = page.locator('.side-row').filter({ hasText: 'Nova' });
  await expect(nova.locator('img')).toBeVisible();
  await expect.poll(() => avatarCookie).toContain('claude_pwa_token=remembered-token');

  const atlas = page.locator('.side-row').filter({ hasText: 'Atlas' });
  await expect(atlas.locator('.avatar-fallback')).toBeVisible();
  await expect(atlas.locator('img')).toHaveCount(0);
});
