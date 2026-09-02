// @ts-check
import { test, expect } from '@playwright/test';

const SNAPSHOT = {
  focus: 'agent-1',
  roster: ['Nova', 'Atlas'],
  available_mcp_servers: ['github'],
  personas: [
    { id: 'persona-nova', name: 'Nova', personality: 'Systems researcher.', builtin: false, avatar_url: '' },
    { id: 'persona-atlas', name: 'Atlas', personality: 'Task planner.', builtin: false, avatar_url: '' },
  ],
  agents: [
    {
      agent_id: 'agent-1', persona: 'Nova', session: 'nova-one', cwd: '/work/nova',
      backend: 'codex', model: 'gpt-5.6', effort: 'high', busy: false, focused: true,
      alive: true, latest_state: 'done', last_activity: 1_788_000_000_000,
      last_message: 'Nova transcript ready.',
      activity: null, mcp_servers: ['github'], heartbeat_enabled: false, dreaming_enabled: false,
      muted: false, queued_turn_count: 0, context_tokens: 0, context_window: 1000000,
      avatar_url: '', team_ids: [], archived_at: null,
    },
    {
      agent_id: 'agent-2', persona: 'Atlas', session: 'atlas-two', cwd: '/work/atlas',
      backend: 'claude', model: 'opus', effort: '', busy: false, focused: false,
      alive: true, latest_state: 'done', last_activity: 1_788_000_000_100,
      last_message: 'Atlas transcript ready.',
      activity: null, mcp_servers: [], heartbeat_enabled: false, dreaming_enabled: false,
      muted: false, queued_turn_count: 0, context_tokens: 0, context_window: 1000000,
      avatar_url: '', team_ids: [], archived_at: null,
    },
  ],
};

test.describe('Agent switching & transcript loading', () => {
  test('switches agents cleanly without stuck loading state', async ({ page }) => {
    await page.route('**/agents/snapshot', route => route.fulfill({ json: SNAPSHOT }));
    await page.route('**/sessions', route => route.fulfill({
      json: { sessions: ['nova-one', 'atlas-two'], default: 'nova-one' },
    }));
    await page.route('**/select', route => route.fulfill({ json: { ok: true } }));
    await page.route('**/clog', route => route.fulfill({ json: { ok: true } }));
    await page.route(/\/log\?session=nova-one/, route => route.fulfill({
      json: {
        turns: [
          { id: 'turn-nova-1', role: 'user', text: 'Hello Nova', timestamp: '1788200000', revision: 1 },
          { id: 'turn-nova-2', role: 'assistant', text: 'Nova transcript content', timestamp: '1788200010', revision: 2 },
        ],
        latest_revision: 2,
      },
    }));
    await page.route(/\/log\?session=atlas-two/, route => route.fulfill({
      json: {
        turns: [
          { id: 'turn-atlas-1', role: 'user', text: 'Hello Atlas', timestamp: '1788200050', revision: 1 },
          { id: 'turn-atlas-2', role: 'assistant', text: 'Atlas transcript content', timestamp: '1788200060', revision: 2 },
        ],
        latest_revision: 2,
      },
    }));

    await page.goto('/?token=test', { waitUntil: 'domcontentloaded' });

    // Wait for initial transcript to load Nova's content
    await expect(page.locator('#historyBody')).toContainText('Nova transcript content', { timeout: 5000 });
    await expect(page.locator('#historyBody')).not.toContainText('loading…');

    // Click Atlas in the sidebar
    const atlasBtn = page.locator('.side-row').filter({ hasText: 'Atlas' }).first();
    await expect(atlasBtn).toBeVisible();
    await atlasBtn.click();

    // Verify Atlas transcript loads without getting stuck
    await expect(page.locator('#historyBody')).toContainText('Atlas transcript content', { timeout: 5000 });
    await expect(page.locator('#historyBody')).not.toContainText('loading…');

    // Switch back to Nova (should be instantaneous from memory cache)
    const novaBtn = page.locator('.side-row').filter({ hasText: 'Nova' }).first();
    await novaBtn.click();
    await expect(page.locator('#historyBody')).toContainText('Nova transcript content');
    await expect(page.locator('#historyBody')).not.toContainText('loading…');
  });

  test('handles failed /log request gracefully with error placeholder', async ({ page }) => {
    await page.route('**/agents/snapshot', route => route.fulfill({ json: SNAPSHOT }));
    await page.route('**/sessions', route => route.fulfill({
      json: { sessions: ['nova-one', 'atlas-two'], default: 'nova-one' },
    }));
    await page.route('**/select', route => route.fulfill({ json: { ok: true } }));
    await page.route('**/clog', route => route.fulfill({ json: { ok: true } }));
    await page.route(/\/log\?session=nova-one/, route => route.fulfill({
      json: {
        turns: [
          { id: 'turn-nova-1', role: 'user', text: 'Hello Nova', timestamp: '1788200000', revision: 1 },
          { id: 'turn-nova-2', role: 'assistant', text: 'Nova transcript content', timestamp: '1788200010', revision: 2 },
        ],
        latest_revision: 2,
      },
    }));
    await page.route(/\/log\?session=atlas-two/, route => route.abort('failed'));

    await page.goto('/?token=test', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('#historyBody')).toContainText('Nova transcript content');

    // Click Atlas, which fails to fetch
    const atlasBtn = page.locator('.side-row').filter({ hasText: 'Atlas' }).first();
    await atlasBtn.click();

    // Verify it shows an error placeholder rather than staying stuck on loading…
    await expect(page.locator('#historyBody')).toContainText('error:');
    await expect(page.locator('#historyBody')).not.toContainText('loading…');
  });
});
