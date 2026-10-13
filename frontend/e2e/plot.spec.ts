import { expect, test } from '@playwright/test';

import { collectErrors, colourShare, drawnFraction, freshPage, openDataset } from './helpers';

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
  // the plot takes the bottom third of the embedding's tile: the embedding keeps its width
  const [eb, pb] = [(await cells.first().boundingBox())!, (await cell.boundingBox())!];
  expect(Math.abs(eb.width - pb.width)).toBeLessThan(3);
  expect(pb.y).toBeGreaterThan(eb.y + eb.height - 4);
  expect(pb.height).toBeLessThan(eb.height);
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
  // panned left and down, points beyond the axes are covered (not drawn over the tick labels)
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.2, box.y + box.height * 0.8, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(300);
  await page.screenshot({ path: 'test-results/screens/plot-scatter-panned.png' });
  const pink = await legend
    .nth(1)
    .locator('.legend-row')
    .filter({ hasText: 'Monocyte' })
    .locator('.swatch')
    .evaluate((e) => getComputedStyle(e).backgroundColor.match(/\d+/g)!.slice(0, 3).map(Number) as [number, number, number]);
  const margin = 48 / box.width; // the y axis' room
  expect(await colourShare(page, cell, pink, 40, { x0: margin + 0.01, x1: margin + 0.2 })).toBeGreaterThan(0); // up to the axis
  expect(await colourShare(page, cell, pink, 40, { x0: 0, x1: margin - 0.005 })).toBe(0); // none beyond it
  await cell.getByLabel('Fit to data').click();

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

// S7c: a lasso drawn in the view selects cells; "Plot against: Lasso selection" compares them
// with the other cells (composition for a category colour, violins for a number or gene).
test('lasso: select cells, plot them against the others', async ({ page }) => {
  const errors = collectErrors(page);
  await freshPage(page);
  await openDataset(page, 'example_with_layers');
  const cells = page.getByTestId('view-cell');
  const view = cells.first();
  await page.getByTestId('plot-companion').locator('input').check();
  await expect(cells).toHaveCount(2);
  const plot = cells.nth(1);
  await page.getByTestId('plot-y').click();
  await page.getByRole('option', { name: /^Lasso selection/ }).click();
  // nothing selected yet: a quiet instruction
  await expect(plot).toHaveAttribute('data-status', 'idle');
  await expect(plot.getByTestId('view-idle')).toContainText('lasso');

  // draw a lasso around the middle of the view
  const box = (await view.boundingBox())!;
  const [cx, cy, r] = [box.x + box.width / 2, box.y + box.height / 2, Math.min(box.width, box.height) / 4];
  const lassoAround = async (x: number, y: number, shift = false) => {
    await view.getByTestId('lasso').click();
    await expect(page.getByTestId('lasso-overlay')).toBeVisible();
    if (shift) await page.keyboard.down('Shift');
    await page.mouse.move(x + r, y);
    await page.mouse.down();
    for (let k = 1; k <= 24; k += 1) await page.mouse.move(x + r * Math.cos((k * Math.PI) / 12), y + r * Math.sin((k * Math.PI) / 12));
    await page.mouse.up();
    if (shift) await page.keyboard.up('Shift');
    await expect(page.getByTestId('lasso-overlay')).toHaveCount(0);
  };
  await lassoAround(cx, cy);
  const chip = view.getByTestId('selection-chip');
  await expect(chip).toContainText('selected');
  const count = async () => Number(((await chip.textContent()) ?? '').replace(/[^0-9]/g, ''));
  const first = await count();
  expect(first).toBeGreaterThan(0);
  expect(first).toBeLessThan(8000);

  // the view's colour (cell type) against the selection: two bars, the selected and the rest
  await expect(plot).toHaveAttribute('data-status', 'ready');
  await expect(plot.getByTestId('bar')).toHaveCount(2);
  await expect(plot.getByTestId('view-caption')).toContainText('selected of 8.0 k cells');
  await page.screenshot({ path: 'test-results/screens/lasso-bars.png' });

  // Shift adds a second lasso elsewhere
  await lassoAround(cx - r * 1.2, cy - r * 1.2, true);
  await expect.poll(count).toBeGreaterThan(first);

  // a gene colour: violins of the selected cells and the others
  await page.getByTestId('gene-search').fill('Monocyte_m1');
  await page.getByTestId('gene-search').press('Enter');
  await expect(plot.getByTestId('violin')).toHaveCount(2);
  await expect(plot.getByTestId('violin').first()).toContainText('in the selection');

  // cleared: the plot waits for a selection again
  await chip.getByRole('button', { name: 'Clear the selection' }).click();
  await expect(chip).toHaveCount(0);
  await expect(plot).toHaveAttribute('data-status', 'idle');
  expect(errors).toEqual([]);
});
