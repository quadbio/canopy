// One tab of this browser saves the workspace (review full-2 C-H2: two tabs overwrote each other's
// saved views). A tab opened while another is open asks over a BroadcastChannel; if one answers,
// it does not save and says so, until the user chooses to save from it (the other then stops).
// Remembered settings are merged entry by entry when written (`mergeSaved`), so a tab that saves
// keeps what another wrote meanwhile.

import { create } from 'zustand';

interface Tabs {
  /** This tab writes the workspace to browser storage. */
  saving: boolean;
  /** Another tab of this browser has Canopy open (and saves). */
  other: boolean;
  takeOver: () => void;
}

const ID = Math.random().toString(36).slice(2);
const channel: BroadcastChannel | null = typeof BroadcastChannel === 'function' ? new BroadcastChannel('canopy-tabs') : null;

type Message = { kind: 'hello' | 'here' | 'take'; id: string };

export const useTabs = create<Tabs>()((set) => ({
  saving: true,
  other: false,
  takeOver: () => {
    channel?.postMessage({ kind: 'take', id: ID } satisfies Message);
    set({ saving: true, other: true });
  },
}));

if (channel) {
  channel.onmessage = (e: MessageEvent<Message>) => {
    const m = e.data;
    if (!m || m.id === ID) return;
    if (m.kind === 'hello' && useTabs.getState().saving) {
      // a newer tab asks: this one keeps saving, the newer one waits
      channel.postMessage({ kind: 'here', id: ID } satisfies Message);
      useTabs.setState({ other: true });
    } else if (m.kind === 'here') {
      useTabs.setState({ saving: false, other: true });
    } else if (m.kind === 'take') {
      useTabs.setState({ saving: false, other: true });
    }
  };
  channel.postMessage({ kind: 'hello', id: ID } satisfies Message);
}

export function tabSaves(): boolean {
  return useTabs.getState().saving;
}

interface Saved {
  state?: { remembered?: Record<string, { at?: number }> };
}

/** The workspace JSON about to be written, with remembered settings that another tab wrote
 * later kept (newest per dataset wins). */
export function mergeSaved(next: string, stored: string | null): string {
  if (!stored) return next;
  try {
    const a = JSON.parse(next) as Saved;
    const b = JSON.parse(stored) as Saved;
    const mine = a.state?.remembered;
    const theirs = b.state?.remembered;
    if (!mine || !theirs) return next;
    for (const [k, v] of Object.entries(theirs)) {
      if ((v?.at ?? 0) > (mine[k]?.at ?? 0)) mine[k] = v;
    }
    return JSON.stringify(a);
  } catch {
    return next;
  }
}
