import { expect, test } from '@playwright/test';

import { freshPage, openDataset } from './helpers';

// S6 feedback: on a high-density (Retina) screen the hover text was drawn off screen.
test.use({ deviceScaleFactor: 2 });

test('hover text appears next to the pointer on a high-density screen', async ({ page }) => {
  await freshPage(page);
  await openDataset(page, 'spatial.h5ad');
  const cell = page.getByTestId('view-cell').first();
  await page.waitForTimeout(800);
  const box = (await cell.boundingBox())!;
  const tip = page.getByTestId('hover-tip');
  let at: { x: number; y: number } | null = null;
  for (let gx = 0.25; gx < 0.65 && !at; gx += 0.02) {
    for (const gy of [0.4, 0.5, 0.6]) {
      const p = { x: box.x + gx * box.width, y: box.y + gy * box.height };
      await page.mouse.move(p.x, p.y);
      await page.waitForTimeout(50);
      if (await tip.isVisible()) {
        at = p;
        break;
      }
    }
  }
  expect(at).not.toBeNull();
  await expect(tip).toContainText(':');
  const r = (await tip.boundingBox())!;
  // inside the canvas, within 40 px of the pointer
  const canvas = (await page.getByTestId('canvas').boundingBox())!;
  expect(r.x).toBeGreaterThanOrEqual(canvas.x);
  expect(r.y).toBeGreaterThanOrEqual(canvas.y);
  expect(r.x + r.width).toBeLessThanOrEqual(canvas.x + canvas.width);
  expect(r.y + r.height).toBeLessThanOrEqual(canvas.y + canvas.height);
  expect(Math.abs(r.x - at!.x)).toBeLessThan(r.width + 40);
  expect(Math.abs(r.y - at!.y)).toBeLessThan(r.height + 40);
  await page.screenshot({ path: 'test-results/screens/hover-retina.png' });
  // leaving the canvas hides it
  await page.mouse.move(2, 2);
  await expect(tip).toBeHidden();
});
