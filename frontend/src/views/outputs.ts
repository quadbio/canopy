// Each view publishes its layers here; the single canvas reads them all.
import { create } from 'zustand';

import type { ViewOutput } from './registry';

interface Outputs {
  outputs: Record<string, ViewOutput>;
  publish: (id: string, out: ViewOutput) => void;
  drop: (id: string) => void;
}

export const useViewOutputs = create<Outputs>()((set) => ({
  outputs: {},
  publish: (id, out) => set((s) => ({ outputs: { ...s.outputs, [id]: out } })),
  drop: (id) =>
    set((s) => {
      const rest = { ...s.outputs };
      delete rest[id];
      return { outputs: rest };
    }),
}));
