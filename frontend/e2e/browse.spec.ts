import { expect, type Page, test } from '@playwright/test';

import {
  collectErrors,
  colourShare,
  drawnColours,
  drawnFraction,
  freshPage,
  mainColours,
  openDataset,
  parseRgb,
} from './helpers';

// The S4 demo path: find a dataset, open it, see its embedding coloured by cell type.

test('file browser lists the example datasets', async ({ page }) => {
  await freshPage(page);
  await expect(page.getByTestId('dataset-row')).toHaveCount(4); // data roots start expanded
  await page.getByRole('treeitem', { name: /extracted \d+$/ }).click(); // collapse
  await expect(page.getByTestId('dataset-row')).toHaveCount(2);
  await page.getByLabel('Search datasets').fill('region_1');
  await expect(page.getByTestId('dataset-row')).toHaveCount(1);
});

test('dataset details explain tables and storage notes in plain words', async ({ page }) => {
  await freshPage(page);
  await page.getByTestId('dataset-row').filter({ hasText: 'region_1' }).click();
  const details = page.getByTestId('details');
  await expect(details).toContainText('one per object in boundaries_cells');
  await details.locator('select').selectOption('table_tiles');
  await expect(details).toContainText('one per object in boundaries_tiles');
  await page.getByLabel('About “Table”').click(); // a click (or tap) opens the help
  await expect(page.getByRole('dialog')).toContainText('one row per object');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const notes = page.getByTestId('dataset-notes');
  await expect(notes).toContainText('nothing to do');
  await notes.locator('summary').click();
  await expect(notes).toContainText('Image without zoom levels');
  await expect(notes).not.toContainText('single_level'); // codes are not shown
  await page.screenshot({ path: 'test-results/screens/details-notes.png' });
});

test('open spatial.h5ad and colour its UMAP by cell type', async ({ page }) => {
  const errors = collectErrors(page);
  await freshPage(page);
  await openDataset(page, 'spatial.h5ad');
  const cell = page.getByTestId('view-cell').first();
  await expect(page.getByTestId('legend')).toContainText('celltype');
  await expect(page.getByTestId('embedding-select')).toContainText('X_umap');
  expect(await drawnFraction(page, cell, { x0: 0, x1: 0.6 })).toBeGreaterThan(0.03); // points drawn
  // each point has its own colour: many cell types on screen, not one colour for all
  expect(mainColours(await drawnColours(page, cell, { x0: 0, x1: 0.6 })).length).toBeGreaterThan(8);
  await page.screenshot({ path: 'test-results/screens/spatial-umap.png' });

  await page.getByTestId('legend').locator('.legend-item').first().click();
  await expect(page.getByTestId('legend').locator('.legend-item.off')).toHaveCount(1);
  await page.getByTestId('colour-select').click();
  await page.getByRole('option', { name: /^total_counts/ }).click();
  await expect(page.getByTestId('legend')).toContainText('total_counts');
  await expect(page.getByTestId('legend').locator('.colourbar')).toBeVisible();
  await page.waitForTimeout(300);
  // a gradient: both ends of viridis are on screen
  expect(await colourShare(page, cell, [253, 231, 37], 90, { x0: 0, x1: 0.6 })).toBeGreaterThan(0.002);
  expect(await colourShare(page, cell, [68, 1, 84], 90, { x0: 0, x1: 0.6 })).toBeGreaterThan(0.002);
  await page.screenshot({ path: 'test-results/screens/spatial-umap-total-counts.png' });
  expect(errors).toEqual([]);
});

