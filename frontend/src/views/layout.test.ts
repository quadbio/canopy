import { describe, expect, it } from 'vitest';

import { fitBounds, gridCells, resizeView } from './layout';

describe('gridCells', () => {
  it('lays out views as square as possible', () => {
    expect(gridCells(1, 800, 600)).toEqual([{ x: 0, y: 0, width: 800, height: 600 }]);
    const four = gridCells(4, 801, 601);
    expect(four.map((c) => [c.x, c.y])).toEqual([
      [0, 0],
      [401, 0],
      [0, 301],
      [401, 301],
    ]);
    expect(gridCells(5, 900, 600)).toHaveLength(5);
    expect(new Set(gridCells(9, 900, 900).map((c) => c.y)).size).toBe(3);
  });
});

describe('fitBounds', () => {
  it('centres the bounds and fits the tighter side', () => {
    const v = fitBounds([0, 0, 100, 50], { width: 200, height: 200 }, 0);
    expect(v.target).toEqual([50, 25, 0]);
    expect(v.zoom).toBeCloseTo(1); // 2 px per unit
  });

  it('leaves room for the title row', () => {
    const v = fitBounds([0, 0, 100, 100], { width: 200, height: 220 }, 0, 20);
    expect(v.zoom).toBeCloseTo(1);
    expect(v.target[1]).toBeCloseTo(55); // centre moved up by 10 px = 5 units
  });
});

describe('resizeView', () => {
  it('keeps the centre and scales the zoom with the cell', () => {
    const v = resizeView({ target: [3, 4, 0], zoom: 2 }, { width: 800, height: 600 }, { width: 400, height: 600 });
    expect(v.target).toEqual([3, 4, 0]);
    expect(v.zoom).toBeCloseTo(1);
  });
});

describe('fitBounds with insets and flipped y', () => {
  it('leaves room for an open legend at the right', () => {
    const v = fitBounds([0, 0, 100, 100], { width: 300, height: 200 }, 0, { right: 100 });
    expect(v.zoom).toBeCloseTo(1);
    expect(v.target[0]).toBeCloseTo(75); // view centre 50 px = 25 units right of the data centre
  });

  it('moves the centre the other way when y grows downwards', () => {
    const v = fitBounds([0, 0, 100, 100], { width: 200, height: 220 }, 0, 20, true);
    expect(v.target[1]).toBeCloseTo(45);
  });
});
