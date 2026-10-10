// Frontend telemetry: timing marks and environment facts sent to the backend log
// (POST /api/telemetry -> `client_event` lines), so a session on the host can be analysed from
// the server's log files alone. Only numbers and short codes are sent, never names or values.

type Value = number | boolean | string;
interface ClientEvent {
  name: string;
  ms: number | null;
  fields: Record<string, Value>;
}

const SESSION = Math.random().toString(36).slice(2, 10) + '-' + Date.now().toString(36);
const FLUSH_MS = 5000;
const queue: ClientEvent[] = [];
let timer: ReturnType<typeof setTimeout> | null = null;

/** Strings are reduced to the characters the server accepts (codes, not free text). */
export function code(s: string): string {
  return s.replace(/[^A-Za-z0-9_.:/ ()-]/g, '_').slice(0, 80) || '_';
}

function clean(fields: Record<string, Value | null | undefined>): Record<string, Value> {
  const out: Record<string, Value> = {};
  for (const [k, v] of Object.entries(fields).slice(0, 24)) {
    if (v === null || v === undefined) continue;
    out[k.toLowerCase().replace(/[^a-z0-9_]/g, '_').slice(0, 40)] = typeof v === 'string' ? code(v) : v;
  }
  return out;
}

export function event(name: string, ms: number | null = null, fields: Record<string, Value | null | undefined> = {}) {
  queue.push({ name, ms: ms === null ? null : Math.round(ms * 100) / 100, fields: clean(fields) });
  performance.mark(`dbx:${name}`);
  if (performance.getEntriesByType('mark').length > 500) performance.clearMarks();
  if (typeof window === 'undefined') return;
  if (queue.length >= 100) flush();
  else timer ??= setTimeout(flush, FLUSH_MS);
}

/** Time an async call and report it (with extra fields computed from the result). */
export async function timeAsync<T>(
  name: string,
  fn: () => Promise<T>,
  fields?: (r: T) => Record<string, Value | null | undefined>,
): Promise<T> {
  const t0 = performance.now();
  try {
    const r = await fn();
    event(name, performance.now() - t0, fields ? fields(r) : {});
    return r;
  } catch (e) {
    const err = e as { status?: number; code?: string | null; name?: string };
    if (err?.name !== 'AbortError') {
      event(`${name}_failed`, performance.now() - t0, { status: err?.status ?? 0, code: err?.code ?? 'error' });
    }
    throw e;
  }
}

export function flush(beacon = false) {
  if (timer) clearTimeout(timer);
  timer = null;
  if (!queue.length) return;
  const body = JSON.stringify({ session: SESSION, events: queue.splice(0, 200) });
  if (beacon && navigator.sendBeacon) {
    navigator.sendBeacon('/api/telemetry', new Blob([body], { type: 'application/json' }));
    return;
  }
  fetch('/api/telemetry', { method: 'POST', headers: { 'content-type': 'application/json' }, body, keepalive: true }).catch(
    () => undefined,
  );
}

/** Once per page: browser, screen and GPU facts that explain rendering speed. */
export function reportEnvironment(gl?: { renderer?: string; vendor?: string; version?: string } | null) {
  event('environment', null, {
    screen_w: window.screen.width,
    screen_h: window.screen.height,
    dpr: window.devicePixelRatio,
    cores: navigator.hardwareConcurrency ?? 0,
    memory_gb: (navigator as { deviceMemory?: number }).deviceMemory ?? 0,
    gpu: gl?.renderer ?? 'unknown',
    gpu_vendor: gl?.vendor ?? 'unknown',
    gl_version: gl?.version ?? 'unknown',
  });
}

export function installErrorReporting() {
  window.addEventListener('error', (e) => event('js_error', null, { type: code(e.error?.name ?? 'Error') }));
  window.addEventListener('unhandledrejection', (e) =>
    event('js_unhandled_rejection', null, { type: code((e.reason as Error)?.name ?? 'Error') }),
  );
  window.addEventListener('pagehide', () => flush(true));
}

export const sessionId = SESSION;
