// Typed access to the canopy backend. Views never call fetch directly; they go through here.

import { MISSING_VALUE } from '../colour/missing';
import type { BinaryArray, GeneVector, TypedArray } from './types';

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
    // Server errors: {"detail": {"code", "message"}, "request_id"} (see backend/canopy/errors.py).
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

/** Fired when the server refuses for lack of the access token (the app then explains). */
export const AUTH_REQUIRED_EVENT = 'canopy:auth-required';

async function fail(res: Response, path: string): Promise<never> {
  if (res.status === 401 && typeof window !== 'undefined') window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
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
  float64: Float64Array, // whole numbers beyond float32's exact range (ids)
  int8: Int8Array,
  int16: Int16Array,
  int32: Int32Array,
  uint8: Uint8Array,
};

/** A binary array endpoint: little-endian data described by x-canopy-dtype / x-canopy-shape. */
export async function getBinary(path: string, fetchImpl: typeof fetch = fetch, init?: RequestInit): Promise<BinaryArray> {
  const res = await fetchImpl(path, { ...init, headers: { accept: 'application/octet-stream', ...init?.headers } });
  if (!res.ok) return fail(res, path);
  const dtype = res.headers.get('x-canopy-dtype') ?? '';
  const Ctor = ARRAYS[dtype];
  if (!Ctor) throw new ApiError(res.status, path, res.headers.get('x-request-id'), `unknown dtype ${dtype}`);
  const shape = (res.headers.get('x-canopy-shape') ?? '').split(',').map(Number);
  const data = new Ctor(await res.arrayBuffer());
  if (data.length !== shape.reduce((a, b) => a * b, 1)) {
    throw new ApiError(res.status, path, res.headers.get('x-request-id'), 'array length does not match its shape');
  }
  return { data, shape, dtype, kind: res.headers.get('x-canopy-kind') };
}

export const getHealth = (fetchImpl?: typeof fetch) => getJson<Health>('/api/health', fetchImpl);

/** A gene reply (backend canopy/data/genes.py): sparse (uint32 cell indices, then values) or dense;
 * float32, or uint16 codes with value = offset + code * scale (65535 = missing). Decoded straight
 * into the form the GPU takes: missing values as MISSING_VALUE (see colour/missing.ts). */
export function decodeGene(buf: ArrayBuffer, header: (name: string) => string | null): GeneVector {
  const num = (k: string) => Number(header(`x-canopy-${k}`) ?? 'NaN');
  const n = num('n');
  const nnz = num('nnz');
  const sparse = header('x-canopy-encoding') === 'sparse';
  const count = sparse ? nnz : n;
  const off = sparse ? nnz * 4 : 0;
  let vals: Float32Array;
  if (header('x-canopy-dtype') === 'uint16') {
    const codes = new Uint16Array(buf, off, count);
    const offset = num('offset');
    const scale = num('scale');
    vals = new Float32Array(count);
    for (let k = 0; k < count; k += 1) vals[k] = codes[k] === 65535 ? MISSING_VALUE : offset + codes[k] * scale;
  } else {
    vals = new Float32Array(buf, off, count).slice();
    for (let k = 0; k < count; k += 1) if (Number.isNaN(vals[k])) vals[k] = MISSING_VALUE;
  }
  let values = vals;
  if (sparse) {
    const idx = new Uint32Array(buf, 0, nnz);
    values = new Float32Array(n);
    for (let k = 0; k < nnz; k += 1) values[idx[k]] = vals[k];
  }
  return { values, min: num('min'), max: num('max'), p99: num('p99'), nnz };
}

export async function getGene(path: string, init?: RequestInit): Promise<GeneVector> {
  const res = await fetch(path, { ...init, headers: { accept: 'application/octet-stream', ...init?.headers } });
  if (!res.ok) return fail(res, path);
  return decodeGene(await res.arrayBuffer(), (k) => res.headers.get(k));
}
