// @ts-check
// Smoke tests for the refactored PWA running on port 7683.

import { test, expect } from '@playwright/test';

test('shell loads and the dock is present', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#mic')).toBeVisible();
  await expect(page.locator('#historyAgent')).toBeVisible();
  // Connection indicator becomes 'live' once SSE connects.
  await expect(page.locator('#conn')).toHaveClass(/live|connecting/, { timeout: 5000 });
});

test('holding the agent chip shows the overview with the full roster', async ({ page, request }) => {
  const snapshot = await (await request.get('/agents/snapshot')).json();
  await page.goto('/');
  const opener = page.locator('#historyAgent');
  await opener.dispatchEvent('pointerdown');
  await page.waitForTimeout(650);
  await opener.dispatchEvent('pointerup');
  const overview = page.locator('#overview');
  await expect(overview).not.toHaveClass(/hidden/);
  const archivedNames = new Set(snapshot.agents
    .filter(agent => agent.archived_at != null)
    .map(agent => agent.persona));
  for (const name of snapshot.roster.filter(name => !archivedNames.has(name))) {
    await expect(overview.locator(`[data-name="${name}"]`).first()).toBeAttached();
  }
  if (archivedNames.size) {
    await overview.getByRole('button', { name: /Archive/ }).click();
    for (const name of archivedNames) {
      await expect(overview.getByText(name, { exact: true }).first()).toBeAttached();
    }
  }
});

test('state machine starts in idle and accepts a recording transition', async ({ page }) => {
  // domcontentloaded — networkidle never fires because of the SSE stream.
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(500);
  const state = await page.evaluate(async () => {
    const sm = await import('/static/lib/state-machine.js');
    const m = sm.createStateMachine({ awaitDeadlineMs: 1000 });
    m.startRecording('claude');
    m.endRecording();
    m.send('claude');
    return { state: m.state, expecting: m.expectingFrom };
  });
  expect(state.state).toBe('awaiting');
  expect(state.expecting).toBe('claude');
});

test('audio queue holds non-addressee clips while awaiting', async ({ page }) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(500);
  const played = await page.evaluate(async () => {
    const sm = await import('/static/lib/state-machine.js');
    const aq = await import('/static/lib/audio-queue.js');
    const m = sm.createStateMachine({ awaitDeadlineMs: 60000 });
    const played = [];
    const player = {
      async play(clip) {
        played.push(clip.url);
        return { premature: false, duration: 1, currentTime: 1 };
      },
    };
    const s = aq.createScheduler({ machine: m, player, currentSession: () => 'claude' });
    m.startRecording('claude'); m.endRecording(); m.send('claude');
    s.ingest({ url: 'r1', session: 'rachel', ts: 1 });
    await new Promise(r => setTimeout(r, 50));
    if (played.length) return { wrong: 'rachel played first', played };
    s.ingest({ url: 'm1', session: 'claude', ts: 2 });
    await new Promise(r => setTimeout(r, 50));
    return { played };
  });
  expect(played.played).toEqual(['m1', 'r1']);
});

test('sse skips old clips via lastAudioTs', async ({ page }) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  const r = await page.evaluate(async () => {
    const aq = await import('/static/lib/audio-queue.js');
    const sm = await import('/static/lib/state-machine.js');
    const m = sm.createStateMachine({});
    const player = { play: async () => ({ premature: false, duration: 1, currentTime: 1 }) };
    const s = aq.createScheduler({ machine: m, player, currentSession: () => '' });
    const newer = s.ingest({ url: 'a', session: '', ts: 100 });
    const older = s.ingest({ url: 'b', session: '', ts: 50 });
    return { newer, older };
  });
  expect(r.newer.accepted).toBe(true);
  expect(r.older.accepted).toBe(false);
  expect(r.older.reason).toBe('old');
});
