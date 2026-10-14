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
  await page.getByTestId('dataset-row').filter({ hasText: 'example_with_layers' }).click();
  await expect(page.getByTestId('details')).toBeVisible();
  await tickRow(page, 'spatial.h5ad');
  await expect(page.getByTestId('linked-open')).toContainText('1 dataset ticked');
  await expect(page.getByTestId('details')).toHaveCount(0); // ticking replaces the details
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
  // back to the UMAP in both: they show the same coordinates, so they move together
  await cells.nth(0).getByRole('button', { name: 'spatial.h5ad' }).click();
  await page.getByTestId('embedding-select').click();
  await page.getByRole('option', { name: /^X_umap\b/ }).first().click();
  await expect(cells.nth(0)).toHaveAttribute('data-status', 'ready');

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

  // nothing opens beside a linked group (owner): the way out is Close all
  await page.getByTestId('dataset-row').filter({ hasText: 'example_with_layers' }).click();
  await expect(page.getByTestId('open-blocked')).toBeVisible();
  await expect(page.getByTestId('open-dataset')).toHaveCount(0);

  // one view showing other coordinates: fitted on its own; the other keeps the user's zoom
  const zb = await z(1);
  await cells.nth(0).getByRole('button', { name: 'spatial.h5ad' }).click();
  await page.getByTestId('embedding-select').click();
  await page.getByRole('option', { name: /^X_umap_orig/ }).click();
  await expect(cells.nth(0)).toHaveAttribute('data-status', 'ready');
  await page.waitForTimeout(300);
  expect(await z(1)).toBeCloseTo(zb, 3);
  expect(await drawnFraction(page, cells.nth(0))).toBeGreaterThan(0.01);
  // different coordinates move on their own (owner, S7b demo)
  const a1 = (await cells.nth(0).boundingBox())!;
  await page.mouse.move(a1.x + a1.width / 2, a1.y + a1.height / 2);
  await page.mouse.wheel(0, -300);
  await page.waitForTimeout(300);
  expect(await z(1)).toBeCloseTo(zb, 3);
  // back to the same embedding: the same scale as the other again, and linked
  await page.getByTestId('embedding-select').click();
  await page.getByRole('option', { name: /^X_umap\b/ }).first().click();
  await expect(cells.nth(0)).toHaveAttribute('data-status', 'ready');
  await expect.poll(async () => (await z(0)) - (await z(1))).toBeCloseTo(0, 2);
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, -200);
  await expect.poll(async () => (await z(0)) - (await z(1))).toBeCloseTo(0, 2);

  // removing one leaves an independent view
  await cells.nth(1).getByRole('button', { name: 'Remove from the linked views' }).click();
  await expect(page.getByTestId('group-tile')).toHaveCount(0);
  await expect(cells).toHaveCount(1);
  expect(errors).toEqual([]);
});

test('shift-click ticks every dataset between two clicks of the same list', async ({ page }) => {
  await freshPage(page);
  // the search results are one list (the example folders hold two datasets at most)
  await page.getByLabel('Search datasets').fill('h5ad');
  const list = page.getByRole('tree', { name: 'Search results' });
  const ticks = list.getByTestId('dataset-tick');
  await expect(ticks).toHaveCount(3);
  const n = 3;
  await ticks.nth(0).click();
  await ticks.nth(n - 1).click({ modifiers: ['Shift'] });
  for (let k = 0; k < n; k += 1) await expect(ticks.nth(k)).toBeChecked();
  await expect(page.getByTestId('linked-open')).toContainText(`${n} datasets ticked`);
  // a shift-click from an unticked one unticks the range
  await ticks.nth(n - 1).click();
  await ticks.nth(1).click({ modifiers: ['Shift'] });
  await expect(ticks.nth(0)).toBeChecked();
  for (let k = 1; k < n; k += 1) await expect(ticks.nth(k)).not.toBeChecked();
});

test('a dataset without the shared column shows its cells as NA', async ({ page }) => {
  const errors = collectErrors(page);
  await freshPage(page);
  // with a view open, linked opening waits for Close all (owner)
  await page.getByTestId('dataset-row').filter({ hasText: 'example_with_layers' }).click();
  await page.getByTestId('open-dataset').click();
  await tickRow(page, 'spatial.h5ad');
  await tickRow(page, 'dissociated.h5ad');
  await tickRow(page, 'example_with_layers');
  await expect(page.getByTestId('linked-blocked')).toBeVisible();
  await page.getByTestId('linked-blocked').getByRole('button', { name: 'Close all' }).click();
  await page.getByTestId('open-linked').click();
  const cells = page.getByTestId('view-cell');
  await expect(cells).toHaveCount(3);
  for (const k of [0, 1, 2]) await expect(cells.nth(k)).toHaveAttribute('data-status', 'ready');
  // coloured by the column most of them have (B-M5); the third lacks it: NA, said in the legend
  await expect(page.getByTestId('legend')).toContainText('celltype_harmonized');
  await expect(cells.nth(2).getByTestId('view-caption')).toContainText('NA');
  await expect(page.getByTestId('legend-na')).toContainText('8,000 cells (1 dataset');
  await expect(page.getByTestId('legend')).toHaveCount(1);
  await page.waitForTimeout(300);
  expect(await drawnFraction(page, cells.nth(2))).toBeGreaterThan(0.01);
  expect(errors).toEqual([]);
});

// Linked spatial views (two MERSCOPE regions) are tested at the end of browse.spec.ts: they
// convert region_3, which a test there needs unconverted.
