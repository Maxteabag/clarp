// @ts-check
// Regression reproducing a bug reported on the refactor instance:
//   R1: microphone capture fails — origin isn't a secure context, so
//       navigator.mediaDevices.getUserMedia is undefined / refused.

import { test, expect } from '@playwright/test';

test.use({
  permissions: ['microphone'],
});

test('R1 — origin is a secure context so getUserMedia is usable', async ({ page }) => {
  // The real-world failure is "microphone permissions failed" because the
  // user reaches the refactor over plain HTTP at a non-localhost IP, which
  // Chrome and iOS Safari don't treat as a secure context — meaning
  // navigator.mediaDevices is undefined. Our localhost test won't reproduce
  // that since 127.0.0.1 is a localhost-exempt secure context. So we assert
  // both: the API is reachable here, AND we surface a clear failure mode for
  // future tests run against the actual external host.
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(800);
  const ctx = await page.evaluate(async () => {
    if (!window.isSecureContext) {
      return { secure: false, hasGUM: false, reason: 'not a secure context' };
    }
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
      return { secure: true, hasGUM: false, reason: 'mediaDevices missing' };
    }
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: true });
      s.getTracks().forEach(t => t.stop());
      return { secure: true, hasGUM: true, opened: true };
    } catch (e) {
      return { secure: true, hasGUM: true, opened: false, reason: String(e && e.message || e) };
    }
  });
  expect(ctx.secure, JSON.stringify(ctx)).toBe(true);
  expect(ctx.hasGUM, JSON.stringify(ctx)).toBe(true);
  expect(ctx.opened, JSON.stringify(ctx)).toBe(true);
});
