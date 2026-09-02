// @ts-check
import { test, expect, devices } from '@playwright/test';

const SNAPSHOT = {
  focus: 'agent-1',
  roster: ['Nova', 'Atlas'],
  available_mcp_servers: ['github', 'playwright'],
  personas: [
    { id: 'persona-nova', name: 'Nova', personality: 'Careful systems researcher.', builtin: false, avatar_url: '' },
    { id: 'persona-atlas', name: 'Atlas', personality: 'Maps complex work into clear paths.', builtin: false, avatar_url: '' },
  ],
  agents: [
    {
      agent_id: 'agent-1', persona: 'Nova', session: 'nova-one', cwd: '/work/clarp',
      backend: 'codex', model: 'gpt-5.6', effort: 'high', busy: true, focused: true,
      alive: true, latest_state: 'tool', last_activity: 1_788_000_000_000,
      last_message: 'Implementing the responsive agent overview.',
      activity: { kind: 'tool', phase: 'editing', summary: 'Overview.svelte', status: 'running', ts: 1_788_000_000_000 },
      mcp_servers: ['github'], heartbeat_enabled: true, dreaming_enabled: false,
      muted: false, queued_turn_count: 2, context_tokens: 420000, context_window: 1000000,
      avatar_url: '', team_ids: [], archived_at: null,
    },
    {
      agent_id: 'agent-2', persona: 'Nova', session: 'nova-two', cwd: '/work/other',
      backend: 'claude', model: 'opus', effort: '', busy: false, focused: false,
      alive: true, latest_state: 'idle', last_activity: 1_787_000_000_000,
      last_message: 'A distinct Chat with the same Contact.', mcp_servers: [],
      heartbeat_enabled: false, dreaming_enabled: true, muted: true,
      queued_turn_count: 0, context_tokens: 0, context_window: 1000000,
      avatar_url: '', team_ids: [], archived_at: null,
    },
  ],
};

async function mockAgentAPIs(page) {
  await page.route('**/agents/snapshot', route => route.fulfill({ json: SNAPSHOT }));
  await page.route('**/sessions', route => route.fulfill({
    json: { sessions: ['nova-one', 'nova-two'], default: 'nova-one' },
  }));
  await page.route('**/select', route => route.fulfill({ json: { ok: true } }));
  await page.route('**/log?*', route => route.fulfill({
    json: { turns: [], latest_revision: 0, cwd: '/work/clarp' },
  }));
  await page.route('**/events*', route => route.fulfill({
    status: 200, contentType: 'text/event-stream', body: '',
  }));
}

async function openDesktopOverview(page) {
  const opener = page.locator('#historyAgent');
  await opener.dispatchEvent('pointerdown');
  await page.waitForTimeout(650);
  await opener.dispatchEvent('pointerup');
  await expect(page.locator('#overview')).toBeVisible();
}

test('desktop overview preserves session identity and exposes operational details', async ({ page }) => {
  await mockAgentAPIs(page);
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await openDesktopOverview(page);

  await expect(page.locator('.agent-card[data-name="Nova"]')).toHaveCount(2);
  await expect(page.locator('.agent-card[data-session="nova-one"]')).toContainText('2 queued');
  await expect(page.locator('.contact-card[data-name="Atlas"]')).toBeVisible();

  const first = page.locator('.agent-card[data-session="nova-one"]');
  await first.getByRole('button', { name: 'Details' }).click();
  await expect(first).toContainText('gpt-5.6');
  await expect(first.getByRole('button', { name: 'Heartbeat' })).toHaveAttribute('aria-pressed', 'true');

  await page.getByRole('searchbox', { name: 'Search agents' }).fill('Atlas');
  await expect(page.locator('.agent-card')).toHaveCount(0);
  await expect(page.locator('.contact-card[data-name="Atlas"]')).toBeVisible();
});

test.describe('mobile overview', () => {
  const iphone = devices['iPhone 15 Pro'];
  test.use({
    viewport: iphone.viewport,
    userAgent: iphone.userAgent,
    deviceScaleFactor: iphone.deviceScaleFactor,
    isMobile: iphone.isMobile,
    hasTouch: iphone.hasTouch,
  });

  test('defaults to Chats and keeps Contacts one tap away', async ({ page }) => {
    await mockAgentAPIs(page);
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    const opener = page.locator('#session');
    await opener.dispatchEvent('pointerdown');
    await page.waitForTimeout(650);
    await opener.dispatchEvent('pointerup');

    await expect(page.locator('.agent-card')).toHaveCount(2);
    await expect(page.locator('.contact-card')).toHaveCount(0);
    await page.getByRole('button', { name: /Contacts/ }).click();
    await expect(page.locator('.contact-card[data-name="Atlas"]')).toBeVisible();
    await expect(page.locator('.overview-shell')).toHaveJSProperty('scrollWidth', 393);
  });
});
