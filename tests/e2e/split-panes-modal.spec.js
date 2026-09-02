// @ts-check
import { test, expect } from '@playwright/test';

const SNAPSHOT = {
  focus: 'agent-1',
  roster: ['Nova', 'Atlas', 'Diego'],
  available_mcp_servers: [],
  personas: [
    { id: 'persona-nova', name: 'Nova', personality: 'Systems researcher.', builtin: false, avatar_url: '' },
    { id: 'persona-atlas', name: 'Atlas', personality: 'Task planner.', builtin: false, avatar_url: '' },
    { id: 'persona-diego', name: 'Diego', personality: 'Code reviewer.', builtin: false, avatar_url: '' },
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

test.describe('Modal Keyboard & Split Panes E2E', () => {
  test('navigates, splits vertically and horizontally, switches modes, and closes panes via single keys', async ({ page }) => {
    await page.route('**/agents/snapshot', route => route.fulfill({ json: SNAPSHOT }));
    await page.route('**/sessions', route => route.fulfill({
      json: { sessions: ['nova-one', 'atlas-two'], default: 'nova-one' },
    }));
    await page.route('**/select', route => route.fulfill({ json: { ok: true } }));
    await page.route('**/clog', route => route.fulfill({ json: { ok: true } }));
    await page.route(/\/log\?session=.*/, route => route.fulfill({
      json: {
        turns: [
          { id: 'msg-1', role: 'assistant', text: 'Ready', timestamp: '1788200000', revision: 1 },
        ],
        latest_revision: 1,
      },
    }));

    await page.goto('/?token=test', { waitUntil: 'domcontentloaded' });

    // 1. Verify initial state in pane mode with persistent shortcut bar
    const shortcutBar = page.locator('.shortcut-bar');
    await expect(shortcutBar).toBeVisible();
    await expect(shortcutBar).toContainText('PANE');
    await expect(shortcutBar).toContainText('Split');

    // Initially 1 pane leaf
    await expect(page.locator('.pane-leaf')).toHaveCount(1);

    // 2. Press 'v' to split vertically (side-by-side)
    await page.keyboard.press('v');
    await expect(page.locator('.pane-leaf')).toHaveCount(2);

    // 3. Press 's' to split horizontally (stacked)
    await page.keyboard.press('s');
    await expect(page.locator('.pane-leaf')).toHaveCount(3);

    // 4. Press 'h' to navigate focus to the left pane
    await page.keyboard.press('h');

    // 5. Press 'i' to enter INSERT mode
    await page.keyboard.press('i');
    await expect(shortcutBar).toContainText('INSERT');
    await expect(page.locator('#chatInput')).toBeFocused();

    // 6. Press 'Escape' to exit INSERT mode back to pane mode
    await page.keyboard.press('Escape');
    await expect(shortcutBar).toContainText('PANE');

    // 7. Press 'x' to close active pane
    await page.keyboard.press('x');
    await expect(page.locator('.pane-leaf')).toHaveCount(2);

    // 8. Press 'z' to toggle zoom on active pane
    await page.keyboard.press('z');
    await expect(page.locator('.pane-leaf')).toHaveCount(1);

    // Press 'z' again to unzoom
    await page.keyboard.press('z');
    await expect(page.locator('.pane-leaf')).toHaveCount(2);
  });
});
