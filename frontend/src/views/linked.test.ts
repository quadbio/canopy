import { describe, expect, it } from 'vitest';

import { hexToRgb } from '../colour/palettes';
import { groupParams, membersOf, restoreViews, tilesTaken, useWorkspace } from '../state/workspace';
import type { ColourLegendData } from './colouring';
import { normaliseParams } from './embedding';
import { mergeGeneLists, unionColumns } from './groupMeta';
import { tileCells } from './layout';
import { groupCategories, groupColours, groupRange, mergeLegends } from './linked';

describe('linked groups in the workspace', () => {
  const base = (d: string, emb = 'X_umap') => ({
    type: 'embedding',
    datasetId: d,
    table: null,
    title: d,
    params: normaliseParams({ embedding: emb, colorBy: 'celltype' })! as unknown as Record<string, unknown>,
  });

  it('share every setting but the embedding', () => {
    const ws = useWorkspace.getState();
    ws.reset();
    const g = ws.addGroup([base('a', 'X_umap'), base('b', 'umap'), base('c', 'X_umap')])!;
    const [a, b, c] = useWorkspace.getState().views;
    expect([a.group, b.group, c.group]).toEqual([g, g, g]);
    useWorkspace.getState().updateParams(b.id, { pointSize: 4, embedding: 'X_pca' });
    const after = useWorkspace.getState().views;
    expect(after.map((v) => v.params.pointSize)).toEqual([4, 4, 4]);
    expect(after.map((v) => v.params.embedding)).toEqual(['X_umap', 'X_pca', 'X_umap']);
  });

  it('open only on an empty canvas, and nothing opens next to them (owner)', () => {
    const ws = useWorkspace.getState();
    ws.reset();
    ws.addView(base('x'));
    expect(ws.addGroup([base('a'), base('b')])).toBeNull();
    useWorkspace.getState().reset();
    expect(useWorkspace.getState().addGroup([base('a'), base('b')])).not.toBeNull();
    expect(useWorkspace.getState().addView(base('y'))).toBeNull();
    const views = useWorkspace.getState().views;
    expect(tilesTaken(views)).toBe(1);
    expect(membersOf(views, views[0]).map((v) => v.datasetId)).toEqual(['a', 'b']);
  });

  it('leave one view on its own when all others are removed', () => {
    const ws = useWorkspace.getState();
    ws.reset();
    ws.addGroup([base('a'), base('b')]);
    useWorkspace.getState().removeView(useWorkspace.getState().views[0].id);
    expect(useWorkspace.getState().views.map((v) => [v.datasetId, v.group])).toEqual([['b', undefined]]);
  });

  it('a plot below its view takes no tile', () => {
    const views = [
      { id: 'a', group: undefined, params: {} },
      { id: 'p', group: undefined, params: { follows: 'a' } },
    ];
    expect(tilesTaken(views)).toBe(1);
  });

  it('close together', () => {
    const ws = useWorkspace.getState();
    ws.reset();
    const g = ws.addGroup([base('a'), base('b')])!;
    useWorkspace.getState().removeGroup(g);
    expect(useWorkspace.getState().views).toEqual([]);
  });

  it('are restored alone, beyond the 9 tiles in views', () => {
    const many = Array.from({ length: 12 }, (_, i) => ({ id: `v${i}`, ...base(`d${i}`), group: 'g1' }));
    // with anything else beside it (an older save), the group alone is restored
    const restored = restoreViews([...many, { id: 'solo', ...base('s'), group: 'g2' }]);
    expect(restored).toHaveLength(12);
    expect(restored.every((v) => v.group === 'g1')).toBe(true);
    // independent views: at most 9 tiles (B-L6)
    const loose = Array.from({ length: 12 }, (_, i) => ({ id: `w${i}`, ...base(`e${i}`) }));
    expect(restoreViews(loose)).toHaveLength(9);
  });

  it('keep each member’s own keys', () => {
    expect(groupParams({ embedding: 'a', size: 1 }, { embedding: 'b', size: 2 }, ['embedding'], false)).toEqual({ embedding: 'a', size: 2 });
    expect(groupParams({ embedding: 'a', size: 1 }, { embedding: 'b', size: 2 }, ['embedding'], true)).toEqual({ embedding: 'b', size: 2 });
  });
});

