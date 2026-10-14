// Titles of a linked group's members: lab file names share long prefixes (2023…_sample_), and the
// end of a name is what tells datasets apart, so the shared prefix is left out and a long rest
// is shortened in the middle (review full-2 C-M5). The full name stays in the tooltip and label.

const KEEP = 28;

/** The part of `titles` they all start with, cut back to a separator (_ - . space). */
export function sharedPrefix(titles: string[]): string {
  if (titles.length < 2) return '';
  let p = titles[0];
  for (const t of titles) while (!t.startsWith(p)) p = p.slice(0, -1);
  const cut = Math.max(p.lastIndexOf('_'), p.lastIndexOf('-'), p.lastIndexOf('.'), p.lastIndexOf(' '));
  return cut >= 3 ? p.slice(0, cut + 1) : '';
}

/** A member's title as shown: without the shared prefix, middle-shortened beyond KEEP. */
export function memberTitle(title: string, prefix: string): string {
  const rest = prefix && title.length > prefix.length ? `…${title.slice(prefix.length)}` : title;
  if (rest.length <= KEEP) return rest;
  const head = Math.ceil((KEEP - 1) * 0.4);
  return `${rest.slice(0, head)}…${rest.slice(rest.length - (KEEP - 1 - head))}`;
}
