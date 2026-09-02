// @ts-check
// Integration: the conversation surface and server API should respond with
// sensible shapes.

import { test, expect } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(800);
});

// ---------- API surface ----------

test('GET /agents/snapshot returns the agent registry', async ({ request }) => {
  const r = await request.get('/agents/snapshot');
  expect(r.status()).toBe(200);
  const data = await r.json();
  expect(typeof data).toBe('object');
  // Mike is the protected default — should always exist.
  const hasMike = data.agents.some(
    /** @param {any} v */ v => v && v.persona === 'Mike'
  );
  expect(hasMike).toBe(true);
});

test('GET /voices returns the catalogue with availability', async ({ request }) => {
  const r = await request.get('/voices?for=claude');
  expect(r.status()).toBe(200);
  const data = await r.json();
  expect(Array.isArray(data.voices)).toBe(true);
  expect(data.voices.length).toBeGreaterThan(5);
  for (const v of data.voices) {
    expect(typeof v.id).toBe('string');
    expect(typeof v.label).toBe('string');
  }
});

test('GET /status returns busy flag for the addressee', async ({ request }) => {
  const r = await request.get('/status?session=claude');
  expect(r.status()).toBe(200);
  const data = await r.json();
  expect(typeof data.busy).toBe('boolean');
  expect(data.session).toBe('claude');
});

test('GET /dirs returns directory completions for the home folder', async ({ request }) => {
  const r = await request.get('/dirs?path=' + encodeURIComponent('~/'));
  expect(r.status()).toBe(200);
  const data = await r.json();
  expect(Array.isArray(data.matches)).toBe(true);
});

test('SSE stream connects and pings', async ({ page }) => {
  // The PWA opens an EventSource('/events'). Verify the connection lights up.
  await expect(page.locator('#conn')).toHaveClass(/live/, { timeout: 7000 });
});

// ---------- UI flows ----------

test('opening the agent overview shows the agent inventory', async ({ page }) => {
  const opener = page.locator('#historyAgent');
  await opener.dispatchEvent('pointerdown');
  await page.waitForTimeout(650);
  await opener.dispatchEvent('pointerup');
  await expect(page.locator('#overview')).not.toHaveClass(/hidden/);
  // A Computer may temporarily have no live Chats, but its Contact inventory
  // remains available to start one.
  await expect(page.locator('.agent-card, .contact-card'))
    .not.toHaveCount(0);
});

test('opening the chat bar waits for an explicit input tap before focusing', async ({ page }) => {
  // Desktop keeps the composer permanently visible. The phone has an
  // explicit open/close affordance and must wait for the input tap before
  // raising the software keyboard.
  if (await page.locator('#chatBtn').count()) {
    await page.locator('#chatBtn').click();
    await expect(page.locator('#chatBar')).not.toHaveClass(/hidden/);
    await expect(page.locator('#chatInput')).not.toBeFocused();
    await page.locator('#chatInput').click();
    await expect(page.locator('#chatInput')).toBeFocused();
    await page.locator('#chatClose').click();
    await expect(page.locator('#chatBar')).toHaveClass(/hidden/);
  } else {
    await expect(page.locator('#chatBar')).toBeVisible();
    await expect(page.locator('#chatInput')).toBeVisible();
  }
});
