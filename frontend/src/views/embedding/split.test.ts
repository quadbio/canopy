import { describe, expect, it } from 'vitest';

import { boundsOf, splitGroups, yDown } from '.';

describe('splitGroups', () => {
  const pos = Float32Array.from([0, 0, 1, 1, 2, 2, 3, 3, 4, 4]);

  it("groups cells per category in the column's own order, NA last, empty categories left out", () => {
    // the order of .cat.categories (e.g. stages), not by name (owner, 2026-10-15)
    const g = splitGroups([1, 0, 1, -1, 1], ['s10', 's2', 'empty'], null, pos);
    expect(g.map((x) => x.label)).toEqual(['s10', 's2', 'NA']);
    expect(Array.from(g[0].cells)).toEqual([1]); // code 0 = s10
    expect(Array.from(g[1].cells)).toEqual([0, 2, 4]); // code 1 = s2
    expect(g[1].bounds).toEqual([0, 0, 4, 4]);
    expect(g[2].id).toBe('na');
  });

  it('keeps the draw order inside each group, so a prefix stays representative', () => {
    const order = Int32Array.from([4, 3, 2, 1, 0]);
    const g = splitGroups([0, 0, 0, 1, 0], ['a', 'b'], order, pos);
    expect(Array.from(g[0].cells)).toEqual([4, 2, 1, 0]);
  });
});

describe('yDown', () => {
  it('treats spatial coordinates as image coordinates', () => {
    expect(yDown('spatial')).toBe(true);
    expect(yDown('X_spatial')).toBe(true);
    expect(yDown('X_umap')).toBe(false);
  });
});

describe('panel bounds', () => {
  it('a few stray cells do not widen a large panel', () => {
    const n = 2000;
    const pos = new Float32Array(2 * n);
    for (let i = 0; i < n; i += 1) {
      pos[2 * i] = i % 100;
      pos[2 * i + 1] = Math.floor(i / 100);
    }
    pos[0] = 1e6; // one stray cell
    const b = boundsOf(pos, Int32Array.from({ length: n }, (_, i) => i))!;
    expect(b[2]).toBeLessThan(200);
    const small = boundsOf(pos, Int32Array.from([0, 1, 2]))!;
    expect(small[2]).toBe(1e6); // small panels keep every cell
  });
});
