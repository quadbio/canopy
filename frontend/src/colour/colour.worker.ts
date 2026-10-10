// Colour computation off the main thread: millions of points would block interaction.
import { computeColours, type ColourSpec } from './compute';

self.onmessage = (e: MessageEvent<{ id: number; spec: ColourSpec }>) => {
  const { id, spec } = e.data;
  const t0 = performance.now();
  try {
    const result = computeColours(spec);
    (self as unknown as Worker).postMessage({ id, result, ms: performance.now() - t0 }, [result.rgba.buffer]);
  } catch (err) {
    (self as unknown as Worker).postMessage({ id, error: String(err) });
  }
};
