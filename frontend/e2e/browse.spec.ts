import { expect, test } from '@playwright/test';

import { collectErrors, drawnColours, drawnFraction, freshPage, mainColours, openDataset } from './helpers';

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
  await page.getByLabel('About “Table”').hover();
  await expect(page.getByRole('tooltip')).toContainText('one row per object');
  await page.mouse.move(800, 400, { steps: 5 });
  await expect(page.getByRole('tooltip')).toHaveCount(0);
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
  expect(mainColours(await drawnColours(page, cell, { x0: 0, x1: 0.6 })).length).toBeGreaterThan(4); // a gradient
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

test('palettes: colours follow the label name across datasets; changes send no point data', async ({ page }) => {
  await page.addInitScript(() => {
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
  });
  await freshPage(page);
  await openDataset(page, 'region_1', 'table_cells');
  const legends = page.getByTestId('legend');
  const swatch = async (view: number, label: string) => {
    const lg = legends.nth(view);
    const toggle = lg.locator('.legend-toggle');
    if ((await toggle.getAttribute('aria-expanded')) === 'false') await toggle.click(); // small views start folded
    const item = lg.locator('.legend-item').filter({ has: page.locator('.legend-name', { hasText: new RegExp(`^${label}$`) }) });
    return item.locator('.swatch').evaluate((e) => getComputedStyle(e).backgroundColor);
  };
  // region_1 brings its own colours; the default palette is chosen explicitly to share colours by name
  await page.getByTestId('palette-select').click();
  await page.getByRole('option', { name: /^Default/ }).click();
  const first = await swatch(0, '1');
  await openDataset(page, 'region_3', 'table_cells');
  await page.getByTestId('palette-select').click();
  await page.getByRole('option', { name: /^Default/ }).click();
  expect(await swatch(1, '1')).toBe(first); // cluster "1" has the same colour in both datasets

  const cell = page.getByTestId('view-cell').nth(1);
  const uploaded = () => page.evaluate(() => (window as unknown as { __uploaded: number }).__uploaded);
  await page.waitForTimeout(300);
  const before = await uploaded();
  const coloursBefore = mainColours(await drawnColours(page, cell));
  await page.getByTestId('palette-select').click();
  await page.getByRole('option', { name: /^Colour-blind/ }).click();
  await legends.nth(1).locator('.legend-item').first().click(); // hide one category
  await page.waitForTimeout(300);
  const coloursAfter = mainColours(await drawnColours(page, cell));
  expect(coloursAfter.join()).not.toBe(coloursBefore.join());
  // 7.8 k cells: positions are 62 KB, values 31 KB. Only the colour table and uniforms may move.
  expect((await uploaded()) - before).toBeLessThan(10_000);
});
