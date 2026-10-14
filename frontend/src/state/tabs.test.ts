import { describe, expect, it } from 'vitest';

import { mergeSaved } from './tabs';

describe('saving from one tab (review full-2 C-H2)', () => {
  it('keeps remembered settings another tab wrote later', () => {
    const mine = JSON.stringify({ state: { views: [1], remembered: { a: { at: 5, params: { x: 1 } }, b: { at: 1, params: {} } } } });
    const theirs = JSON.stringify({ state: { views: [2], remembered: { b: { at: 9, params: { y: 2 } }, c: { at: 3, params: {} } } } });
    const out = JSON.parse(mergeSaved(mine, theirs)) as { state: { views: number[]; remembered: Record<string, { at: number }> } };
    expect(out.state.views).toEqual([1]); // the views are this tab's
    expect(out.state.remembered.a.at).toBe(5);
    expect(out.state.remembered.b.at).toBe(9); // newer there
    expect(out.state.remembered.c.at).toBe(3);
    expect(mergeSaved(mine, null)).toBe(mine);
    expect(mergeSaved(mine, 'not json')).toBe(mine);
  });
});
