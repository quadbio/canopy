import { describe, expect, it } from 'vitest';

import type { DatasetMeta } from '../../api/types';
import { viewType } from '../registry';
import { categoryAxisHeight, outlineCounts, shortLabel } from './charts';
import { companionParams, defaultFeatures, featureKind, plotKind, SCATTER_MAX, scatterPositions } from '.';

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

describe('companion plots', () => {
  it('are not opened on their own and keep the view they follow', () => {
    const t = viewType('plot')!;
    expect(t.create(meta)).toBeNull();
    const p = companionParams(meta, 'v1', { ...t.normalise({})!, palette: 'okabe-ito' } as never);
    expect(p).toMatchObject({ follows: 'v1', x: null, y: null, palette: 'okabe-ito' });
    expect(t.normalise(p)).toMatchObject({ follows: 'v1' });
    expect(t.normalise({ follows: 3 })).toMatchObject({ follows: null });
  });
});

describe('category labels', () => {
  it('are shortened in the middle, so labels differing at the end stay apart', () => {
    const a = shortLabel('a very long category label number 1');
    const b = shortLabel('a very long category label number 2');
    expect(a).not.toBe(b);
    expect(a.length).toBeLessThanOrEqual(22);
    expect(a.endsWith('1')).toBe(true);
    expect(shortLabel('T cell')).toBe('T cell');
  });
  it('get room below the axis by their length when turned', () => {
    expect(categoryAxisHeight(['a', 'b'], 100)).toBe(48);
    const long = categoryAxisHeight(['a very long category label number 1'], 30);
    expect(long).toBeGreaterThan(categoryAxisHeight(['short'], 30));
    expect(long).toBeLessThanOrEqual(140);
  });
});

describe('gene violins', () => {
  it('keep the small positive values of the bin holding 0 (review full-2 B-H2)', () => {
    // a counts gene: 3,980 cells at 0 and 4,043 at 1-3 share bin 0 (max 200, 64 bins)
    const hist = [8023, 900, 500, 0, 1];
    expect(outlineCounts(hist, 0, 3980)).toEqual([4043, 900, 500, 0, 1]);
    expect(outlineCounts(hist, 0, undefined)).toEqual([0, 900, 500, 0, 1]); // an older server
    expect(outlineCounts(hist, -1, 3980)).toEqual(hist); // not a gene: nothing apart
  });
});
