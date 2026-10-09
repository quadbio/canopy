// Typed access to the dbx backend. Views never call fetch directly; they go through here.

export interface Health {
  status: string;
  version: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly path: string,
    readonly requestId: string | null,
  ) {
    super(`API ${path} failed with ${status}${requestId ? ` (request ${requestId})` : ''}`);
  }
}

export async function getJson<T>(path: string, fetchImpl: typeof fetch = fetch): Promise<T> {
  const res = await fetchImpl(path, { headers: { accept: 'application/json' } });
  if (!res.ok) throw new ApiError(res.status, path, res.headers.get('x-request-id'));
  return (await res.json()) as T;
}

export const getHealth = (fetchImpl?: typeof fetch) => getJson<Health>('/api/health', fetchImpl);
