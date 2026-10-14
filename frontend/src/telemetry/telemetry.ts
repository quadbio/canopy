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

/** Fields that may carry strings: codes and short texts. The server accepts exactly these
 * (backend/canopy/telemetry.py; a test keeps both lists equal); others are dropped here. */
export const STRING_FIELDS = [
  'type',
  'mode',
  'status',
  'layer',
  'where',
  'caller',
  'build',
  'browser',
  'view',
  'dataset',
  'fit',
  'x_source',
  'gpu',
  'gpu_vendor',
  'gl_version',
  'message',
  'code',
  'kind',
] as const;
const STRINGS = new Set<string>(STRING_FIELDS);

/** Strings are reduced to the characters the server accepts (codes, not free text). */
export function code(s: string): string {
  return s.replace(/[^A-Za-z0-9_.:/ ()-]/g, '_').slice(0, 80) || '_';
}

/** A message without its quoted and parenthesised parts (names, ids) and addresses, as a code. */
export function messageCode(msg: string): string {
  return code(
    msg
      .replace(/\([^)]*\)/g, '()')
      .replace(/(["'`])[^"'`]*\1/g, '_')
      .replace(/https?:\/\/\S+/g, '_url_')
      .replace(/\s+/g, ' ')
      .trim(),
  );
}

/** Where an error was thrown: the top frame of its stack as `file:line:col` in the bundle. */
export function where(stack: string | undefined): string {
  return frames(stack)[0] ?? 'unknown';
}

/** The two frames below the top one (the top is often inside a library): what called it. */
export function caller(stack: string | undefined): string {
  const below = frames(stack).slice(1, 3);
  return below.length ? code(below.join(' ')) : 'unknown';
}

function frames(stack: string | undefined): string[] {
  const out: string[] = [];
  for (const line of (stack ?? '').split('\n')) {
    const m = /([^/\s(]+\.(?:js|ts|tsx|mjs)):(\d+):(\d+)/.exec(line);
    if (m) out.push(code(`${m[1]}:${m[2]}:${m[3]}`));
  }
  return out;
}

/** Browser family and major version, from the user agent (e.g. "Chrome 140"). */
export function browser(ua: string): string {
  for (const [name, re] of [
    ['Edge', /Edg\/(\d+)/],
    ['Firefox', /Firefox\/(\d+)/],
    ['Chrome', /Chrome\/(\d+)/],
    ['Safari', /Version\/(\d+).*Safari/],
  ] as const) {
    const m = re.exec(ua);
    if (m) return `${name} ${m[1]}`;
  }
  return 'other';
}

function clean(fields: Record<string, Value | null | undefined>): Record<string, Value> {
  const out: Record<string, Value> = {};
  for (const [k, v] of Object.entries(fields).slice(0, 24)) {
    if (v === null || v === undefined) continue;
    const key = k.toLowerCase().replace(/[^a-z0-9_]/g, '_').slice(0, 40);
    if (typeof v === 'string' && !STRINGS.has(key)) continue; // the server would drop it
    out[key] = typeof v === 'string' ? code(v) : v;
  }
  return out;
}

export function event(name: string, ms: number | null = null, fields: Record<string, Value | null | undefined> = {}) {
  queue.push({ name, ms: ms === null ? null : Math.round(ms * 100) / 100, fields: clean(fields) });
  performance.mark(`canopy:${name}`);
  // only our own marks are cleared (a test mode may set its own)
  const ours = performance.getEntriesByType('mark').filter((m) => m.name.startsWith('canopy:'));
  if (ours.length > 500) for (const m of ours.slice(0, ours.length - 100)) performance.clearMarks(m.name);
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
  const report = (name: string, e: unknown) => {
    const err = e as { name?: string; message?: string; stack?: string } | null;
    event(name, null, {
      type: code(err?.name ?? 'Error'),
      message: messageCode(String(err?.message ?? '')),
      where: where(err?.stack),
      caller: caller(err?.stack),
    });
  };
  window.addEventListener('error', (e) => report('js_error', e.error ?? { name: 'Error', message: e.message }));
  window.addEventListener('unhandledrejection', (e) => report('js_unhandled_rejection', e.reason));
  window.addEventListener('pagehide', () => flush(true));
}

/** Facts about this page load that explain what a log shows: browser, window, app build. */
export function pageFacts(): Record<string, Value> {
  return {
    browser: browser(navigator.userAgent),
    build: typeof __CANOPY_BUILD__ === 'string' ? __CANOPY_BUILD__ : 'unknown',
    viewport_w: window.innerWidth,
    viewport_h: window.innerHeight,
    dpr: window.devicePixelRatio,
  };
}

/** A deck.gl error as codes without names: the layer class and the message with the
 * parenthesised parts (layer ids, which hold element names) and quoted text removed. */
export function deckErrorFields(e: Error | null | undefined): Record<string, string> {
  const msg = String(e?.message ?? '');
  const layer = /\b([A-Z][A-Za-z0-9]*Layer)\b/.exec(msg)?.[1] ?? 'none';
  return { type: e?.name ?? 'Error', layer, message: messageCode(msg) };
}

export const sessionId = SESSION;
