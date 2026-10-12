import { expect, test } from '@playwright/test';

import { collectErrors, drawnFraction, freshPage, openDataset } from './helpers';

// S7 plot (ADR 0013): "Plot against a second feature" in an embedding's settings opens a plot
// beside it whose first feature is the embedding's colour: violins, composition, scatter, density.

test('plot against a second feature: follows the embedding colour, closes with it', async ({ page }) => {
  const errors = collectErrors(page);
  await freshPage(page);
  await openDataset(page, 'example_with_layers');
  const cells = page.getByTestId('view-cell');
  await expect(cells.first()).toHaveAttribute('data-status', 'ready');
  await expect(page.getByTestId('open-plot')).toHaveCount(0);

  // unticked by default; ticked, a plot opens and the second feature is chosen here
  const companion = page.getByTestId('plot-companion').locator('input');
  await expect(companion).not.toBeChecked();
  await companion.check();
  await expect(cells).toHaveCount(2);
  const cell = cells.nth(1);
  await page.getByTestId('plot-y').click();
  await page.getByRole('option', { name: /^total_counts/ }).click();
  await expect(cell).toHaveAttribute('data-status', 'ready');

  // cell type (the embedding's colour) against total_counts: violins in the embedding's colours
  await expect(cell.getByTestId('violin')).toHaveCount(12);
  // in the legends' natural order (B cell first); the T cell violin has the legend's colour
  await expect(cell.getByTestId('violin').first()).toContainText('B cell:');
  const fill = await cell.getByTestId('violin').filter({ hasText: 'T cell:' }).locator('polygon').getAttribute('fill');
  const legend = page.getByTestId('legend');
  await expect(legend).toHaveCount(1); // the embedding's legend controls the plot's categories
  await legend.getByRole('button', { name: /cell_type/ }).click(); // collapsed in the narrower cell
  const tcell = legend.locator('.legend-row').filter({ hasText: 'T cell' }).locator('.swatch');
  expect((await tcell.evaluate((e) => getComputedStyle(e).backgroundColor)).replace(/\s/g, '')).toBe(fill);
  // a category hidden in the embedding leaves the plot
  await legend.getByRole('button', { name: /^T cell/ }).click();
  await expect(cell.getByTestId('violin')).toHaveCount(11);
  await legend.getByRole('button', { name: /^T cell/ }).click();

  // a gene: violins with the share of cells above 0
  await page.getByTestId('plot-y-gene').fill('Monocyte_m1');
  await page.getByTestId('plot-y-gene').press('Enter');
  await expect(cell.getByTestId('violins')).toContainText('100%');
  // the cells at 0 are a flat bar per group (narrow for monocytes, which all express it)
  await expect(cell.getByTestId('violin-zeros')).not.toHaveCount(0);
  await page.screenshot({ path: 'test-results/screens/plot-violins.png' });

  // two category columns: composition
  await page.getByTestId('plot-y').click();
  await page.getByRole('option', { name: /^sample/ }).click();
  await expect(cell.getByTestId('bar')).toHaveCount(12);
  // a cell type hidden in the embedding leaves the composition too
  await legend.getByRole('button', { name: /^T cell/ }).click();
  await expect(cell.getByTestId('bar')).toHaveCount(11);
  await legend.getByRole('button', { name: /^T cell/ }).click();
  await page.screenshot({ path: 'test-results/screens/plot-bars.png' });

  // colouring the embedding by a number changes the plot's first feature: a scatter
  await page.getByTestId('colour-select').click();
  await page.getByRole('option', { name: /^total_counts/ }).click();
  await page.getByTestId('plot-y-gene').fill('Monocyte_m1');
  await page.getByTestId('plot-y-gene').press('Enter');
  await expect(cell.getByTestId('scatter-axes')).toBeVisible();
  await page.waitForTimeout(500);
  expect(await drawnFraction(page, cell, { x0: 0.1, x1: 0.6 })).toBeGreaterThan(0.002);
  // the points fill the panel's width, not a narrow strip (the legend is folded in this cell)
  expect(await drawnFraction(page, cell, { x0: 0.65, x1: 0.95 })).toBeGreaterThan(0.0005);
  const box = (await cell.boundingBox())!;
  let tip: string | null = null;
  await page.screenshot({ path: 'test-results/screens/plot-scatter.png' });
  // most cells do not express the gene: a row of points along the bottom of the plotting area
  for (let gy = 0.95; gy > 0.75 && !tip; gy -= 0.01) {
    for (let gx = 0.1; gx < 0.9 && !tip; gx += 0.04) {
      await page.mouse.move(box.x + gx * box.width, box.y + box.height * gy);
      if (await page.getByTestId('hover-tip').isVisible()) tip = await page.getByTestId('hover-tip').textContent();
    }
  }
  expect(tip).toMatch(/^total_counts: .+ · Monocyte_m1: /);

  // the plot's own settings: the first feature is shown, not chosen; the same as a density map
  await cell.locator('.view-title').click();
  await expect(page.getByTestId('plot-first')).toContainText('total_counts');
  await expect(page.getByTestId('plot-x')).toHaveCount(0);
  await page.getByTestId('plot-numbers-as').click();
  await page.getByRole('option', { name: 'Density map' }).click();
  await expect(cell.getByTestId('density')).toBeVisible();
  await expect(legend.nth(1)).toContainText('cells per bin (log scale)');
  await page.screenshot({ path: 'test-results/screens/plot-density.png' });

  // unticking closes the plot; ticked again and the embedding closed, both go
  await cells.first().locator('.view-title').click();
  await companion.uncheck();
  await expect(cells).toHaveCount(1);
  await companion.check();
  await expect(cells).toHaveCount(2);
  await cells.first().getByLabel('Close view').click();
  await expect(cells).toHaveCount(0);
  expect(errors).toEqual([]);
});
