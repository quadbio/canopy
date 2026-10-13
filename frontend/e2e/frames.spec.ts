import { expect, type Page, test } from '@playwright/test';

import { freshPage, openDataset } from './helpers';

// Frame times on large embeddings (first host test: ~9 fps at 1.7 M cells on an M2). Not part of
// the usual run: needs the synthetic datasets (CANOPY_DEMO_LARGE=1 pixi run -e dev demo makes
// them) and the GPU project.
//   CANOPY_BENCH=1 CANOPY_E2E_LARGE=1 CANOPY_E2E_GPU=1 pnpm exec playwright test --project gpu-2x frames
test.skip(process.env.CANOPY_BENCH !== '1', 'benchmark: CANOPY_BENCH=1');
test.setTimeout(300_000);
const [vw, vh] = (process.env.CANOPY_BENCH_VIEWPORT ?? '1440x900').split('x').map(Number);
test.use({ viewport: { width: vw, height: vh } });

/** Counts frames that drew something: WebGL draw calls mark the animation frame. */
function installCounter(page: Page) {
  return page.addInitScript(() => {
    const w = window as unknown as { __drew: boolean; __frames: number[]; __on: boolean };
    w.__drew = false;
    w.__frames = [];
    w.__on = false;
    const proto = WebGL2RenderingContext.prototype as unknown as Record<string, (...a: unknown[]) => unknown>;
    for (const m of ['drawArrays', 'drawElements', 'drawArraysInstanced', 'drawElementsInstanced']) {
      const orig = proto[m];
      proto[m] = function (this: unknown, ...a: unknown[]) {
        w.__drew = true;
        return orig.apply(this, a);
      };
    }
    let last = 0;
    const tick = (t: number) => {
      if (w.__drew && w.__on) {
        if (last) w.__frames.push(t - last);
        last = t;
      } else if (!w.__on) last = 0;
      w.__drew = false;
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
}

/** Intervals (ms) between frames that drew, while `act` runs. */
async function frames(page: Page, act: () => Promise<void>): Promise<{ n: number; median: number; p95: number }> {
  await page.evaluate(() => {
    const w = window as unknown as { __frames: number[]; __on: boolean };
    w.__frames = [];
    w.__on = true;
  });
  await act();
  const xs: number[] = await page.evaluate(() => {
    const w = window as unknown as { __frames: number[]; __on: boolean };
    w.__on = false;
    return w.__frames.filter((x) => x < 1000);
  });
  xs.sort((a, b) => a - b);
  const q = (p: number) => (xs.length ? xs[Math.min(xs.length - 1, Math.floor(p * xs.length))] : NaN);
  return { n: xs.length, median: Math.round(q(0.5) * 10) / 10, p95: Math.round(q(0.95) * 10) / 10 };
}

/** Time of one forced redraw and of one hover pick, each waited for on the GPU (median of 15). */
async function costs(page: Page, x: number, y: number) {
  return page.evaluate(
    ({ x, y }) => {
      const deck = (window as unknown as { __canopyDeck: { deck: { redraw: (r: string) => void; pickObject: (o: object) => unknown; getCanvas?: () => HTMLCanvasElement; canvas?: HTMLCanvasElement } } }).__canopyDeck.deck;
      const canvas = deck.getCanvas?.() ?? deck.canvas!;
      const gl = canvas.getContext('webgl2')!;
      const px = new Uint8Array(4);
      const sync = () => gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
      const box = canvas.getBoundingClientRect();
      const med = (f: () => void) => {
        const xs: number[] = [];
        for (let i = 0; i < 15; i += 1) {
          sync();
          const t = performance.now();
          f();
          sync();
          xs.push(performance.now() - t);
        }
        xs.sort((a, b) => a - b);
        return Math.round(xs[7] * 10) / 10;
      };
      return {
        draw_ms: med(() => deck.redraw('bench')),
        pick_ms: med(() => deck.pickObject({ x: x - box.left, y: y - box.top, radius: 3 })),
      };
    },
    { x, y },
  );
}

for (const name of (process.env.CANOPY_BENCH_DATA ?? 'synthetic_1M_cells.h5ad,synthetic_3M_cells.h5ad').split(',')) {
  test(`frame times: ${name}`, async ({ page }) => {
    await installCounter(page);
    await freshPage(page);
    await openDataset(page, name);
    const cell = page.getByTestId('view-cell').last();
    await page.waitForTimeout(3000); // the representative order and colours settle
    const b = (await cell.boundingBox())!;
    const cx = b.x + b.width / 2;
    const cy = b.y + b.height / 2;
    const out: Record<string, unknown> = {};
    const t0 = Date.now();
    const step = (what: string) => console.log(`STEP ${((Date.now() - t0) / 1000).toFixed(1)} s ${what} ${JSON.stringify(out)}`);
    step('pan');
    out.pan = await frames(page, async () => {
      await page.mouse.move(cx, cy);
      await page.mouse.down();
      for (let i = 0; i < 60; i += 1) await page.mouse.move(cx + 3 * i, cy + i);
      await page.mouse.up();
    });
    step('zoom');
    out.zoom = await frames(page, async () => {
      await page.mouse.move(cx, cy);
      for (let i = 0; i < 20; i += 1) {
        await page.mouse.wheel(0, -60);
        await page.waitForTimeout(16);
      }
    });
    step('hover');
    out.hover = await frames(page, async () => {
      for (let i = 0; i < 60; i += 1) {
        await page.mouse.move(cx - 100 + 3 * i, cy - 50 + 2 * i);
        await page.waitForTimeout(16);
      }
    });
    step('zoomOut');
    out.zoomOut = await frames(page, async () => {
      for (let i = 0; i < 20; i += 1) {
        await page.mouse.wheel(0, 60);
        await page.waitForTimeout(16);
      }
    });
    step('cost');
    out.cost = await costs(page, cx, cy);
    for (let i = 0; i < 6; i += 1) {
      await page.mouse.move(cx, cy);
      await page.mouse.wheel(0, -120);
      await page.waitForTimeout(300);
    }
    step('costZoomedIn');
    out.costZoomedIn = await costs(page, cx, cy);
    // back to the whole view, then the costs with a gene colouring (higher values on top: depth
    // test) and with larger points
    for (let i = 0; i < 8; i += 1) {
      await page.mouse.move(cx, cy);
      await page.mouse.wheel(0, 120);
      await page.waitForTimeout(200);
    }
    step('gene search');
    await page.getByTestId('gene-search').fill('1');
    await page.waitForTimeout(800);
    await page.getByTestId('gene-search').press('Enter');
    await expect(cell).toHaveAttribute('data-status', 'ready');
    await page.waitForTimeout(1500);
    step('costGene');
    out.costGene = await costs(page, cx, cy);
    const size = page.getByRole('slider', { name: 'Point size' });
    await size.focus();
    for (let i = 0; i < 5; i += 1) await page.keyboard.press('ArrowRight');
    await page.waitForTimeout(800);
    step('costGeneBig');
    out.costGeneBig = await costs(page, cx, cy);
    step('panGeneBig');
    out.panGeneBig = await frames(page, async () => {
      await page.mouse.move(cx, cy);
      await page.mouse.down();
      for (let i = 0; i < 60; i += 1) await page.mouse.move(cx + 3 * i, cy + i);
      await page.mouse.up();
    });
    console.log(`FRAMES ${name} ${JSON.stringify(out)}`);
    expect(out).toBeTruthy();
  });
}
