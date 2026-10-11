import { describe, expect, it } from 'vitest';

import { categoryColouring, displayRange, lookup, numberColouring, robustRange } from './colouring';
import { gpuValues, MISSING_VALUE } from './gpu';
import { categoryColours, colormapLut, coloursRepeat, hexToRgb, PALETTES, resolvePalette } from './palettes';
import { assignSlots, MAX_SLOTS, mergeSlots } from './slots';

describe('colouring (CPU reference of the shader)', () => {
  it('colours categories, greys missing codes and hides hidden categories', () => {
    const vc = categoryColouring([hexToRgb('#ff0000'), hexToRgb('#00ff00')], [false, true]);
    expect(lookup(vc, 0)).toEqual([255, 0, 0, 255]);
    expect(lookup(vc, 1)[3]).toBe(0); // hidden
    expect(lookup(vc, -1)).toEqual([190, 190, 190, 255]); // missing
    expect(lookup(vc, 7)).toEqual([190, 190, 190, 255]); // out of range
  });

  it('maps numbers through the colour map within the range, clamped; missing is grey', () => {
    const lut = colormapLut('viridis');
    const vc = numberColouring('viridis', [0, 10]);
    expect(lookup(vc, 0)).toEqual(Array.from(lut.slice(0, 4)));
    expect(lookup(vc, 10)).toEqual(Array.from(lut.slice(255 * 4)));
    expect(lookup(vc, 20)).toEqual(Array.from(lut.slice(255 * 4)));
    expect(lookup(vc, MISSING_VALUE)).toEqual([190, 190, 190, 255]);
  });

  it('centres diverging maps on 0', () => {
    expect(displayRange([-1, 5], 'blue-red')).toEqual([-5, 5]);
    expect(displayRange([-1, 5], 'viridis')).toEqual([-1, 5]);
  });

  it('uses a robust range that ignores outliers and NaN', () => {
    const v = new Float32Array(1000).map((_, i) => i);
    v[999] = 1e9;
    v[0] = NaN;
    const [lo, hi] = robustRange(v);
    expect(lo).toBeGreaterThanOrEqual(1);
    expect(hi).toBeLessThan(1000);
  });

  it('sends NaN to the GPU as the missing marker and codes as they are', () => {
    expect(Array.from(gpuValues(new Float32Array([1, NaN]), true))).toEqual([1, MISSING_VALUE]);
    expect(Array.from(gpuValues(new Int8Array([2, -1]), false))).toEqual([2, -1]);
  });
});

describe('palettes', () => {
  it('automatic: the dataset colours when complete, else default, else many', () => {
    expect(resolvePalette('auto', 2, ['#000000', '#ffffff'])).toBe('dataset');
    expect(resolvePalette('auto', 3, ['#000000', '#ffffff'])).toBe('default');
    expect(resolvePalette('auto', 80, null)).toBe('many');
    expect(resolvePalette('dataset', 3, null)).toBe('default');
    expect(PALETTES).toContain('colourblind');
  });

  it('colours by slot, and says when colours repeat', () => {
    expect(categoryColours('dataset', [0, 1], ['#000000', '#ffffff'])).toEqual([
      [0, 0, 0],
      [255, 255, 255],
    ]);
    expect(categoryColours('colourblind', [0, 7], null)[1]).toEqual(categoryColours('colourblind', [0], null)[0]);
    expect(coloursRepeat('default', 54)).toBe(false);
    expect(coloursRepeat('default', 60)).toBe(true);
    expect(coloursRepeat('many', 200)).toBe(false);
  });

  it('has no black and no greys in the default and many palettes', () => {
    const slots = Array.from({ length: 256 }, (_, i) => i);
    for (const p of ['default', 'many'] as const) {
      const cols = categoryColours(p, slots.slice(0, p === 'default' ? 54 : 256), null);
      for (const [r, g, b] of cols) {
        expect(Math.max(r, g, b)).toBeGreaterThan(60);
        expect(Math.max(r, g, b) - Math.min(r, g, b)).toBeGreaterThan(10);
      }
    }
  });

  it('viridis starts dark purple and ends yellow (matplotlib values)', () => {
    const lut = colormapLut('viridis');
    expect(Array.from(lut.slice(0, 3))).toEqual([68, 1, 84]);
    expect(Array.from(lut.slice(255 * 4, 255 * 4 + 3))).toEqual([253, 231, 37]);
  });
});

describe('colour slots (colours by name)', () => {
  const nat = (n: number) => Array.from({ length: n }, (_, i) => i);

  it('gives new labels the lowest free slots and keeps known ones', () => {
    const known = new Map([['B cell', 3]]);
    const { slots, added } = assignSlots(['T cell', 'B cell', 'NK'], nat(3), known, 54);
    expect(slots).toEqual([0, 3, 1]);
    expect(added).toEqual([
      ['T cell', 0],
      ['NK', 1],
    ]);
  });

  it('never gives two labels of one column the same slot while slots are free', () => {
    const known = new Map([
      ['a', 0],
      ['b', 0], // learnt from different datasets
    ]);
    const { slots, added } = assignSlots(['a', 'b'], nat(2), known, 54);
    expect(new Set(slots).size).toBe(2);
    expect(added).toEqual([]); // the known slot of "b" is not overwritten
  });

  it('repeats colours only beyond the palette size', () => {
    const labels = nat(10).map(String);
    const { slots } = assignSlots(labels, nat(10), new Map(), 7);
    expect(slots.slice(0, 7)).toEqual(nat(7));
    expect(slots.slice(7)).toEqual([0, 1, 2]);
  });

  it('keeps at most MAX_SLOTS labels, dropping the oldest', () => {
    const many = Array.from({ length: MAX_SLOTS + 3 }, (_, i) => [`l${i}`, i % 54] as [string, number]);
    const merged = mergeSlots([], many);
    expect(merged).toHaveLength(MAX_SLOTS);
    expect(merged[0][0]).toBe('l3');
    expect(mergeSlots([['x', 1]], [['x', 2]])).toEqual([['x', 1]]);
  });
});
