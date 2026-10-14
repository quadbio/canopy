import { beforeEach, describe, expect, it } from 'vitest';

import { resetTickAnchor, ticksFor } from './ticks';

describe('shift-click ticking', () => {
  const list = ['a', 'b', 'c', 'd', 'e'];
  beforeEach(resetTickAnchor);

  it('ticks everything between the last click and a shift-click, either way', () => {
    expect(ticksFor(list, 'b', false)).toEqual(['b']);
    expect(ticksFor(list, 'e', true)).toEqual(['b', 'c', 'd', 'e']);
    expect(ticksFor(list, 'c', true)).toEqual(['c', 'd', 'e']); // from the last click (e)
  });

  it('is a single tick without shift, a first click, or a start in another list', () => {
    expect(ticksFor(list, 'c', true)).toEqual(['c']);
    expect(ticksFor(list, 'd', false)).toEqual(['d']);
    expect(ticksFor(['x', 'y'], 'y', true)).toEqual(['y']);
  });
});
