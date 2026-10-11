import { describe, expect, it } from 'vitest';

import { FULL_LIMIT, useLod, visibleShare } from './lod';

describe('level of detail', () => {
  it('measures the share of the data on screen', () => {
    const b: [number, number, number, number] = [0, 0, 100, 100];
    expect(visibleShare(b, 500, 500, 2)).toBe(1); // 125 x 125 data units visible: all of it
    expect(visibleShare(b, 500, 500, 3)).toBeCloseTo(0.390625); // 62.5 x 62.5
  });

  it('updates in powers of two only, so zooming re-renders views rarely', () => {
    const { setVisible } = useLod.getState();
    setVisible('v', 0.3);
    expect(useLod.getState().visible.v).toBe(0.5);
    const before = useLod.getState().visible;
    setVisible('v', 0.4); // same step: no new state
    expect(useLod.getState().visible).toBe(before);
    setVisible('v', 0.2);
    expect(useLod.getState().visible.v).toBe(0.25);
    expect(FULL_LIMIT).toBeGreaterThan(1_000_000);
  });
});
