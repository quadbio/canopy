import { expect, test } from '@playwright/test';

import { collectErrors, drawnFraction, freshPage } from './helpers';

// S7 plot view: violins, composition, scatter and density, from the generated example.

test('plot view: violins, composition, scatter and density of two features', async ({ page }) => {
  const errors = collectErrors(page);
  await freshPage(page);
  await page.getByTestId('dataset-row').filter({ hasText: 'example_with_layers' }).click();
  await page.getByTestId('open-plot').click();
  const cell = page.getByTestId('view-cell').first();
  await expect(cell).toHaveAttribute('data-status', 'ready');

  // default: cell type against total_counts as violins, one per cell type, in the legend's colours
  await expect(cell.getByTestId('violin')).toHaveCount(12);
  const fill = await cell.getByTestId('violin').first().locator('polygon').getAttribute('fill');
  const tcell = page.getByTestId('legend').locator('.legend-row').filter({ hasText: 'T cell' }).locator('.swatch');
  expect((await tcell.evaluate((e) => getComputedStyle(e).backgroundColor)).replace(/\s/g, '')).toBe(fill);
  // a hidden category leaves the plot
  await page.getByTestId('legend').getByRole('button', { name: /^T cell/ }).click();
  await expect(cell.getByTestId('violin')).toHaveCount(11);
  await page.getByTestId('legend').getByRole('button', { name: /^T cell/ }).click();

  // a gene: violins with the share of cells above 0
  await page.getByTestId('plot-y-gene').fill('Monocyte_m1');
  await page.getByTestId('plot-y-gene').press('Enter');
  await expect(cell.getByTestId('violins')).toContainText('100%');
  await page.screenshot({ path: 'test-results/screens/plot-violins.png' });

  // two category columns: composition
  await page.getByTestId('plot-y').click();
  await page.getByRole('option', { name: /^sample/ }).click();
  await expect(cell.getByTestId('bar')).toHaveCount(12);
  await page.screenshot({ path: 'test-results/screens/plot-bars.png' });

  // two numbers: a scatter with axes, hover names both values
  await page.getByTestId('plot-x').click();
  await page.getByRole('option', { name: /^total_counts/ }).click();
  await page.getByTestId('plot-y-gene').fill('Monocyte_m1');
  await page.getByTestId('plot-y-gene').press('Enter');
  await expect(cell.getByTestId('scatter-axes')).toBeVisible();
  await page.waitForTimeout(500);
  expect(await drawnFraction(page, cell, { x0: 0.1, x1: 0.6 })).toBeGreaterThan(0.002);
  const box = (await cell.boundingBox())!;
  let tip: string | null = null;
  for (let gx = 0.1; gx < 0.6 && !tip; gx += 0.02) {
    await page.mouse.move(box.x + gx * box.width, box.y + box.height * 0.9);
    if (await page.getByTestId('hover-tip').isVisible()) tip = await page.getByTestId('hover-tip').textContent();
  }
  expect(tip).toMatch(/^total_counts: .+ · Monocyte_m1: /);
  await page.screenshot({ path: 'test-results/screens/plot-scatter.png' });

  // the same as a density map (computed on the server)
  await page.getByTestId('plot-numbers-as').click();
  await page.getByRole('option', { name: 'Density map' }).click();
  await expect(cell.getByTestId('density')).toBeVisible();
  await expect(page.getByTestId('legend')).toContainText('cells per bin');
  await page.screenshot({ path: 'test-results/screens/plot-density.png' });
  expect(errors).toEqual([]);
});
