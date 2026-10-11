import { describe, expect, it } from 'vitest';

import { categoryColouring, displayRange, lookup, numberColouring, robustRange } from './colouring';
import { gpuValues, MISSING_VALUE } from './gpu';
import {
  categoryColours,
  colormapLut,
  coloursExtended,
  hexToRgb,
  midColour,
  paletteSize,
  PALETTES,
  resolvePalette,
  rgbToHex,
  slotColours,
} from './palettes';
import { assignSlots, hashLabel } from './slots';

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

  it('colours by slot, and says when colours are interpolated', () => {
    expect(categoryColours('dataset', [0, 1], ['#000000', '#ffffff'])).toEqual([
      [0, 0, 0],
      [255, 255, 255],
    ]);
    expect(categoryColours('colourblind', [0, 7], null)[1]).toEqual(categoryColours('colourblind', [0], null)[0]);
    expect(coloursExtended('default', 54)).toBe(false);
    expect(coloursExtended('default', 60)).toBe(true);
    expect(coloursExtended('many', 200)).toBe(false);
  });

  it('adds in-between colours instead of repeating them (feedback S5)', () => {
    expect(paletteSize('colourblind', 5)).toBe(7);
    expect(paletteSize('colourblind', 20)).toBe(28);
    expect(paletteSize('default', 60)).toBe(108);
    const list = slotColours('colourblind', 28);
    expect(new Set(list.map(rgbToHex)).size).toBe(28);
    // the palette's own colours stay, at every 4th place
    expect(list.filter((_, i) => i % 4 === 0)).toEqual(slotColours('colourblind', 7));
    // 20 categories with 7 colours: all different
    const labels = Array.from({ length: 20 }, (_, i) => `c${i}`);
    const order = labels.map((_, i) => i);
    const cols = categoryColours('colourblind', assignSlots(labels, order, paletteSize('colourblind', 20)), null);
    expect(new Set(cols.map(rgbToHex)).size).toBe(20);
  });

  it('mixes two vivid colours into a vivid one, not grey', () => {
    const [r, g, b] = midColour(hexToRgb('#1f77b4'), hexToRgb('#ff7f0e'));
    expect(Math.max(r, g, b) - Math.min(r, g, b)).toBeGreaterThan(60);
    expect(midColour(hexToRgb('#ff0000'), hexToRgb('#ff0000'))).toEqual([255, 0, 0]);
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

  it('gives a label the same slot in every column, whatever was seen before', () => {
    const a = ['Astrocyte', 'Neuron'];
    const b = ['Microglia', 'Oligodendrocyte'];
    const all = ['Astrocyte', 'Microglia', 'Neuron', 'Oligodendrocyte'];
    const slotOf = (labels: string[]) => Object.fromEntries(labels.map((l, k) => [l, assignSlots(labels, nat(labels.length), 54)[k]]));
    const sa = slotOf(a);
    const sb = slotOf(b);
    const sAll = slotOf(all);
    // without collisions inside a column, a label's slot is its hash: the same everywhere
    for (const l of all) expect(sAll[l]).toBe(hashLabel(l) % 54);
    expect(sa.Astrocyte).toBe(sAll.Astrocyte);
    expect(sb.Microglia).toBe(sAll.Microglia);
  });

  it('never gives two labels of one column the same slot while slots are free', () => {
    const labels = Array.from({ length: 54 }, (_, i) => `cluster ${i}`);
    expect(new Set(assignSlots(labels, nat(54), 54)).size).toBe(54);
  });

  it('repeats colours only beyond the palette size', () => {
    const labels = nat(10).map(String);
    const slots = assignSlots(labels, nat(10), 7);
    expect(new Set(slots.slice(0, 7)).size).toBe(7);
  });

  it('is stable across runs (part of the colour contract)', () => {
    expect(hashLabel('T cell')).toBe(hashLabel('T cell'));
    expect(hashLabel('')).toBe(0x811c9dc5);
  });
});
