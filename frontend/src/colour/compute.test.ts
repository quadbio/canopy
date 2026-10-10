import { describe, expect, it } from 'vitest';

import { computeColours, robustRange } from './compute';
import { categoryColors, colormapLut, hexToRgb } from './palettes';

describe('computeColours', () => {
  it('colours categories, greys missing codes and hides hidden categories', () => {
    const { rgba } = computeColours({
      kind: 'categorical',
      codes: new Int8Array([0, 1, -1, 1]),
      colors: [hexToRgb('#ff0000'), hexToRgb('#00ff00')],
      hidden: [false, true],
      opacity: 1,
    });
    expect(Array.from(rgba.slice(0, 4))).toEqual([255, 0, 0, 255]);
    expect(rgba[7]).toBe(0); // hidden
    expect(Array.from(rgba.slice(8, 12))).toEqual([190, 190, 190, 255]); // missing
  });

  it('maps continuous values through the colormap within the range', () => {
    const lut = colormapLut('viridis');
    const { rgba, range } = computeColours({
      kind: 'continuous',
      values: new Float32Array([0, 10, NaN, 20]),
      colormap: 'viridis',
      range: [0, 10],
      opacity: 0.5,
    });
    expect(range).toEqual([0, 10]);
    expect(Array.from(rgba.slice(0, 3))).toEqual(Array.from(lut.slice(0, 3)));
    expect(Array.from(rgba.slice(4, 7))).toEqual(Array.from(lut.slice(255 * 3)));
    expect(Array.from(rgba.slice(12, 15))).toEqual(Array.from(lut.slice(255 * 3))); // clamped
    expect(rgba[3]).toBe(128);
  });

  it('uses a robust range that ignores outliers and NaN', () => {
    const v = new Float32Array(1000).map((_, i) => i);
    v[999] = 1e9;
    v[0] = NaN;
    const [lo, hi] = robustRange(v);
    expect(lo).toBeGreaterThanOrEqual(1);
    expect(hi).toBeLessThan(1000);
  });
});

describe('palettes', () => {
  it('prefers the dataset colours when there is one per category', () => {
    expect(categoryColors(2, ['#000000', '#ffffff'])).toEqual([
      [0, 0, 0],
      [255, 255, 255],
    ]);
    expect(categoryColors(40, null)).toHaveLength(40);
  });

  it('viridis starts dark purple and ends yellow', () => {
    const lut = colormapLut('viridis');
    expect(lut[0]).toBeLessThan(80);
    expect(lut[255 * 3 + 1]).toBeGreaterThan(200);
  });
});