test('hidden categories are neither drawn nor reported on hover', async ({ page }) => {
  await freshPage(page);
  await openDataset(page, 'spatial.h5ad');
  const cell = page.getByTestId('view-cell').first();
  const box = (await cell.boundingBox())!;
  // find a spot with a tooltip
  let spot: { x: number; y: number } | null = null;
  for (let gx = 0.2; gx < 0.6 && !spot; gx += 0.05) {
    for (let gy = 0.3; gy < 0.9 && !spot; gy += 0.05) {
      await page.mouse.move(box.x + gx * box.width, box.y + gy * box.height);
      if (await page.locator('.deck-tooltip').isVisible({ timeout: 150 }).catch(() => false)) {
        spot = { x: box.x + gx * box.width, y: box.y + gy * box.height };
      }
    }
  }
  expect(spot).not.toBeNull();
  await page.getByTestId('legend').getByRole('button', { name: 'hide all' }).click();
  await expect(page.getByTestId('legend').locator('.legend-item.off').first()).toBeVisible();
  await page.mouse.move(spot!.x + 1, spot!.y + 1);
  await page.mouse.move(spot!.x, spot!.y);
  await page.waitForTimeout(300);
  await expect(page.locator('.deck-tooltip')).toBeHidden();
  expect(await drawnFraction(page, cell, { x0: 0, x1: 0.6 })).toBeLessThan(0.002);
});

test('a broken saved workspace does not break the app', async ({ page }) => {
  const errors = collectErrors(page);
  await page.goto('/');
  await page.evaluate(() =>
    localStorage.setItem(
      'dbx-workspace',
      JSON.stringify({
        state: {
          views: [
            { id: 'a', type: 'spatial', datasetId: 'x', table: null, title: 'future view', params: {} },
            { id: 'b', type: 'embedding', datasetId: 'nope', table: null, title: 'old', params: { embedding: 'X_umap' } },
          ],
          selected: 'a',
        },
        version: 1,
      }),
    ),
  );
  await page.reload();
  await expect(page.getByTestId('dataset-row').first()).toBeVisible(); // the app is up
  await expect(page.getByTestId('view-cell')).toHaveCount(1); // unknown type dropped
  await expect(page.getByTestId('view-cell')).toHaveAttribute('data-status', 'error'); // unknown dataset: its own error
  await page.getByRole('button', { name: 'Close all' }).click();
  await expect(page.getByTestId('view-cell')).toHaveCount(0);
  expect(errors.filter((e) => !/404|Not Found/.test(e))).toEqual([]);
});

test('two datasets side by side; zoom is kept when the second opens', async ({ page }) => {
  const errors = collectErrors(page);
  await freshPage(page);
  await openDataset(page, 'dissociated.h5ad');
  const first = page.getByTestId('view-cell').first();
  const box = (await first.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let i = 0; i < 5; i += 1) await page.mouse.wheel(0, -200);
  await page.waitForTimeout(300);
  const zoomed = Number(await first.getAttribute('data-zoom'));
  await openDataset(page, 'region_1');
  await expect(page.getByTestId('view-cell')).toHaveCount(2);
  const after = Number(await first.getAttribute('data-zoom'));
  const box2 = (await first.boundingBox())!;
  // kept: only scaled with the narrower cell, not refitted
  expect(after).toBeCloseTo(zoomed + Math.log2(Math.min(box2.width / box.width, box2.height / box.height)), 1);
  expect(await page.locator('canvas').count()).toBe(1); // one WebGL context for all views
  expect(await drawnFraction(page, first)).toBeGreaterThan(0.001); // zoomed in: fewer points on screen
  expect(await drawnFraction(page, page.getByTestId('view-cell').nth(1))).toBeGreaterThan(0.01);
  await page.screenshot({ path: 'test-results/screens/two-views.png' });
  expect(errors).toEqual([]);
});

test('four views in dark mode', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'dark' });
  await freshPage(page);
  await openDataset(page, 'spatial.h5ad');
  await openDataset(page, 'dissociated.h5ad');
  await openDataset(page, 'region_1');
  await openDataset(page, 'region_3');
  await expect(page.getByTestId('view-cell')).toHaveCount(4);
  for (const c of await page.getByTestId('view-cell').all()) expect(await drawnFraction(page, c)).toBeGreaterThan(0.01);
  await page.screenshot({ path: 'test-results/screens/four-views-dark.png' });
});

