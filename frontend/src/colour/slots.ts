// Colours by name: each category label gets a palette slot the first time it is shown, and keeps
// it, so "T cell" has the same colour in every view and dataset (with the same palette).
// Within one column, labels never share a slot while free slots remain.

export type SlotTable = ReadonlyMap<string, number>;

/** At most this many labels are remembered; the oldest are forgotten first. */
export const MAX_SLOTS = 5000;

/**
 * Slots for `labels` (in natural order first, so new labels fill slots in reading order).
 * Returns the slot per label (same order as `labels`) and the labels that got a new slot.
 */
export function assignSlots(
  labels: readonly string[],
  order: readonly number[],
  known: SlotTable,
  paletteSize: number,
): { slots: number[]; added: [string, number][] } {
  const slots = new Array<number>(labels.length).fill(-1);
  const taken = new Set<number>();
  // 1. Known labels keep their slot unless another label of this column already has it.
  for (const i of order) {
    const s = known.get(labels[i]);
    if (s !== undefined && s < paletteSize && !taken.has(s)) {
      slots[i] = s;
      taken.add(s);
    }
  }
  // 2. The others take the lowest free slots; when none are left, colours repeat.
  const added: [string, number][] = [];
  let next = 0;
  let wrap = 0;
  for (const i of order) {
    if (slots[i] >= 0) continue;
    while (next < paletteSize && taken.has(next)) next += 1;
    const s = next < paletteSize ? next : wrap++ % paletteSize;
    if (next < paletteSize) {
      taken.add(s);
      next += 1;
    }
    slots[i] = s;
    if (!known.has(labels[i])) added.push([labels[i], s]);
  }
  return { slots, added };
}

/** `known` plus `added` (existing entries are kept), trimmed to MAX_SLOTS (oldest first out). */
export function mergeSlots(known: readonly [string, number][], added: readonly [string, number][]): [string, number][] {
  const m = new Map(known);
  for (const [k, v] of added) if (!m.has(k)) m.set(k, v);
  const all = [...m.entries()];
  return all.length > MAX_SLOTS ? all.slice(all.length - MAX_SLOTS) : all;
}
