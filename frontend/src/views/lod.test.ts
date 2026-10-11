import { describe, expect, it } from 'vitest';

import { allowances, drawCount, representativeOrder, useLod, visibleShare, VERTEX_FACTOR } from './lod';

describe('level of detail', () => {
  it('measures the share of the data on screen', () => {
    const b: [number, number, number, number] = [0, 0, 100, 100];
    expect(visibleShare(b, 500, 500, 2)).toBe(1); // 125 x 125 data units visible: all of it
    expect(visibleShare(b, 500, 500, 3)).toBeCloseTo(0.390625); // 62.5 x 62.5
  });

  it('updates the visible share in powers of two only', () => {
    useLod.setState({ visible: {}, allowance: {} });
    const { setVisible } = useLod.getState();
    setVisible('v', 0.3);
    expect(useLod.getState().visible.v).toBe(0.5);
    const before = useLod.getState().visible;
    setVisible('v', 0.4);
    expect(useLod.getState().visible).toBe(before);
    useLod.getState().forget(['v']);
    expect(useLod.getState().visible.v).toBeUndefined();
  });

  it('shares the canvas budget in proportion to the cells, only when they do not all fit', () => {
    expect(allowances({ a: 300, b: 100 }, 1000)).toEqual({ a: Infinity, b: Infinity });
    const a = allowances({ a: 3_000_000, b: 1_000_000 }, 1_000_000);
    expect(a.a).toBe(750_000);
    expect(a.b).toBe(250_000);
  });

  it('draws more cells as fewer are on screen, never beyond the vertex cap', () => {
    const total = 3_000_000;
    expect(drawCount(total, 1, 1_000_000)).toBe(1_000_000); // all in view: the budget
    expect(drawCount(total, 0.5, 1_000_000)).toBe(1_500_000); // half in view: more, up to the cap
    expect(drawCount(1_200_000, 0.125, 1_000_000)).toBe(1_200_000); // a small part: all cells
    expect(drawCount(10_000_000, 0.01, 1_000_000)).toBe(VERTEX_FACTOR * 1_000_000);
    expect(drawCount(total, 1, Infinity)).toBe(total);
    // one small population shown, the rest hidden: all of it is drawn
    expect(drawCount(total, 1, 1_000_000, 0.025)).toBe(total);
  });

  it('orders the subset first, every cell exactly once, the same on every call', () => {
    const subset = Int32Array.from([1, 4, 7]);
    const order = representativeOrder(subset, 10);
    expect(new Set(order.slice(0, 3))).toEqual(new Set([1, 4, 7]));
    expect([...order].sort((x, y) => x - y)).toEqual([...Array(10).keys()]);
    expect([...representativeOrder(subset, 10)]).toEqual([...order]);
  });
});
