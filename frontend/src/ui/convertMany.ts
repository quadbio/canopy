// "Convert ticked" (owner, host test 3): conversions of many datasets asked at once. The server
// keeps at most `jobs.max_queued` waiting (refusing more with `queue_full`), so they are handed
// over in order: when the queue is full, again after RETRY_MS. Lasts for the page.

import type { QueryClient } from '@tanstack/react-query';
import { create } from 'zustand';

import { ApiError } from '../api/client';
import { jobs } from '../api/endpoints';
import { event } from '../telemetry/telemetry';

export const RETRY_MS = 10_000;

interface ConvertMany {
  /** Datasets not yet handed to the server, in order. */
  waiting: string[];
  /** Datasets the server refused (other than for a full queue), with its message. */
  failed: { id: string; message: string }[];
  add: (ids: string[], qc: QueryClient) => void;
  /** Forgets the datasets not yet handed over (those handed over convert on). */
  stop: () => void;
}

let timer: ReturnType<typeof setTimeout> | null = null;
let running = false;

export const useConvertMany = create<ConvertMany>()((set, get) => {
  const feed = async (qc: QueryClient) => {
    if (running) return;
    running = true;
    try {
      while (get().waiting.length) {
        const id = get().waiting[0];
        try {
          await jobs.convert(id);
          event('conversion_started', null, { x_source: 'X' });
        } catch (e) {
          if (e instanceof ApiError && e.code === 'queue_full') {
            timer = setTimeout(() => void feed(qc), RETRY_MS);
            return;
          }
          set((s) => ({ failed: [...s.failed, { id, message: e instanceof Error ? e.message : String(e) }] }));
        }
        set((s) => ({ waiting: s.waiting.filter((x) => x !== id) }));
        await qc.invalidateQueries({ queryKey: ['dataset', id] });
        await qc.invalidateQueries({ queryKey: ['jobs-active'] });
      }
    } finally {
      running = false;
    }
  };
  return {
    waiting: [],
    failed: [],
    add: (ids, qc) => {
      const have = new Set(get().waiting);
      set((s) => ({
        waiting: [...s.waiting, ...ids.filter((id) => !have.has(id))],
        failed: s.failed.filter((f) => !ids.includes(f.id)),
      }));
      if (timer) clearTimeout(timer);
      timer = null;
      void feed(qc);
    },
    stop: () => {
      if (timer) clearTimeout(timer);
      timer = null;
      set({ waiting: [] });
    },
  };
});
