import { describe, expect, it } from 'vitest';

import { MISSING_VALUE } from '../colour/missing';
import { inside, lassoInto, NOT_SELECTED, SELECTED, selectionBars, selectionViolin } from './selection';

const square: [number, number][] = [
  [0, 0],
  [2, 0],
  [2, 2],
  [0, 2],
];

describe('lasso', () => {
  it('tells inside from outside, also for a concave shape', () => {
    expect(inside(1, 1, square)).toBe(true);
    expect(inside(3, 1, square)).toBe(false);
    const u: [number, number][] = [
      [0, 0],
      [3, 0],
      [3, 3],
      [2, 3],
      [2, 1],
      [1, 1],
      [1, 3],
      [0, 3],
    ];
    expect(inside(1.5, 2, u)).toBe(false); // in the notch
    expect(inside(0.5, 2, u)).toBe(true);
  });

  it('selects the cells inside, skipping hidden ones, only in a panel’s cells', () => {
    const xy = new Float32Array([1, 1, 5, 5, 1.5, 0.5, 0.2, 1.8]);
    const mask = new Uint8Array(4);
    expect(lassoInto(mask, xy, square, null, (i) => i === 3)).toBe(2);
    expect([...mask]).toEqual([1, 0, 1, 0]);
    const panel = new Uint8Array(4);
    expect(lassoInto(panel, xy, square, Int32Array.from([1, 2]))).toBe(1);
    expect([...panel]).toEqual([0, 0, 1, 0]);
  });

  it('adds to a selection without counting a cell twice', () => {
    const xy = new Float32Array([1, 1, 3, 1]);
    const mask = new Uint8Array(2);
    lassoInto(mask, xy, square);
    const wider: [number, number][] = [
      [0, 0],
      [4, 0],
      [4, 2],
      [0, 2],
    ];
    expect(lassoInto(mask, xy, wider)).toBe(1);
    expect([...mask]).toEqual([1, 1]);
  });

  it('maps polygon rows to table rows (spatial: centroids in polygon order)', () => {
    const xy = new Float32Array([1, 1, 9, 9]);
    const mask = new Uint8Array(3);
    lassoInto(mask, xy, square, null, null, Int32Array.from([2, 0]));
    expect([...mask]).toEqual([0, 0, 1]);
  });
});

describe('selection summaries', () => {
  it('composition of the selected cells and of the others', () => {
    const s = selectionBars(Float32Array.from([0, 1, 1, 0, -1]), ['a', 'b'], Uint8Array.from([1, 1, 0, 0, 1]));
    expect(s.rows).toEqual([SELECTED, NOT_SELECTED]);
    expect(s.counts).toEqual([
      [1, 1],
      [1, 1],
    ]);
    expect(s.n_missing).toBe(1);
  });

  it('violins of a number for the selected cells and the others', () => {
    const vals = Float32Array.from([0, 1, 2, 3, 4, MISSING_VALUE]);
    const s = selectionViolin(vals, Uint8Array.from([1, 1, 1, 0, 0, 1]), false, 4);
    expect(s.categories).toEqual([SELECTED, NOT_SELECTED]);
    expect(s.range).toEqual([0, 4]);
    expect(s.groups[0]).toMatchObject({ n: 3, median: 1, frac_above_zero: 2 / 3 });
    expect(s.groups[1]).toMatchObject({ n: 2, median: 3.5, frac_above_zero: 1 });
    expect(s.groups[0].hist.reduce((a, b) => a + b, 0)).toBe(3);
    expect(s.n_missing_value).toBe(1);
  });
});
