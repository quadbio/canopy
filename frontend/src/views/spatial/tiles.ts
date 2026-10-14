/** Viv reads a tile that starts past the image's right or bottom edge as an empty zarr slice,
 * which zarrita refuses ("Input contains an empty iterator") and Viv passes on as an unhandled
 * error (host test 3: two linked regions, one scale, the view reaching past the smaller image).
 * Such tiles are refused here before any read, and the refusal is not an error. */

interface TileSource {
  shape: number[];
  labels: string[];
  tileSize: number;
  getTile: (props: { x: number; y: number; [k: string]: unknown }) => Promise<unknown>;
  onTileError: (err: Error) => void;
}

class OutsideImage extends Error {}

export function outsideImage(src: Pick<TileSource, 'shape' | 'labels' | 'tileSize'>, x: number, y: number): boolean {
  const width = src.shape[src.labels.indexOf('x')];
  const height = src.shape[src.labels.indexOf('y')];
  return x < 0 || y < 0 || x * src.tileSize >= width || y * src.tileSize >= height;
}

/** Each level of an image, with tiles outside it refused quietly. */
export function guardTiles<T>(levels: T[]): T[] {
  for (const level of levels) {
    const src = level as unknown as TileSource;
    if (typeof src.getTile !== 'function' || !Array.isArray(src.labels)) continue;
    const getTile = src.getTile.bind(src);
    const onTileError = src.onTileError.bind(src);
    src.getTile = (props) => (outsideImage(src, props.x, props.y) ? Promise.reject(new OutsideImage()) : getTile(props));
    src.onTileError = (err) => {
      if (!(err instanceof OutsideImage)) onTileError(err);
    };
  }
  return levels;
}
