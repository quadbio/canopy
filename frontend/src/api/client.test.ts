import { describe, expect, it } from 'vitest';

import { MISSING_VALUE } from '../colour/missing';
import { ApiError, decodeGene, getJson } from './client';

const fakeFetch = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  (async () => new Response(JSON.stringify(body), { status, headers })) as typeof fetch;

describe('getJson', () => {
  it('returns parsed JSON', async () => {
    expect(await getJson('/api/health', fakeFetch(200, { status: 'ok' }))).toEqual({ status: 'ok' });
  });

  it('throws ApiError with the request id on failure', async () => {
    const err = (await getJson('/api/x', fakeFetch(500, {}, { 'x-request-id': 'r1' })).catch(
      (e: unknown) => e,
    )) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.requestId).toBe('r1');
    expect(err.status).toBe(500);
  });
});

describe('decodeGene', () => {
  const headers = (h: Record<string, string>) => (k: string) => h[k] ?? null;

  it('expands sparse uint16 replies, with missing values as NaN', () => {
    const buf = new ArrayBuffer(3 * 4 + 3 * 2);
    new Uint32Array(buf, 0, 3).set([1, 4, 5]);
    new Uint16Array(buf, 12, 3).set([0, 65534, 65535]);
    const g = decodeGene(
      buf,
      headers({
        'x-canopy-encoding': 'sparse',
        'x-canopy-n': '6',
        'x-canopy-nnz': '3',
        'x-canopy-dtype': 'uint16',
        'x-canopy-offset': '1',
        'x-canopy-scale': String(1 / 65534),
        'x-canopy-max': '2',
        'x-canopy-min': '0',
        'x-canopy-p99': '2',
      }),
    );
    expect(Array.from(g.values.slice(0, 5))).toEqual([0, 1, 0, 0, 2]);
    expect(g.values[5]).toBe(MISSING_VALUE); // missing, in the GPU's form
    expect(g.nnz).toBe(3);
  });

  it('reads dense float32 replies as they are', () => {
    const buf = new Float32Array([0.5, 0, 2]).buffer;
    const g = decodeGene(buf, headers({ 'x-canopy-encoding': 'dense', 'x-canopy-n': '3', 'x-canopy-nnz': '2', 'x-canopy-dtype': 'float32' }));
    expect(Array.from(g.values)).toEqual([0.5, 0, 2]);
  });
});
