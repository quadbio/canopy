// Promise interface to the colour worker; the newest request per key wins.
import type { ColourResult, ColourSpec } from './compute';
import { event } from '../telemetry/telemetry';

type Pending = { resolve: (r: ColourResult) => void; reject: (e: Error) => void; n: number };

let worker: Worker | null = null;
let nextId = 1;
const pending = new Map<number, Pending>();

function getWorker(): Worker {
  if (worker) return worker;
  worker = new Worker(new URL('./colour.worker.ts', import.meta.url), { type: 'module' });
  worker.onmessage = (e: MessageEvent<{ id: number; result?: ColourResult; error?: string; ms?: number }>) => {
    const p = pending.get(e.data.id);
    if (!p) return;
    pending.delete(e.data.id);
    if (e.data.result) {
      event('colours_computed', e.data.ms ?? null, { n: p.n });
      p.resolve(e.data.result);
    } else p.reject(new Error(e.data.error ?? 'colour worker failed'));
  };
  return worker;
}

export function computeColoursAsync(spec: ColourSpec): Promise<ColourResult> {
  const id = nextId++;
  const n = spec.kind === 'constant' ? spec.n : spec.kind === 'categorical' ? spec.codes.length : spec.values.length;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject, n });
    // Inputs are copied (not transferred): they stay in the query cache for the next colouring.
    getWorker().postMessage({ id, spec });
  });
}
