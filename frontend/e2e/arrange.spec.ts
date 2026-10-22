// Arranging views (issue #1 and the owner's "open several times") and the legend search (issue #2).

import { expect, type Page, test } from '@playwright/test';

import { collectErrors, freshPage, openDataset } from './helpers';

const titles = (page: Page) => page.getByTestId('view-cell').locator('.view-title').allTextContents();

test('legend search: find categories, show only those, hide them', async ({ page }) => {
  const errors = collectErrors(page);
  await freshPage(page);
  await openDataset(page, 'example_with_layers.h5ad');
  await page.getByTestId('colour-select').click();
  await page.getByRole('option', { name: /^sample/ }).click();
  const legend = page.getByTestId('legend');
  await expect(legend).toContainText('sample');
  await legend.getByTestId('legend-search').fill('DONOR01');
  await expect(legend).toContainText('4 found');
  await expect(legend.locator('.legend-row')).toHaveCount(4);
  await legend.getByTestId('legend-only').click();
  await legend.getByTestId('legend-search').fill('');
  await expect(legend.locator('.legend-row')).toHaveCount(80);
  await expect(legend.locator('.legend-row:not(.off)')).toHaveCount(4);
  await page.screenshot({ path: 'test-results/screens/legend-search.png' });
  await legend.getByTestId('legend-search').fill('_s1');
  await legend.getByTestId('legend-hide-found').click();
  await legend.getByTestId('legend-search').press('Escape');
  await expect(legend.locator('.legend-row:not(.off)')).toHaveCount(3); // donor01_s2–s4
  expect(errors).toEqual([]);
});

test('open a dataset several times, fixed columns, move views by dragging and with the keyboard', async ({ page }) => {
  const errors = collectErrors(page);
  await freshPage(page);
  await page.getByTestId('dataset-row').filter({ hasText: 'example_with_layers.h5ad' }).first().click();
  await page.getByTestId('open-several').check();
  await page.getByTestId('open-times').fill('3');
  await page.getByTestId('open-dataset').click();
  await expect(page.getByTestId('view-cell')).toHaveCount(3);
  expect(await titles(page)).toEqual(['example_with_layers.h5ad (1)', 'example_with_layers.h5ad (2)', 'example_with_layers.h5ad (3)']);
  // more than fit: refused, nothing opened
  await page.getByTestId('open-times').fill('9');
  await page.getByTestId('open-dataset').click();
  await expect(page.getByTestId('details')).toContainText('Only 6 more views fit');
  await expect(page.getByTestId('view-cell')).toHaveCount(3);

  // three columns: the three copies in one row, the next dataset starts the second row
  await page.getByTestId('columns-select').selectOption('3');
  await openDataset(page, 'spatial.h5ad');
  const boxes = await Promise.all([0, 1, 2, 3].map(async (i) => (await page.getByTestId('view-cell').nth(i).boundingBox())!));
  expect(new Set(boxes.slice(0, 3).map((b) => Math.round(b.y))).size).toBe(1);
  expect(Math.round(boxes[3].x)).toBe(Math.round(boxes[0].x));
  expect(boxes[3].y).toBeGreaterThan(boxes[0].y + 10);

  // drag the spatial view onto the first: it takes that place, the others shift
  const grip = page.getByTestId('view-cell').nth(3).getByTestId('view-grip');
  await grip.hover();
  await page.mouse.down();
  await page.mouse.move(boxes[0].x + boxes[0].width / 2, boxes[0].y + boxes[0].height / 2, { steps: 8 });
  await expect(page.locator('.drop-target.over')).toHaveCount(1);
  await page.mouse.up();
  await expect.poll(() => titles(page)).toEqual([
    'spatial.h5ad',
    'example_with_layers.h5ad (1)',
    'example_with_layers.h5ad (2)',
    'example_with_layers.h5ad (3)',
  ]);
  await expect(page.getByTestId('drop-target')).toHaveCount(0);
  // the keyboard: Alt+→ moves it one place on
  await page.getByTestId('view-cell').nth(0).getByTestId('view-grip').focus();
  await page.keyboard.press('Alt+ArrowRight');
  await expect.poll(async () => (await titles(page))[1]).toBe('spatial.h5ad');
  await expect(page.getByTestId('view-cell').nth(1)).toHaveAttribute('data-status', 'ready');
  await page.screenshot({ path: 'test-results/screens/arranged.png' });

  // kept after a reload
  await page.reload();
  await expect(page.getByTestId('columns-select')).toHaveValue('3');
  expect((await titles(page))[1]).toBe('spatial.h5ad');
  expect(errors).toEqual([]);
});
