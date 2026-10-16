import { describe, expect, it } from 'vitest';

import type { ColumnMeta, DatasetMeta } from '../../api/types';
import { boundsOf, effectiveParams, levelCodes, mergeLevels, normaliseParams, splitGroups, splittable, yDown } from '.';

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

describe('split by numbers with few values', () => {
  const num: ColumnMeta = { name: 'time', kind: 'numeric', dtype: 'float64', n_categories: null, ordered: null, categories: null, colors: null };
  const levels = { columns: { time: { values: [Math.fround(0.1), 0.5, 2.5], labels: ['0.1', '0.5', '2.5'] } } };

  it('offers a number column only when the server lists its levels', () => {
    expect(splittable(num)).toBe(false);
    expect(splittable(num, levels)).toBe(true);
    expect(splittable({ ...num, name: 'score' }, levels)).toBe(false);
  });

  it('turns values (as sent, float32) into level codes, missing and others as NA', () => {
    const values = Float32Array.from([2.5, 0.1, NaN, 0.5, 7]);
    expect(Array.from(levelCodes(values, levels.columns.time.values))).toEqual([2, 0, -1, 1, -1]);
    const g = splitGroups(levelCodes(values, levels.columns.time.values), levels.columns.time.labels, null, new Float32Array(10));
    expect(g.map((x) => x.label)).toEqual(['0.1', '0.5', '2.5', 'NA']);
  });

  it('keeps a saved number split until the levels are known, then drops it if not one', () => {
    const meta = { obs: [num], embeddings: [{ key: 'X_umap', dims: 2 }], matrices: [] } as unknown as DatasetMeta;
    const p = { ...normaliseParams({ embedding: 'X_umap' })!, splitBy: 'time' };
    expect(effectiveParams(p, meta).p.splitBy).toBe('time');
    expect(effectiveParams(p, meta, undefined, false, levels).notice).toBeUndefined();
    const r = effectiveParams(p, meta, undefined, false, { columns: {} });
    expect(r.p.splitBy).toBeNull();
    expect(r.notice).toContain('too many different values');
  });

  it("merges a group's levels: every member's values once", () => {
    const m = mergeLevels([levels, { columns: { time: { values: [0.5, 4], labels: ['0.5', '4'] } } }, undefined]);
    expect(m.columns.time.labels).toEqual(['0.1', '0.5', '2.5', '4']);
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
