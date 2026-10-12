import { describe, expect, it } from 'vitest';

import type { OrthoState } from './layout';
import { fitLayout, linkedStates, panelRects } from './panels';
import { ownerOf, panelKey, panelLayerId, panelsOf } from './registry';

describe('panels', () => {
  it('lay out below the title and left of the legend; one panel fills the cell', () => {
    const cell = { x: 10, y: 20, width: 830, height: 630 };
    expect(panelRects(1, cell, { top: 30, right: 200 })).toEqual([cell]);
    const r = panelRects(4, cell, { top: 30, right: 200 }, 0);
    expect(r[0]).toEqual({ x: 10, y: 50, width: 315, height: 300 });
    expect(r[3].x + r[3].width).toBeLessThanOrEqual(10 + 630);
  });

  it('follow the panel that moved, relative to their own fit', () => {
    const fitted: Record<string, OrthoState> = { a: { target: [0, 0, 0], zoom: 1 }, b: { target: [100, 0, 0], zoom: 3 } };
    const next = { target: [5, -3, 0] as [number, number, number], zoom: 2 };
    // a: one step in, 5 units = 20 px right; b: one step in too, 20 px = 1.25 units at zoom 4
    expect(linkedStates('pan+zoom', 'a', next, { ...fitted }, fitted).b).toEqual({ target: [101.25, -0.75, 0], zoom: 4 });
    expect(linkedStates('zoom', 'a', next, { ...fitted }, fitted).b).toEqual({ target: [100, 0, 0], zoom: 4 });
    expect(Object.keys(linkedStates('none', 'a', next, { ...fitted }, fitted))).toEqual(['a']);
  });

  it('name layers by view and panel', () => {
    expect(ownerOf(panelLayerId('v1', 'c3', 'points'))).toEqual({ view: 'v1', panel: 'c3' });
    expect(ownerOf(`${panelKey('v1', 'main')}:points-sub`)).toEqual({ view: 'v1', panel: 'main' });
    expect(panelsOf({ layers: [], bounds: null, status: 'ready' })[0].id).toBe('main');
  });
});

describe('fitLayout', () => {
  const rects = [
    { x: 0, y: 0, width: 200, height: 200 },
    { x: 204, y: 0, width: 200, height: 200 },
  ];
  const base = { keys: ['v/a', 'v/b'], rects, fitInsets: {}, flipY: false };
  const bounds: [number, number, number, number][] = [
    [0, 0, 10, 10],
    [100, 100, 140, 140],
  ];

  it('same coordinates: every panel shows the union of the data at one view state', () => {
    const f = fitLayout({ ...base, bounds, sameScale: true, sameCoordinates: true });
    expect(f['v/a']).toEqual(f['v/b']);
    expect(f['v/a'].target[0]).toBeCloseTo(70);
  });

  it('same scale: own centres, one zoom (the smallest); fill: own zooms', () => {
    const s = fitLayout({ ...base, bounds, sameScale: true, sameCoordinates: false });
    expect(s['v/a'].zoom).toBe(s['v/b'].zoom);
    expect(s['v/a'].target[0]).toBeCloseTo(5);
    expect(s['v/b'].target[0]).toBeCloseTo(120);
    const f = fitLayout({ ...base, bounds, sameScale: false, sameCoordinates: false });
    expect(f['v/a'].zoom).toBeGreaterThan(f['v/b'].zoom);
  });
});
