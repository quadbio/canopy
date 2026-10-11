// Colours by name: a category label's palette slot comes from a stable hash of the label, so
// "T cell" has the same colour in every view, dataset, browser and session (with the same
// palette), whatever was opened before. Within one column two labels never share a slot while
// free slots remain: on a collision the later label (in natural order) takes the next free slot.
// Nothing is stored, so there is nothing to grow, evict or reset.

/** FNV-1a, 32 bit: fast, stable across browsers and versions (part of the colour contract). */
export function hashLabel(label: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < label.length; i += 1) {
    h ^= label.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** Slot per label (same order as `labels`); `order` is the natural order used for collisions. */
export function assignSlots(labels: readonly string[], order: readonly number[], paletteSize: number): number[] {
  const slots = new Array<number>(labels.length).fill(-1);
  const taken = new Set<number>();
  for (const i of order) {
    let s = hashLabel(labels[i]) % paletteSize;
    if (taken.size < paletteSize) {
      while (taken.has(s)) s = (s + 1) % paletteSize;
    }
    taken.add(s);
    slots[i] = s;
  }
  return slots;
}
