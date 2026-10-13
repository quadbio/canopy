import { expect, type Page, test } from '@playwright/test';

import { collectErrors, drawnFraction, freshPage } from './helpers';

// S7b: datasets ticked together open as linked views: one tile, one legend, shared settings
// (the embedding per dataset), linked pan and zoom, NA where a dataset lacks the column.

async function tickRow(page: Page, name: string) {
  const row = page.getByTestId('dataset-row').filter({ hasText: name }).first();
  await row.locator('..').getByTestId('dataset-tick').check();
}

test('two datasets opened linked share settings, legend, zoom; the embedding stays per dataset', async ({ page }) => {
  const errors = collectErrors(page);
  await freshPage(page);
  await tickRow(page, 'spatial.h5ad');
  await expect(page.getByTestId('linked-open')).toContainText('1 dataset ticked');
  await expect(page.getByTestId('open-linked')).toBeDisabled();
  await tickRow(page, 'dissociated.h5ad');
  await page.getByTestId('open-linked').click();

  await expect(page.getByTestId('group-tile')).toHaveCount(1);
  await expect(page.getByTestId('group-tile')).toContainText('Linked · 2 datasets');
  const cells = page.getByTestId('view-cell');
  await expect(cells).toHaveCount(2);
  for (const k of [0, 1]) await expect(cells.nth(k)).toHaveAttribute('data-status', 'ready');
  await expect(page.getByTestId('dataset-tick').first()).not.toBeChecked(); // ticks cleared

  // one legend: the column both datasets have, their categories together (29), their own
  // colours (they agree), counts added up
  const legend = page.getByTestId('legend');
  await expect(legend).toHaveCount(1);
  await expect(legend).toContainText('celltype_harmonized');
  await expect(legend.locator('.legend-row')).toHaveCount(29);
  await expect(legend.locator('.legend-row').filter({ hasText: 'Allantois' })).toContainText('1,358');
  await expect(page.getByTestId('colours-regenerated')).toHaveCount(0);
  await page.waitForTimeout(300);
  for (const k of [0, 1]) expect(await drawnFraction(page, cells.nth(k))).toBeGreaterThan(0.01);
  await page.screenshot({ path: 'test-results/screens/linked-two.png' });

  // the settings say they are shared; a change applies to both
  await cells.nth(1).getByRole('button', { name: 'dissociated.h5ad' }).click();
  await expect(page.getByTestId('linked-note')).toContainText('Linked with 1 other dataset');
  const size = page.getByRole('slider', { name: 'Point size' });
  await size.focus();
  await page.keyboard.press('ArrowRight');
  const v1 = await size.getAttribute('aria-valuenow');
  await cells.nth(0).getByRole('button', { name: 'spatial.h5ad' }).click();
  await expect(page.getByRole('slider', { name: 'Point size' })).toHaveAttribute('aria-valuenow', v1!);

  // hiding a category in the legend hides it in both
  await legend.locator('.legend-row').filter({ hasText: 'Allantois' }).locator('.legend-item').click();
  await expect(legend.locator('.legend-row.off')).toHaveCount(1);

  // the embedding is chosen per dataset
  await page.getByTestId('embedding-select').click();
  await page.getByRole('option', { name: /^spatial/ }).click();
  await expect(cells.nth(0)).toHaveAttribute('data-status', 'ready');
  await cells.nth(1).getByRole('button', { name: 'dissociated.h5ad' }).click();
  await expect(page.getByTestId('embedding-select')).toContainText('X_umap');

  // zooming one view zooms the other
  await page.getByTestId('group-tile').getByRole('button', { name: 'Fit to data' }).click();
  await page.waitForTimeout(300);
  const z = async (k: number) => Number(await cells.nth(k).getAttribute('data-zoom'));
  const [a0, b0] = [await z(0), await z(1)];
  const box = (await cells.nth(1).boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, -400);
  await expect.poll(() => z(1)).toBeGreaterThan(b0 + 0.5);
  await expect.poll(async () => (await z(0)) - a0).toBeCloseTo((await z(1)) - b0, 1);

  // removing one leaves an independent view
  await cells.nth(1).getByRole('button', { name: 'Remove from the linked views' }).click();
  await expect(page.getByTestId('group-tile')).toHaveCount(0);
  await expect(cells).toHaveCount(1);
  expect(errors).toEqual([]);
});

test('a dataset without the shared column shows its cells as NA', async ({ page }) => {
  const errors = collectErrors(page);
  await freshPage(page);
  await tickRow(page, 'spatial.h5ad');
  await tickRow(page, 'example_with_layers');
  await page.getByTestId('open-linked').click();
  const cells = page.getByTestId('view-cell');
  await expect(cells).toHaveCount(2);
  for (const k of [0, 1]) await expect(cells.nth(k)).toHaveAttribute('data-status', 'ready');
  // no column in common: the first dataset's colouring, missing in the second
  await expect(cells.nth(1).getByTestId('view-caption')).toContainText('NA');
  await expect(page.getByTestId('legend')).toHaveCount(1);
  await page.waitForTimeout(300);
  expect(await drawnFraction(page, cells.nth(1))).toBeGreaterThan(0.01);
  expect(errors).toEqual([]);
});

// Linked spatial views (two MERSCOPE regions) are tested at the end of browse.spec.ts: they
// convert region_3, which a test there needs unconverted.