describe('group tiles', () => {
  it('put a group in one tile, its views in a grid below the title row and left of the legend', () => {
    const { cells, groups } = tileCells(
      [
        { id: 'x', follows: null },
        { id: 'a', follows: null, group: 'g' },
        { id: 'b', follows: null, group: 'g' },
        { id: 'c', follows: null, group: 'g' },
        { id: 'd', follows: null, group: 'g' },
      ],
      1000,
      500,
      0,
      30,
      { g: 200 },
    );
    expect(cells[0]).toEqual({ x: 0, y: 0, width: 500, height: 500 });
    expect(groups.g).toEqual({ x: 500, y: 0, width: 500, height: 500 });
    // 2 × 2 in 300 × 470, from (500, 30)
    expect(cells[1]).toMatchObject({ x: 500, y: 30 });
    expect(cells[4].x + cells[4].width).toBeLessThanOrEqual(800);
    expect(cells[4].y + cells[4].height).toBeLessThanOrEqual(500);
  });
});

describe('colours of a linked group', () => {
  it('use the preset colours when they agree, and give the others free palette colours', () => {
    const r = groupColours(
      [
        { key: 'k', labels: ['T', 'B'], given: ['#ff0000', '#00ff00'] },
        { key: 'k', labels: ['B', 'NK'], given: null },
      ],
      'auto',
    );
    expect(r.regenerated).toBe(false);
    expect(r.colourOf.get('T')).toEqual(hexToRgb('#ff0000'));
    expect(r.colourOf.get('B')).toEqual(hexToRgb('#00ff00'));
    const nk = r.colourOf.get('NK')!;
    expect(nk).not.toEqual(hexToRgb('#ff0000'));
    expect(nk).not.toEqual(hexToRgb('#00ff00'));
  });

  it('are regenerated when one category has two colours, or one colour two categories', () => {
    const twoColours = groupCategories([
      { key: 'k', labels: ['T'], given: ['#ff0000'] },
      { key: 'k', labels: ['T'], given: ['#0000ff'] },
    ]);
    expect(twoColours.conflict).toBe(true);
    const twoLabels = groupColours(
      [
        { key: 'k', labels: ['T'], given: ['#ff0000'] },
        { key: 'k', labels: ['B'], given: ['#FF0000'] },
      ],
      'auto',
    );
    expect(twoLabels.regenerated).toBe(true);
    expect(twoLabels.colourOf.get('T')).not.toEqual(twoLabels.colourOf.get('B'));
  });

  it('give every category of the union its own colour with a palette', () => {
    const r = groupColours(
      [
        { key: 'k', labels: ['a', 'b'] },
        { key: 'k', labels: ['c'] },
      ],
      'default',
    );
    const cs = ['a', 'b', 'c'].map((l) => r.colourOf.get(l)!.join());
    expect(new Set(cs).size).toBe(3);
  });

  it('share one number range', () => {
    expect(groupRange([{ key: 'k', range: [0, 2] }, { key: 'k', range: [-1, 1] }, { key: 'k' }])).toEqual([-1, 2]);
  });

  it('give one legend with the counts added up', () => {
    const leg = (labels: string[], counts: number[]): ColourLegendData => ({
      kind: 'categories',
      name: 'celltype',
      labels,
      colours: labels.map(() => [1, 2, 3]),
      counts,
      order: labels.map((_, i) => i),
      extended: false,
      custom: labels.map(() => false),
    });
    const m = mergeLegends([leg(['T', 'B'], [5, 1]), undefined, leg(['NK', 'T'], [2, 3])])!;
    expect(m.kind).toBe('categories');
    if (m.kind !== 'categories') return;
    // the first legend's order, then what only later ones have (owner, 2026-10-15)
    expect(m.order.map((i) => [m.labels[i], m.counts[i]])).toEqual([
      ['T', 8],
      ['B', 1],
      ['NK', 2],
    ]);
  });
});

describe('a group offers what any member has (review full-2 C-H1)', () => {
  const meta = (cols: string[]) =>
    ({ obs: cols.map((name) => ({ name, kind: 'categorical', categories: ['a', 'b'] })) }) as unknown as import('../api/types').DatasetMeta;

  it('columns of all members, with how many have each', () => {
    const u = unionColumns([meta(['cell_type', 'leiden']), meta(['cell_type']), undefined]);
    expect(u.map((x) => [x.column.name, x.k])).toEqual([
      ['cell_type', 2],
      ['leiden', 1],
    ]);
  });

  it('genes of all members once, symbols kept', () => {
    const g = mergeGeneLists([
      { names: ['ENSG1', 'ENSG2'], symbols: ['CD3E', 'MS4A1'], symbol_column: 'gene_symbols', duplicates: 0, matrices: [] },
      { names: ['ENSG2', 'Cd8a'], symbols: null, symbol_column: null, duplicates: 0, matrices: [] },
    ]);
    expect(g.names).toEqual(['ENSG1', 'ENSG2', 'Cd8a']);
    expect(g.symbols).toEqual(['CD3E', 'MS4A1', 'Cd8a']);
  });
});
