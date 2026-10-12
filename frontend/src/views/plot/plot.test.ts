import { describe, expect, it } from 'vitest';

import type { DatasetMeta } from '../../api/types';
import { defaultFeatures, featureKind, plotKind, SCATTER_MAX, scatterPositions } from '.';

const meta = {
  n_obs: 100,
  obs: [
    { name: 'sample', kind: 'categorical', categories: ['a', 'b'] },
    { name: 'cell_type', kind: 'categorical', categories: ['T', 'B', 'NK'] },
    { name: 'total_counts', kind: 'numeric' },
    { name: 'flag', kind: 'boolean', categories: ['false', 'true'] },
  ],
} as unknown as DatasetMeta;

describe('plot view', () => {
  it('a pair of features asks for its plot', () => {
    expect(plotKind('category', 'number', 10, 'auto')).toEqual({ kind: 'violin', swap: false });
    expect(plotKind('number', 'category', 10, 'auto')).toEqual({ kind: 'violin', swap: true });
    expect(plotKind('category', 'category', 10, 'auto')?.kind).toBe('bars');
    expect(plotKind('number', 'number', 10, 'auto')?.kind).toBe('scatter');
    expect(plotKind('number', 'number', SCATTER_MAX + 1, 'auto')?.kind).toBe('density');
    expect(plotKind('number', 'number', 10, 'density')?.kind).toBe('density');
    expect(plotKind(null, 'number', 10, 'auto')).toBeNull();
  });

  it('features are categories or numbers; genes are numbers', () => {
    expect(featureKind({ kind: 'obs', name: 'flag' }, meta)).toBe('category');
    expect(featureKind({ kind: 'obs', name: 'total_counts' }, meta)).toBe('number');
    expect(featureKind({ kind: 'gene', name: 'CD3E' }, meta)).toBe('number');
    expect(featureKind({ kind: 'obs', name: 'nope' }, meta)).toBeNull();
  });

  it('defaults to the cell type column against the first number', () => {
    expect(defaultFeatures(meta)).toEqual({ x: { kind: 'obs', name: 'cell_type' }, y: { kind: 'obs', name: 'total_counts' } });
  });

  it('scatter positions fill [0, aspect] × [0, 1] and leave out missing values', () => {
    const xs = Float32Array.from([0, 5, 10, NaN]);
    const ys = Float32Array.from([1, 2, 3, 4]);
    const p = scatterPositions(xs, ys, false, false, 2);
    expect(Array.from(p.cells)).toEqual([0, 1, 2]);
    expect(Array.from(p.pos)).toEqual([0, 0, 1, 0.5, 2, 1]);
    expect(p.xr).toEqual([0, 10]);
    const l = scatterPositions(Float32Array.from([0, Math.E - 1]), Float32Array.from([0, 1]), true, false);
    expect(l.xr[1]).toBeCloseTo(1);
  });
});
