import { describe, expect, it } from 'vitest';

import { legendMatches } from './colouring';

describe('legend search (issue #2)', () => {
  // legend order differs from index order; category 3 has no cells here
  const data = { labels: ['T cell', 'B cell', 'NK cell', 'Tregs', 'Monocyte'], order: [4, 0, 3, 1, 2], counts: [5, 4, 3, 0, 2] };

  it('finds categories containing the text, ignoring case, in legend order', () => {
    expect(legendMatches(data, 'cell')).toEqual([0, 1, 2]);
    expect(legendMatches(data, '  T ')).toEqual([4, 0]); // Monocyte, T cell
    expect(legendMatches(data, 'treg')).toEqual([]); // Tregs has no cells: not listed
    expect(legendMatches(data, 'MONO')).toEqual([4]);
    expect(legendMatches(data, 'xyz')).toEqual([]);
  });

  it('lists every present category, past the first 200 rows', () => {
    const n = 500;
    const big = { labels: Array.from({ length: n }, (_, i) => `c${i}`), order: [...Array(n).keys()], counts: new Array(n).fill(1) };
    expect(legendMatches(big, 'c4').length).toBe(111); // c4, c40–c49, c400–c499
  });
});
