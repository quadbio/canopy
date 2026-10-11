import { expect, type Locator, type Page } from '@playwright/test';

/** Share of pixels in an element's screenshot that differ clearly from its top-left pixel
 * (the canvas background): proves something was actually drawn. */
export async function drawnFraction(page: Page, target: Locator, clip?: { x0: number; x1: number }): Promise<number> {
  const png = (await target.screenshot()).toString('base64');
  return page.evaluate(
    async ({ png, clip }) => {
      const blob = await (await fetch(`data:image/png;base64,${png}`)).blob();
      const bmp = await createImageBitmap(blob);
      const c = new OffscreenCanvas(bmp.width, bmp.height);
      const ctx = c.getContext('2d')!;
      ctx.drawImage(bmp, 0, 0);
      const x0 = Math.round((clip?.x0 ?? 0) * bmp.width);
      const x1 = Math.round((clip?.x1 ?? 1) * bmp.width);
      const top = 40; // skip the title row
      const { data } = ctx.getImageData(x0, top, x1 - x0, bmp.height - top - 2);
      const bg = ctx.getImageData(2, bmp.height - 3, 1, 1).data;
      let n = 0;
      for (let i = 0; i < data.length; i += 4) {
        const d = Math.abs(data[i] - bg[0]) + Math.abs(data[i + 1] - bg[1]) + Math.abs(data[i + 2] - bg[2]);
        if (d > 60) n += 1;
      }
      return n / (data.length / 4);
    },
    { png, clip },
  );
}

/** Share of drawn pixels per colour (quantised to 16 levels per channel), in an element's
 * screenshot: shows that points are coloured per point, and which colours are on screen. */
export async function drawnColours(
  page: Page,
  target: Locator,
  clip?: { x0: number; x1: number },
): Promise<Record<string, number>> {
  const png = (await target.screenshot()).toString('base64');
  return page.evaluate(
    async ({ png, clip }) => {
      const blob = await (await fetch(`data:image/png;base64,${png}`)).blob();
      const bmp = await createImageBitmap(blob);
      const c = new OffscreenCanvas(bmp.width, bmp.height);
      const ctx = c.getContext('2d')!;
      ctx.drawImage(bmp, 0, 0);
      const x0 = Math.round((clip?.x0 ?? 0) * bmp.width);
      const x1 = Math.round((clip?.x1 ?? 1) * bmp.width);
      const top = 40;
      const { data } = ctx.getImageData(x0, top, x1 - x0, bmp.height - top - 2);
      const bg = ctx.getImageData(2, bmp.height - 3, 1, 1).data;
      const counts: Record<string, number> = {};
      let drawn = 0;
      for (let i = 0; i < data.length; i += 4) {
        const d = Math.abs(data[i] - bg[0]) + Math.abs(data[i + 1] - bg[1]) + Math.abs(data[i + 2] - bg[2]);
        if (d <= 60) continue;
        drawn += 1;
        const k = `${data[i] >> 4},${data[i + 1] >> 4},${data[i + 2] >> 4}`;
        counts[k] = (counts[k] ?? 0) + 1;
      }
      for (const k of Object.keys(counts)) counts[k] /= drawn || 1;
      return counts;
    },
    { png, clip },
  );
}

/** Colours covering at least `min` of the drawn pixels. */
export const mainColours = (c: Record<string, number>, min = 0.005) => Object.keys(c).filter((k) => c[k] >= min);

export async function freshPage(page: Page) {
  await page.goto('/');
  await page.evaluate(() => localStorage.clear());
  await page.reload();
}

export async function openDataset(page: Page, name: string, table?: string) {
  await page.getByTestId('dataset-row').filter({ hasText: name }).first().click();
  await expect(page.getByTestId('details')).toContainText(name);
  if (table) await page.getByTestId('details').locator('select').selectOption(table);
  const before = await page.getByTestId('view-cell').count();
  await page.getByTestId('open-dataset').click();
  await expect(page.getByTestId('view-cell')).toHaveCount(before + 1);
  await expect(page.getByTestId('view-cell').last()).toHaveAttribute('data-status', 'ready');
}

export function collectErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
  page.on('pageerror', (e) => errors.push(e.message));
  return errors;
}
