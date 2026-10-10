import { describe, expect, it } from 'vitest';

import { fitBounds, gridCells } from './layout';

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
    const v = fitBounds([0, 0, 100, 50], { x: 0, y: 0, width: 200, height: 200 }, 0);
    expect(v.target).toEqual([50, 25, 0]);
    expect(v.zoom).toBeCloseTo(1); // 2 px per unit
  });
});
