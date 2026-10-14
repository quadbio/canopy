/** Shift-click ticking (owner, host test 3): a shift-click ticks (or unticks) every dataset of
 * the same list between the last ticked one and the one clicked, as in a file manager. */

let anchor: string | null = null;

/** The datasets a click on `id`'s tick changes: the range from the last clicked tick in the same
 * list (`list`: that list's ticks, in order) with shift, else just `id`. The click becomes the
 * next range's start. */
export function ticksFor(list: readonly string[], id: string, shift: boolean): string[] {
  const from = anchor;
  anchor = id;
  const a = from === null ? -1 : list.indexOf(from);
  const b = list.indexOf(id);
  if (!shift || a < 0 || b < 0) return [id];
  return list.slice(Math.min(a, b), Math.max(a, b) + 1);
}

/** Forgets the range's start: when the ticks are cleared (and for tests; review H3 B-L6). */
export function resetTickAnchor() {
  anchor = null;
}
