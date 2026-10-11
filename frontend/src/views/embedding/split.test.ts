import { describe, expect, it } from 'vitest';

import { splitGroups, yDown } from '.';

describe('splitGroups', () => {
  const pos = Float32Array.from([0, 0, 1, 1, 2, 2, 3, 3, 4, 4]);

  it('groups cells per category in natural label order, NA last, empty categories left out', () => {
    const g = splitGroups([1, 0, 1, -1, 1], ['s10', 's2', 'empty'], null, pos);
    expect(g.map((x) => x.label)).toEqual(['s2', 's10', 'NA']);
    expect(Array.from(g[0].cells)).toEqual([0, 2, 4]); // code 1 = s2
    expect(Array.from(g[1].cells)).toEqual([1]);
    expect(g[0].bounds).toEqual([0, 0, 4, 4]);
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
