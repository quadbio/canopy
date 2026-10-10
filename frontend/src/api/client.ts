// Typed access to the dbx backend. Views never call fetch directly; they go through here.

import type { BinaryArray, TypedArray } from './types';

export interface Health {
  status: string;
  version: string;
}

export class ApiError extends Error {
  readonly code: string | null;

  constructor(
    readonly status: number,
    readonly path: string,
    readonly requestId: string | null,
    detail: unknown = null,
  ) {
    const d = detail as { code?: string; message?: string } | string | null;
    const message = typeof d === 'string' ? d : (d?.message ?? null);
    super(message ?? `API ${path} failed with ${status}${requestId ? ` (request ${requestId})` : ''}`);
    this.code = typeof d === 'object' && d ? (d.code ?? null) : null;
  }
}

type Params = Record<string, string | number | boolean | null | undefined>;

export function url(path: string, params?: Params): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params ?? {})) if (v !== null && v !== undefined) q.set(k, String(v));
  const s = q.toString();
  return s ? `${path}?${s}` : path;
}

async function fail(res: Response, path: string): Promise<never> {
  let detail: unknown = null;
  try {
    detail = ((await res.json()) as { detail?: unknown }).detail ?? null;
  } catch {
    // not JSON
  }
  throw new ApiError(res.status, path, res.headers.get('x-request-id'), detail);
}

export async function getJson<T>(path: string, fetchImpl: typeof fetch = fetch, init?: RequestInit): Promise<T> {
  const res = await fetchImpl(path, { ...init, headers: { accept: 'application/json', ...init?.headers } });
  if (!res.ok) return fail(res, path);
  return (await res.json()) as T;
}

export async function postJson<T>(path: string, body?: unknown, fetchImpl: typeof fetch = fetch): Promise<T | null> {
  const res = await fetchImpl(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) return fail(res, path);
  return res.status === 204 ? null : ((await res.json()) as T);
}

const ARRAYS: Record<string, new (b: ArrayBuffer) => TypedArray> = {
  float32: Float32Array,
  int8: Int8Array,
  int16: Int16Array,
  int32: Int32Array,
  uint8: Uint8Array,
};

/** A binary array endpoint: little-endian data described by x-dbx-dtype / x-dbx-shape. */
export async function getBinary(path: string, fetchImpl: typeof fetch = fetch, init?: RequestInit): Promise<BinaryArray> {
  const res = await fetchImpl(path, { ...init, headers: { accept: 'application/octet-stream', ...init?.headers } });
  if (!res.ok) return fail(res, path);
  const dtype = res.headers.get('x-dbx-dtype') ?? '';
  const Ctor = ARRAYS[dtype];
  if (!Ctor) throw new ApiError(res.status, path, res.headers.get('x-request-id'), `unknown dtype ${dtype}`);
  const shape = (res.headers.get('x-dbx-shape') ?? '').split(',').map(Number);
  const data = new Ctor(await res.arrayBuffer());
  if (data.length !== shape.reduce((a, b) => a * b, 1)) {
    throw new ApiError(res.status, path, res.headers.get('x-request-id'), 'array length does not match its shape');
  }
  return { data, shape, dtype, kind: res.headers.get('x-dbx-kind') };
}

export const getHealth = (fetchImpl?: typeof fetch) => getJson<Health>('/api/health', fetchImpl);
