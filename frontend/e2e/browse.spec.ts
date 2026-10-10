import { expect, type Page, test } from '@playwright/test';

// The S4 demo path: find a dataset, open it, see its embedding coloured by cell type.

async function openDataset(page: Page, root: string, name: string) {
  await page.goto('/');
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await page.getByRole('button', { name: new RegExp(`${root} \\d+$`) }).waitFor();
  await page.getByTestId('dataset-row').filter({ hasText: name }).first().click();
  await expect(page.getByTestId('details')).toContainText(name);
  await page.getByTestId('open-dataset').click();
  await expect(page.getByTestId('view-cell').last()).toHaveAttribute('data-status', 'ready');
}

function collectErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
  page.on('pageerror', (e) => errors.push(e.message));
  return errors;
}

test('file browser lists the example datasets', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('dataset-row')).toHaveCount(4); // data roots start expanded
  await page.getByRole('button', { name: /extracted \d+$/ }).click(); // collapse
  await expect(page.getByTestId('dataset-row')).toHaveCount(2);
  await page.getByLabel('Search datasets').fill('region_1');
  await expect(page.getByTestId('dataset-row')).toHaveCount(1);
});

test('open spatial.h5ad and colour its UMAP by cell type', async ({ page }) => {
  const errors = collectErrors(page);
  await openDataset(page, 'examples', 'spatial.h5ad');
  await expect(page.getByTestId('legend')).toContainText('celltype');
  await expect(page.getByTestId('embedding-select')).toContainText('X_umap');
  await page.screenshot({ path: 'test-results/screens/spatial-umap.png' });
  // hide one category from the legend, then switch colouring to a numeric column
  await page.getByTestId('legend').locator('.legend-item').first().click();
  await expect(page.getByTestId('legend').locator('.legend-item.off')).toHaveCount(1);
  await page.getByTestId('colour-select').click();
  await page.getByRole('option', { name: /^total_counts/ }).click();
  await expect(page.getByTestId('legend')).toContainText('total_counts');
  await expect(page.getByTestId('legend').locator('.colourbar')).toBeVisible();
  await page.screenshot({ path: 'test-results/screens/spatial-umap-total-counts.png' });
  expect(errors).toEqual([]);
});

test('two datasets side by side in one canvas', async ({ page }) => {
  const errors = collectErrors(page);
  await openDataset(page, 'examples', 'dissociated.h5ad');
  await page.getByTestId('dataset-row').filter({ hasText: 'region_1' }).click();
  await page.getByTestId('open-dataset').click();
  await expect(page.getByTestId('view-cell')).toHaveCount(2);
  await expect(page.getByTestId('view-cell').nth(1)).toHaveAttribute('data-status', 'ready');
  expect(await page.locator('canvas').count()).toBe(1); // one WebGL context for all views
  await page.screenshot({ path: 'test-results/screens/two-views.png' });
  expect(errors).toEqual([]);
});