test('size and opacity changes send no point data to the GPU', async ({ page }) => {
  // Count bytes uploaded into WebGL buffers.
  await page.addInitScript(() => {
    const w = window as unknown as { __uploaded: number };
    w.__uploaded = 0;
    for (const proto of [WebGL2RenderingContext.prototype, WebGLRenderingContext.prototype]) {
      const sub = proto.bufferSubData as (...a: unknown[]) => void;
      proto.bufferSubData = function (this: unknown, ...a: unknown[]) {
        const src = a[2] as ArrayBufferView | undefined;
        w.__uploaded += src?.byteLength ?? 0;
        return sub.apply(this, a);
      } as typeof proto.bufferSubData;
      const data = proto.bufferData as (...a: unknown[]) => void;
      proto.bufferData = function (this: unknown, ...a: unknown[]) {
        const src = a[1];
        w.__uploaded += typeof src === 'number' ? 0 : ((src as ArrayBufferView | null)?.byteLength ?? 0);
        return data.apply(this, a);
      } as typeof proto.bufferData;
    }
  });
  await freshPage(page);
  await openDataset(page, 'spatial.h5ad');
  await page.waitForTimeout(500);
  const uploaded = () => page.evaluate(() => (window as unknown as { __uploaded: number }).__uploaded);
  const before = await uploaded();
  expect(before).toBeGreaterThan(400_000); // the counter works: opening uploaded the points
  const size = page.getByRole('slider', { name: 'Point size' });
  await size.focus();
  for (let i = 0; i < 5; i += 1) await page.keyboard.press('ArrowRight');
  const opacity = page.getByRole('slider', { name: 'Opacity' });
  await opacity.focus();
  for (let i = 0; i < 3; i += 1) await page.keyboard.press('ArrowLeft');
  await page.waitForTimeout(300);
  // 52 k cells: positions alone are 414 KB, colours 207 KB. Only small uniform buffers may move.
  expect((await uploaded()) - before).toBeLessThan(20_000);
});

test('reopening a dataset continues with its last settings; reset goes back to defaults', async ({ page }) => {
  await freshPage(page);
  await openDataset(page, 'spatial.h5ad');
  await page.getByTestId('colour-select').click();
  await page.getByRole('option', { name: /^total_counts/ }).click();
  await expect(page.getByTestId('legend')).toContainText('total_counts');
  await page.getByLabel('Close view').click();
  await expect(page.getByTestId('view-cell')).toHaveCount(0);

  await openDataset(page, 'spatial.h5ad');
  await expect(page.getByTestId('legend')).toContainText('total_counts');
  await page.getByTestId('reset-view').click();
  await expect(page.getByTestId('legend')).toContainText('celltype');

  // the reset is remembered too
  await page.getByLabel('Close view').click();
  await openDataset(page, 'spatial.h5ad');
  await expect(page.getByTestId('legend')).toContainText('celltype');
});

const countUploads = () => {
  const w = window as unknown as { __uploaded: number };
  w.__uploaded = 0;
  const proto = WebGL2RenderingContext.prototype;
  const sub = proto.bufferSubData as (...a: unknown[]) => void;
  proto.bufferSubData = function (this: unknown, ...a: unknown[]) {
    w.__uploaded += (a[2] as ArrayBufferView | undefined)?.byteLength ?? 0;
    return sub.apply(this, a);
  } as typeof proto.bufferSubData;
  const data = proto.bufferData as (...a: unknown[]) => void;
  proto.bufferData = function (this: unknown, ...a: unknown[]) {
    const src = a[1];
    w.__uploaded += typeof src === 'number' ? 0 : ((src as ArrayBufferView | null)?.byteLength ?? 0);
    return data.apply(this, a);
  } as typeof proto.bufferData;
};

async function legendSwatch(page: Page, view: number, label: string): Promise<string> {
  const lg = page.getByTestId('legend').nth(view);
  const toggle = lg.locator('.legend-toggle');
  if ((await toggle.getAttribute('aria-expanded')) === 'false') await toggle.click(); // small views start folded
  const item = lg.locator('.legend-item').filter({ has: page.locator('.legend-name', { hasText: new RegExp(`^${label}$`) }) });
  return item.locator('.swatch').evaluate((e) => getComputedStyle(e).backgroundColor);
}

