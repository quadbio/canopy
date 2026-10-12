import { describe, expect, it } from 'vitest';

import { log1pTicks, niceTicks, tickLabel } from './axes';

describe('axes', () => {
  it('nice ticks are round and cover the range', () => {
    expect(niceTicks(0, 10, 5)).toEqual([0, 2, 4, 6, 8, 10]);
    expect(niceTicks(-0.37, 1.21, 4)).toEqual([0, 0.5, 1]);
    expect(niceTicks(3, 3)).toEqual([3]);
    const t = niceTicks(12_345, 98_765, 5);
    expect(t[0]).toBeGreaterThanOrEqual(12_345);
    expect(t.at(-1)!).toBeLessThanOrEqual(98_765);
  });
  it('labels are short', () => {
    expect(tickLabel(1200)).toBe('1200');
    expect(tickLabel(35_000)).toBe('35k');
    expect(tickLabel(2_500_000)).toBe('2.5M');
    expect(tickLabel(0.05)).toBe('0.05');
  });
  it('log1p ticks are round values within the range', () => {
    const t = log1pTicks(Math.log1p(150));
    expect(t.map((x) => x.label)).toContain('100');
    expect(t.every((x) => x.at <= Math.log1p(150) + 1e-9)).toBe(true);
  });
});
