import { describe, expect, it } from 'vitest';

import { hexToRgb } from '../colour/palettes';
import { groupParams, membersOf, restoreViews, tilesTaken, useWorkspace } from '../state/workspace';
import type { ColourLegendData } from './colouring';
import { normaliseParams } from './embedding';
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

  it('count as one tile; a group left with one view becomes independent', () => {
    const ws = useWorkspace.getState();
    ws.reset();
    ws.addView(base('x'));
    ws.addGroup([base('a'), base('b')]);
    let views = useWorkspace.getState().views;
    expect(tilesTaken(views)).toBe(2);
    expect(membersOf(views, views[1]).map((v) => v.datasetId)).toEqual(['a', 'b']);
    useWorkspace.getState().removeView(views[1].id);
    views = useWorkspace.getState().views;
    expect(views.map((v) => [v.datasetId, v.group])).toEqual([
      ['x', undefined],
      ['b', undefined],
    ]);
  });

  it('close together', () => {
    const ws = useWorkspace.getState();
    ws.reset();
    ws.addView(base('x'));
    const g = ws.addGroup([base('a'), base('b')])!;
    useWorkspace.getState().removeGroup(g);
    expect(useWorkspace.getState().views.map((v) => v.datasetId)).toEqual(['x']);
  });

  it('are restored together, beyond the 9 tiles in views', () => {
    const many = Array.from({ length: 12 }, (_, i) => ({ id: `v${i}`, ...base(`d${i}`), group: 'g1' }));
    const restored = restoreViews([...many, { id: 'solo', ...base('s'), group: 'g2' }]);
    expect(restored).toHaveLength(13);
    expect(restored.filter((v) => v.group === 'g1')).toHaveLength(12);
    expect(restored.at(-1)!.group).toBeUndefined(); // a group of one
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
    expect(m.order.map((i) => [m.labels[i], m.counts[i]])).toEqual([
      ['B', 1],
      ['NK', 2],
      ['T', 8],
    ]);
  });
});