async function colourBy(page: Page, view: number, column: string, palette?: RegExp) {
  await page.getByTestId('view-cell').nth(view).locator('.view-title').click(); // select the view
  await page.getByTestId('colour-select').click();
  await page.getByRole('option', { name: new RegExp(`^${column}`) }).click();
  if (palette) {
    await page.getByTestId('palette-select').click();
    await page.getByRole('option', { name: palette }).click();
  }
  await expect(page.getByTestId('legend').nth(view)).toContainText(column);
}

test('colours follow the label name across datasets, whatever their order in each', async ({ page }) => {
  await freshPage(page);
  await openDataset(page, 'spatial.h5ad');
  await colourBy(page, 0, 'celltype_harmonized', /^Default/);
  await openDataset(page, 'dissociated.h5ad');
  await colourBy(page, 1, 'celltype_harmonized', /^Default/);
  // "Erythroid" is the 9th category in one dataset and the 7th in the other
  for (const label of ['Erythroid', 'NMP', 'Spinal cord']) {
    expect(await legendSwatch(page, 1, label)).toBe(await legendSwatch(page, 0, label));
  }
});

test('hiding a category and changing the palette send no point data', async ({ page }) => {
  await page.addInitScript(countUploads);
  await freshPage(page);
  await openDataset(page, 'spatial.h5ad');
  await colourBy(page, 0, 'celltype_harmonized', /^Default/);
  const cell = page.getByTestId('view-cell').first();
  const uploaded = () => page.evaluate(() => (window as unknown as { __uploaded: number }).__uploaded);
  await page.waitForTimeout(300);
  const before = await uploaded();
  // hide one category: its colour leaves the plot
  const ery = parseRgb(await legendSwatch(page, 0, 'Erythroid'));
  const shareBefore = await colourShare(page, cell, ery, 30);
  expect(shareBefore).toBeGreaterThan(0.002);
  await page.getByTestId('legend').locator('.legend-item', { hasText: /^Erythroid/ }).click();
  await page.waitForTimeout(300);
  expect(await colourShare(page, cell, ery, 30)).toBeLessThan(shareBefore / 5);
  // change the palette: the colours change
  const coloursBefore = mainColours(await drawnColours(page, cell));
  await page.getByTestId('palette-select').click();
  await page.getByRole('option', { name: /^Colour-blind/ }).click();
  await page.waitForTimeout(300);
  expect(mainColours(await drawnColours(page, cell)).join()).not.toBe(coloursBefore.join());
  // 52 k cells: positions are 414 KB, values 207 KB. Only the colour table and uniforms may move.
  expect((await uploaded()) - before).toBeLessThan(10_000);
});

test('colour by a gene: search, gradient, tooltip, values from a layer', async ({ page }) => {
  const errors = collectErrors(page);
  await freshPage(page);
  await openDataset(page, 'spatial.h5ad');
  await page.waitForTimeout(300);
  const covered = await drawnFraction(page, page.getByTestId('view-cell').first(), { x0: 0, x1: 0.6 });
  const search = page.getByTestId('gene-search');
  await search.fill('lin28');
  await expect(page.getByRole('option', { name: /^Lin28a/ })).toBeVisible();
  await search.press('Enter');
  const legend = page.getByTestId('legend');
  await expect(legend).toContainText('Lin28a');
  await expect(legend.locator('.colourbar')).toBeVisible();
  await expect(page.getByTestId('colour-select')).toContainText('Lin28a');
  const cell = page.getByTestId('view-cell').first();
  await expect(cell).toHaveAttribute('data-status', 'ready');
  await page.waitForTimeout(300);
  // a gradient: both ends of viridis are on screen (a one-colour plot has neither)
  expect(await colourShare(page, cell, [253, 231, 37], 90, { x0: 0, x1: 0.6 })).toBeGreaterThan(0.005);
  expect(await colourShare(page, cell, [68, 1, 84], 90, { x0: 0, x1: 0.6 })).toBeGreaterThan(0.005);
  // every cell is still drawn (expressing cells are moved in front, not out of view)
  expect(await drawnFraction(page, cell, { x0: 0, x1: 0.6 })).toBeGreaterThan(0.85 * covered);
  await page.screenshot({ path: 'test-results/screens/spatial-gene.png' });
  // a hover shows the gene's value
  const box = (await cell.boundingBox())!;
  let tip: string | null = null;
  for (let gx = 0.25; gx < 0.6 && !tip; gx += 0.05) {
    for (let gy = 0.3; gy < 0.9 && !tip; gy += 0.05) {
      await page.mouse.move(box.x + gx * box.width, box.y + gy * box.height);
      if (await page.locator('.deck-tooltip').isVisible({ timeout: 150 }).catch(() => false)) {
        tip = await page.locator('.deck-tooltip').textContent();
      }
    }
  }
  expect(tip).toMatch(/^Lin28a: [\d.e+-]+$/);

  // a SpatialData table with layers: values from "counts"
  await openDataset(page, 'region_1', 'table_cells');
  await page.getByTestId('gene-search').fill('a');
  await page.getByTestId('gene-search').press('Enter');
  await expect(page.getByTestId('matrix-select')).toBeVisible();
  await page.getByTestId('matrix-select').click();
  await page.getByRole('option', { name: /^counts/ }).click();
  await expect(page.getByTestId('legend').nth(1)).toContainText('(counts)');
  await expect(page.getByTestId('view-cell').nth(1)).toHaveAttribute('data-status', 'ready');
  expect(errors).toEqual([]);
});

