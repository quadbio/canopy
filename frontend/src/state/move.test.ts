import { describe, expect, it } from 'vitest';

import { gridCells, tileCells } from '../views/layout';
import { movable, movedViews, placeOf, useWorkspace } from './workspace';

// issue #1: views moved in the grid, and a fixed number of columns
const v = (id: string, extra: { group?: string; follows?: string } = {}) => ({
  id,
  group: extra.group,
  params: extra.follows ? { follows: extra.follows } : {},
});
const ids = (vs: { id: string }[]) => vs.map((x) => x.id).join(',');

describe('moving views', () => {
  const views = [v('a'), v('b'), v('p', { follows: 'b' }), v('c'), v('d')];

  it('a dropped view takes the target place; the others shift', () => {
    expect(ids(movedViews(views, 'a', placeOf(views, 'a', 'c')!))).toBe('b,p,c,a,d'); // forward: after c
    expect(ids(movedViews(views, 'd', placeOf(views, 'd', 'b')!))).toBe('a,d,b,p,c'); // back: before b
    expect(placeOf(views, 'c', 'd')).toBeNull(); // to the end
    expect(ids(movedViews(views, 'c', null))).toBe('a,b,p,d,c');
  });

  it('a plot below a view moves with it and is not a place of its own', () => {
    expect(movable(views, 'a')).toEqual(['a', 'b', 'c', 'd']);
    expect(ids(movedViews(views, 'b', placeOf(views, 'b', 'd')!))).toBe('a,c,d,b,p');
  });

  it('keyboard: one step left or right; nothing past the ends', () => {
    expect(ids(movedViews(views, 'c', placeOf(views, 'c', -1)!))).toBe('a,c,b,p,d');
    expect(ids(movedViews(views, 'c', placeOf(views, 'c', 1)!))).toBe('a,b,p,d,c');
    expect(placeOf(views, 'a', -1)).toBeUndefined();
    expect(placeOf(views, 'd', 1)).toBeUndefined();
  });

  it('within a linked group only', () => {
    const g = [v('x', { group: 'g' }), v('y', { group: 'g' }), v('z', { group: 'g' })];
    expect(ids(movedViews(g, 'z', placeOf(g, 'z', 'x')!))).toBe('z,x,y');
    expect(ids(movedViews(g, 'x', null))).toBe('y,z,x');
    expect(ids(movedViews([...g, v('o')], 'o', 'x'))).toBe('x,y,z,o'); // across: unchanged
  });
});

describe('fixed columns', () => {
  it('keeps the number of columns whatever the number of views', () => {
    expect(gridCells(4, 300, 200, 0).filter((c) => c.y === 0)).toHaveLength(2); // auto: 2 × 2
    expect(gridCells(4, 300, 200, 0, 3).filter((c) => c.y === 0)).toHaveLength(3);
    const six = gridCells(6, 300, 200, 0, 3);
    expect(six.map((c) => c.x)).toEqual([0, 100, 200, 0, 100, 200]); // one dataset per row
    expect(gridCells(2, 300, 200, 0, 3)[1].width).toBe(100); // the same tile size
  });

  it('applies inside a linked group too', () => {
    const vs = ['a', 'b', 'c', 'd'].map((id) => ({ id, follows: null, group: 'g' }));
    const { cells } = tileCells(vs, 400, 200, 0, 0, {}, 1);
    expect(cells.map((c) => c.x)).toEqual([0, 0, 0, 0]);
  });
});

describe('columns and an empty canvas', () => {
  const spec = { type: 'embedding', datasetId: 'd', table: null, title: 'd', params: {} };

  it('go back to Auto when the last view closes (closed one by one, or all at once)', () => {
    const ws = useWorkspace.getState();
    ws.reset();
    const a = ws.addView(spec)!;
    const b = ws.addView(spec)!;
    useWorkspace.getState().setColumns(3);
    useWorkspace.getState().removeView(a);
    expect(useWorkspace.getState().columns).toBe(3); // one view left: kept (the choice stays shown)
    useWorkspace.getState().removeView(b);
    expect(useWorkspace.getState().columns).toBeNull();
    useWorkspace.getState().addView(spec);
    useWorkspace.getState().setColumns(2);
    useWorkspace.getState().reset();
    expect(useWorkspace.getState().columns).toBeNull();
  });
});
