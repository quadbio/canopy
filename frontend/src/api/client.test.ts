import { describe, expect, it } from 'vitest';

import { ApiError, getJson } from './client';

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