test('higher values are drawn on top, and only when asked', async ({ page }) => {
  await freshPage(page);
  await openDataset(page, 'spatial.h5ad');
  await page.getByTestId('gene-search').fill('Plek'); // expressed in 1 cell of 10
  await page.getByTestId('gene-search').press('Enter');
  await expect(page.getByTestId('legend')).toContainText('Plek');
  const cell = page.getByTestId('view-cell').first();
  await page.waitForTimeout(300);
  const top = (await colourShare(page, cell, [253, 231, 37], 90)) + (await colourShare(page, cell, [94, 201, 98], 90));
  await page.getByTestId('sort-by-value').locator('input').uncheck();
  await page.waitForTimeout(300);
  const mixed = (await colourShare(page, cell, [253, 231, 37], 90)) + (await colourShare(page, cell, [94, 201, 98], 90));
  expect(top).toBeGreaterThan(mixed * 1.2);
});

test('a slow or failing gene keeps the plot on screen; Retry recovers', async ({ page }) => {
  await freshPage(page);
  await openDataset(page, 'spatial.h5ad');
  const cell = page.getByTestId('view-cell').first();
  await page.waitForTimeout(300);
  const covered = await drawnFraction(page, cell, { x0: 0, x1: 0.6 });
  // slow
  await page.route('**/api/datasets/*/gene?*', async (route) => {
    await new Promise((r) => setTimeout(r, 1500));
    await route.continue();
  });
  await page.getByTestId('gene-search').fill('Lin28a');
  await page.getByTestId('gene-search').press('Enter');
  await expect(cell).toHaveAttribute('data-status', 'loading');
  expect(await drawnFraction(page, cell, { x0: 0, x1: 0.6 })).toBeGreaterThan(0.85 * covered);
  await expect(page.getByTestId('legend')).toContainText('Lin28a');
  await page.unroute('**/api/datasets/*/gene?*');
  // failing
  await page.route('**/api/datasets/*/gene?*', (route) =>
    route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":{"code":"x","message":"boom"}}' }),
  );
  await page.getByTestId('gene-search').fill('Plek');
  await page.getByTestId('gene-search').press('Enter');
  await expect(page.getByTestId('view-problem')).toContainText('boom');
  expect(await drawnFraction(page, cell, { x0: 0, x1: 0.6 })).toBeGreaterThan(0.85 * covered);
  await page.unroute('**/api/datasets/*/gene?*');
  await page.getByTestId('view-problem').getByRole('button', { name: 'Retry' }).click();
  await expect(page.getByTestId('legend')).toContainText('Plek');
  await expect(page.getByTestId('view-problem')).toHaveCount(0);
});
