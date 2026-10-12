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
  // it names the cell under the pointer: the pixel there has that category's colour (the legend
  // swatch nearest to it, after the points' opacity over the background)
  const label = (await tip.textContent())!.split(': ').slice(1).join(': ').trim();
  const shot = await page.screenshot({ clip: { x: at!.x, y: at!.y, width: 1, height: 1 }, scale: 'css' });
  const px = await page.evaluate(async (b64) => {
    const img = new Image();
    img.src = `data:image/png;base64,${b64}`;
    await img.decode();
    const c = document.createElement('canvas');
    c.width = c.height = 1;
    const g = c.getContext('2d')!;
    g.drawImage(img, 0, 0);
    return Array.from(g.getImageData(0, 0, 1, 1).data.slice(0, 3));
  }, shot.toString('base64'));
  const swatches = await page.evaluate(() =>
    Array.from(document.querySelectorAll('.legend-row')).map((row) => ({
      name: row.querySelector('.legend-name')?.textContent ?? '',
      rgb: (getComputedStyle(row.querySelector('.swatch')!).backgroundColor.match(/\d+/g) ?? []).slice(0, 3).map(Number),
    })),
  );
  const bg = await page.evaluate(() =>
    (getComputedStyle(document.querySelector('.canvas')!).backgroundColor.match(/\d+/g) ?? ['255', '255', '255']).slice(0, 3).map(Number),
  );
  const blend = (c: number[]) => c.map((v, i) => 0.85 * v + 0.15 * bg[i]);
  const dist = (a: number[], b: number[]) => a.reduce((s, v, i) => s + (v - b[i]) ** 2, 0);
  const nearest = swatches.reduce((best, s) => (dist(blend(s.rgb), px) < dist(blend(best.rgb), px) ? s : best));
  expect(nearest.name).toBe(label);
  // leaving the canvas hides it
  await page.mouse.move(2, 2);
  await expect(tip).toBeHidden();
});
