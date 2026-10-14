import { describe, expect, it } from 'vitest';

import { guardTiles, outsideImage } from './tiles';

describe('image tiles', () => {
  const level = () => ({
    shape: [3, 1000, 600],
    labels: ['c', 'y', 'x'],
    tileSize: 512,
    reads: 0,
    errors: [] as Error[],
    getTile(this: { reads: number }, props: { x: number; y: number }) {
      this.reads += props.x >= 0 ? 1 : 0;
      return Promise.resolve('tile');
    },
    onTileError(this: { errors: Error[] }, e: Error) {
      this.errors.push(e);
    },
  });

  it('past the right or bottom edge are outside the image', () => {
    const s = level();
    expect(outsideImage(s, 1, 1)).toBe(false); // x 512..600, y 512..1000
    expect(outsideImage(s, 2, 0)).toBe(true); // starts at x 1024 > 600 (Viv let it through)
    expect(outsideImage(s, 0, 2)).toBe(true);
    expect(outsideImage(s, -1, 0)).toBe(true);
  });

  it('outside are refused without a read, and quietly', async () => {
    const [s] = guardTiles([level()]);
    await expect(s.getTile({ x: 0, y: 0 } as never)).resolves.toBe('tile');
    let refused: Error | null = null;
    await s.getTile({ x: 5, y: 0 } as never).catch((e: Error) => (refused = e));
    expect(s.reads).toBe(1);
    s.onTileError(refused!);
    expect(s.errors).toHaveLength(0);
    s.onTileError(new Error('other'));
    expect(s.errors).toHaveLength(1);
  });
});
