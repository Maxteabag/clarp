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
    await page.route('**/events*', route => route.fulfill({
      status: 200, contentType: 'text/event-stream', body: '',
    }));
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

    // Split the left pane too, producing a balanced 2x2 grid. A compatibility
    // Ctrl+Alt chord must move to the pane actually to the right, not merely
    // the next leaf in tree order.
    await page.keyboard.press('s');
    await expect(page.locator('.pane-leaf')).toHaveCount(4);
    const beforeMove = await page.locator('.pane-leaf.active').boundingBox();
    await page.keyboard.press('Control+Alt+ArrowRight');
    const afterMove = await page.locator('.pane-leaf.active').boundingBox();
    expect(afterMove.x).toBeGreaterThan(beforeMove.x);

    // 5. Press 'i' to enter INSERT mode
    await page.keyboard.press('i');
    await expect(shortcutBar).toContainText('INSERT');
    await expect(page.locator('#chatInput')).toBeFocused();

    // Workspace chords stay reliable while the composer owns ordinary keys.
    const composer = page.locator('#chatInput');
    await composer.fill('unsent draft');
    const draftRecipient = await page.evaluate(() => window.__CLARP__.app.session);
    const draftPaneId = await page.locator('.pane-leaf.active').getAttribute('data-pane-id');
    await composer.press('Control+Alt+ArrowLeft');
    await expect(composer).toHaveValue('');
    await expect.poll(() => page.evaluate(() => window.__CLARP__.app.session))
      .not.toBe(draftRecipient);
    await composer.press('Control+Alt+ArrowRight');
    await expect(composer).toHaveValue('unsent draft');
    await expect.poll(() => page.evaluate(() => window.__CLARP__.app.session))
      .toBe(draftRecipient);

    await composer.press('Control+Alt+z');
    await expect(page.locator('.pane-leaf')).toHaveCount(1);
    await expect(shortcutBar).toContainText('INSERT · ZOOM');
    await expect(composer).toBeFocused();
    await expect(composer).toHaveValue('unsent draft');
    await composer.press('Control+Alt+z');
    await expect(page.locator('.pane-leaf')).toHaveCount(4);

    // A deliberate pane click leaves insert mode instead of a global click
    // handler immediately stealing focus back into the composer.
    await page.locator('.pane-leaf:not(.active)').first().click({ position: { x: 120, y: 110 } });
    await expect(composer).not.toBeFocused();
    await expect(shortcutBar).toContainText('PANE');
    await page.keyboard.press('i');
    await expect(composer).toBeFocused();
    await expect(composer).toHaveValue('');
    await page.keyboard.press('Escape');
    await page.locator(`[data-pane-id="${draftPaneId}"]`).click({ position: { x: 120, y: 110 } });
    await page.keyboard.press('i');
    await expect(composer).toBeFocused();
    await expect(composer).toHaveValue('unsent draft');

    // Commands are discoverable without leaving or losing the draft.
    await composer.press('Control+k');
    await expect(page.locator('.command-palette')).toBeVisible();
    await expect(page.getByRole('option', { name: /Zoom pane/ })).toContainText('z');
    await page.keyboard.press('Escape');
    await expect(page.locator('.command-palette')).toHaveCount(0);
    await expect(composer).toBeFocused();
    await expect(composer).toHaveValue('unsent draft');
    await expect(shortcutBar).toContainText('INSERT');

    // Focus-moving commands must not restore insert mode over their target.
    await composer.press('Control+k');
    await page.getByRole('textbox', { name: 'Search commands' }).fill('Chats');
    await page.keyboard.press('Enter');
    await expect(shortcutBar).toContainText('CHATS');
    await expect(composer).not.toBeFocused();
    await page.keyboard.press('Escape');
    await expect(shortcutBar).toContainText('PANE');
    await page.keyboard.press('i');
    await expect(composer).toBeFocused();
    await expect(composer).toHaveValue('unsent draft');

    // 6. Press 'Escape' to exit INSERT mode back to pane mode
    await page.keyboard.press('Escape');
    await expect(shortcutBar).toContainText('PANE');

    // 7. Press 'x' to close active pane
    await page.keyboard.press('x');
    await expect(page.locator('.pane-leaf')).toHaveCount(3);

    // 8. Press 'z' to toggle zoom on active pane
    await page.keyboard.press('z');
    await expect(page.locator('.pane-leaf')).toHaveCount(1);

    // Press 'z' again to unzoom
    await page.keyboard.press('z');
    await expect(page.locator('.pane-leaf')).toHaveCount(3);

    // An explicit day selection remains authoritative even though desktop
    // defaults to the dark terminal palette.
    await page.evaluate(() => { document.documentElement.dataset.theme = 'day'; });
    await expect(page.locator('body')).toHaveCSS('background-color', 'rgb(225, 226, 231)');
    await expect(page.locator('.sidebar')).toHaveCSS('background-color', 'rgb(208, 213, 227)');
    await expect(page.locator('.pane-leaf.active')).toHaveCSS('background-color', 'rgb(225, 226, 231)');
  });
});
