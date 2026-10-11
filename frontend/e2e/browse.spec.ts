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
  await expect(page.getByTestId('dataset-row')).toHaveCount(5); // data roots start expanded
  await page.getByRole('treeitem', { name: /extracted \d+$/ }).click(); // collapse
  await expect(page.getByTestId('dataset-row')).toHaveCount(3);
  await page.getByLabel('Search datasets').fill('region_1');
  await expect(page.getByTestId('dataset-row')).toHaveCount(1);
});

test('dataset details explain tables and storage notes in plain words', async ({ page }) => {
  await freshPage(page);
  await page.getByTestId('dataset-row').filter({ hasText: 'region_1' }).click();
  const details = page.getByTestId('details');
  await expect(details).toContainText('one per object in boundaries_cells');
  await details.getByLabel('Table', { exact: true }).selectOption('table_tiles');
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
  const item = lg.locator('.legend-row').filter({ has: page.locator('.legend-name', { hasText: new RegExp(`^${label}$`) }) });
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

test('more categories than palette colours: no repeats; colours picked by hand are kept', async ({ page }) => {
  const errors = collectErrors(page);
  await freshPage(page);
  await openDataset(page, 'example_with_layers.h5ad');
  await expect(page.getByTestId('layers')).toHaveText('counts'); // listed before any gene is picked
  // 80 samples with the 7 colour-blind safe colours: interpolated, all different
  await colourBy(page, 0, 'sample', /^Colour-blind/);
  const legend = page.getByTestId('legend');
  await expect(legend).toContainText('in-between colours added');
  const swatches = await legend
    .locator('.swatch')
    .evaluateAll((els) => els.map((e) => getComputedStyle(e).backgroundColor));
  expect(swatches.length).toBe(80);
  expect(new Set(swatches).size).toBe(80);

  // pick a colour for one cell type: legend and plot follow, and it is remembered
  await colourBy(page, 0, 'cell_type');
  const cell = page.getByTestId('view-cell').first();
  await legend.getByLabel('Colour of Monocyte').fill('#00ff00');
  await expect.poll(() => legendSwatch(page, 0, 'Monocyte')).toBe('rgb(0, 255, 0)');
  await page.waitForTimeout(300);
  expect(await colourShare(page, cell, [0, 255, 0], 20)).toBeGreaterThan(0.002);
  await page.getByRole('button', { name: 'Close all' }).click();
  await openDataset(page, 'example_with_layers.h5ad');
  expect(await legendSwatch(page, 0, 'Monocyte')).toBe('rgb(0, 255, 0)');
  await page.screenshot({ path: 'test-results/screens/custom-colour.png' });
  await page.getByTestId('reset-colours').click();
  await expect.poll(() => legendSwatch(page, 0, 'Monocyte')).not.toBe('rgb(0, 255, 0)');
  await expect(page.getByTestId('reset-colours')).toHaveCount(0);

  // values from the counts layer: whole numbers, a larger range than the log-normalised X
  await page.getByTestId('gene-search').fill('Monocyte_m1');
  await expect(page.getByRole('option', { name: /^Monocyte_m1/ })).toBeVisible();
  await page.getByTestId('gene-search').press('Enter');
  await expect(legend).toContainText('Monocyte_m1');
  const xMax = Number(await legend.locator('.colourbar-labels span').last().textContent());
  await page.getByTestId('matrix-select').click();
  await page.getByRole('option', { name: /^counts/ }).click();
  await expect(legend).toContainText('Monocyte_m1 (counts)');
  await expect(cell).toHaveAttribute('data-status', 'ready');
  const countsMax = Number(await legend.locator('.colourbar-labels span').last().textContent());
  expect(countsMax).toBeGreaterThan(xMax);
  expect(errors).toEqual([]);
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

test('level of detail: a prefix of the same buffers, more cells when zoomed in, all when isolated', async ({ page }) => {
  await page.addInitScript(countUploads);
  await page.goto('/?lod=20000'); // a canvas budget of 20 k points: the 52 k-cell example is "large"
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await openDataset(page, 'spatial.h5ad');
  const caption = page.getByTestId('view-caption');
  await expect(caption).toHaveText('20 k of 52 k cells');
  const cell = page.getByTestId('view-cell').first();
  const uploaded = () => page.evaluate(() => (window as unknown as { __uploaded: number }).__uploaded);
  await page.waitForTimeout(300);
  const before = await uploaded();
  // zoom in: more cells are drawn, from the buffers already on the GPU
  const box = (await cell.boundingBox())!;
  await page.mouse.move(box.x + box.width * 0.35, box.y + box.height * 0.55);
  for (let i = 0; i < 4; i += 1) {
    await page.mouse.wheel(0, -400);
    await page.waitForTimeout(150);
  }
  await expect(page.getByText('20 k of 52 k cells')).toHaveCount(0); // more (or all) cells drawn
  expect((await uploaded()) - before).toBeLessThan(20_000);
  // fit again; hovering names the cell under the pointer (through the drawn order)
  await cell.getByRole('button', { name: 'Fit to data' }).click();
  await expect(caption).toHaveText('20 k of 52 k cells');
  let tip: string | null = null;
  for (let gx = 0.25; gx < 0.6 && !tip; gx += 0.05) {
    for (let gy = 0.3; gy < 0.9 && !tip; gy += 0.05) {
      await page.mouse.move(box.x + gx * box.width, box.y + gy * box.height);
      if (await page.locator('.deck-tooltip').isVisible({ timeout: 150 }).catch(() => false)) {
        tip = await page.locator('.deck-tooltip').textContent();
      }
    }
  }
  expect(tip).toMatch(/^celltype_seqfish: /);
  // isolate one population: all of its cells are drawn
  const legend = page.getByTestId('legend');
  await legend.getByRole('button', { name: 'hide all' }).click();
  await legend.locator('.legend-item', { hasText: /^Gut tube/ }).click();
  await expect(caption).toHaveCount(0);
});

test('convert a dataset, keep working elsewhere, come back to the converted copy', async ({ page }) => {
  test.setTimeout(120_000);
  const errors = collectErrors(page);
  await freshPage(page);
  await page.getByTestId('dataset-row').filter({ hasText: 'dissociated.h5ad' }).click();
  const conv = page.getByTestId('details').getByTestId('conversion');
  await conv.getByTestId('convert').click();
  await expect(conv.getByRole('progressbar').or(conv.getByText('Converted'))).toBeVisible();
  // look at another dataset while it runs (the widget of this one is gone)
  await page.getByTestId('dataset-row').filter({ hasText: 'spatial.h5ad' }).click();
  await expect(page.getByTestId('details')).toContainText('spatial.h5ad');
  await expect
    .poll(async () => (await page.request.get('/api/jobs?active=true')).json(), { timeout: 90_000 })
    .toEqual([]);
  // back: the details know it is converted (the app-wide job watcher refreshed them)
  await page.getByTestId('dataset-row').filter({ hasText: 'dissociated.h5ad' }).click();
  await expect(page.getByTestId('details').getByTestId('conversion')).toContainText('Converted', { timeout: 15_000 });
  await page.screenshot({ path: 'test-results/screens/converted.png' });
  await openDataset(page, 'dissociated.h5ad');
  await expect(page.locator('.panel')).toContainText('converted copy');
  await page.getByTestId('gene-search').fill('a');
  await page.getByTestId('gene-search').press('Enter');
  await expect(page.getByTestId('view-cell').first()).toHaveAttribute('data-status', 'ready');
  await expect(page.getByTestId('legend').locator('.colourbar')).toBeVisible();
  // converting again is refused by the server; the details offer no button for it
  await expect(page.getByTestId('details').getByTestId('convert')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('split by a column: one panel per category, linked zoom, one legend', async ({ page }) => {
  const errors = collectErrors(page);
  await page.addInitScript(countUploads);
  await freshPage(page);
  await openDataset(page, 'spatial.h5ad');
  const cell = page.getByTestId('view-cell').first();
  const uploaded = () => page.evaluate(() => (window as unknown as { __uploaded: number }).__uploaded);
  await expect.poll(uploaded).toBeGreaterThan(0); // the first picture is on the GPU
  const start = await uploaded();
  await page.getByTestId('embedding-select').click();
  await page.getByRole('option', { name: /^spatial/ }).click();
  await expect(cell).toHaveAttribute('data-status', 'ready');
  await expect.poll(uploaded).toBeGreaterThan(start + 500_000);
  const before = await uploaded();
  const oneCopy = before - start; // all points of the dataset, once
  await page.getByTestId('split-select').click();
  await page.getByRole('option', { name: /^embryo/ }).click();
  await expect(cell.getByTestId('panel')).toHaveCount(3);
  await expect(cell.getByTestId('panel').first()).toContainText('·');
  await expect(page.getByTestId('legend')).toHaveCount(1);
  await expect(cell).toHaveAttribute('data-status', 'ready');
  await page.waitForTimeout(300);
  // each panel draws its own cells; all together about one copy of the points is uploaded
  for (const k of [0, 1, 2]) expect(await drawnFraction(page, cell.getByTestId('panel').nth(k))).toBeGreaterThan(0.01);
  await page.screenshot({ path: 'test-results/screens/split-by-embryo.png' });
  expect((await uploaded()) - before).toBeLessThan(1.2 * oneCopy);

  // zooming one panel zooms all of them
  const zoom0 = Number(await cell.getAttribute('data-zoom'));
  const p2 = (await cell.getByTestId('panel').nth(2).boundingBox())!;
  await page.mouse.move(p2.x + p2.width / 2, p2.y + p2.height / 2);
  await page.mouse.wheel(0, -400);
  await expect.poll(async () => Number(await cell.getAttribute('data-zoom'))).toBeGreaterThan(zoom0 + 0.5);

  // each panel filling its space: embryo1 (smaller than embryo3) is zoomed in further
  await page.getByTestId('view-cell').first().getByRole('button', { name: 'Fit to data' }).click();
  const shared = Number(await cell.getAttribute('data-zoom'));
  await page.getByTestId('same-scale').locator('input').uncheck();
  await expect.poll(async () => Number(await cell.getAttribute('data-zoom'))).toBeGreaterThan(shared + 0.5);

  // back to one panel
  await page.getByTestId('split-select').click();
  await page.getByRole('option', { name: /^None/ }).click();
  await expect(cell.getByTestId('panel')).toHaveCount(0);
  expect(errors).toEqual([]);
});
