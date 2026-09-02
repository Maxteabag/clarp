// @ts-check
import { test, expect } from '@playwright/test';

const SNAPSHOT = {
  focus: 'agent-1',
  roster: ['Nova'],
  available_mcp_servers: [],
  personas: [
    { id: 'persona-nova', name: 'Nova', personality: 'Systems researcher.', builtin: false, avatar_url: '' },
  ],
  agents: [
    {
      agent_id: 'agent-1', persona: 'Nova', session: 'nova-one', cwd: '/work/nova',
      backend: 'codex', model: 'gpt-5.6', effort: 'high', busy: false, focused: true,
      alive: true, latest_state: 'done', last_activity: 1_788_000_000_000,
      last_message: 'Nova transcript ready.',
      activity: null, mcp_servers: [], heartbeat_enabled: false, dreaming_enabled: false,
      muted: false, queued_turn_count: 0, context_tokens: 0, context_window: 1000000,
      avatar_url: '', team_ids: [], archived_at: null,
    },
  ],
};

test.describe('User chat message immediate visibility & styling', () => {
  test('typing and sending a message instantly renders the user turn with readable max-width', async ({ page }) => {
    await page.route('**/agents/snapshot', route => route.fulfill({ json: SNAPSHOT }));
    await page.route('**/sessions', route => route.fulfill({
      json: { sessions: ['nova-one'], default: 'nova-one' },
    }));
    await page.route('**/select', route => route.fulfill({ json: { ok: true } }));
    await page.route('**/clog', route => route.fulfill({ json: { ok: true } }));
    await page.route(/\/log\?session=nova-one/, route => route.fulfill({
      json: {
        turns: [
          { id: 'msg-init', role: 'assistant', text: 'Hello, how can I help you?', timestamp: '1788200000', revision: 1 },
        ],
        latest_revision: 1,
      },
    }));
    await page.route('**/send', route => route.fulfill({
      json: { ok: true, session: 'nova-one' },
    }));

    await page.goto('/?token=test', { waitUntil: 'domcontentloaded' });

    // Initial assistant greeting
    await expect(page.locator('#historyBody')).toContainText('Hello, how can I help you?');

    // Type a user message and hit Enter
    const chatInput = page.locator('#chatInput');
    await expect(chatInput).toBeVisible();
    await chatInput.fill('Please deploy the latest release');
    await chatInput.press('Enter');

    // User message MUST appear immediately in the DOM with role "user"
    const userTurn = page.locator('#historyBody .turn.user').filter({ hasText: 'Please deploy the latest release' });
    await expect(userTurn).toBeVisible();

    // Verify readable max-width styling
    const maxWidth = await userTurn.evaluate(el => window.getComputedStyle(el).maxWidth);
    expect(maxWidth).not.toBe('none');
  });
});
