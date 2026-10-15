// Colours of a linked group (S7b): the views of a group share their settings, and their colours
// must agree too. Each view publishes what its colouring found in its dataset (the column's
// categories and preset colours, or a number range); the group's colours come from all of them:
//
// - categories: the union of the views' categories. Preset colours (`uns['<col>_colors']`) of
//   any view are used when they agree: never one colour for two categories, never two colours
//   for one category (owner, S7 answer 5); categories without a preset colour get palette
//   colours not used yet. When they disagree, palette colours are made for the whole group.
//   Other palettes give each category of the union a colour by name (colour/slots.ts).
// - numbers: one range over all the views, so a colour means the same value everywhere.
// A view whose dataset lacks the column (or gene) colours its cells as missing (NA).

import { create } from 'zustand';

import {
  categoryColours,
  coloursExtended,
  hexToRgb,
  normaliseHex,
  type PaletteName,
  paletteSize,
  resolvePalette,
  type RGB,
  rgbToHex,
} from '../colour/palettes';
import { assignSlots } from '../colour/slots';
import type { ColourLegendData } from './colouring';

/** What one view's colouring found in its dataset, for the colouring `key`. */
export interface LinkedEntry {
  key: string;
  labels?: string[];
  given?: string[] | null;
  range?: [number, number];
}

interface LinkedStore {
  entries: Record<string, Record<string, LinkedEntry>>; // group → view → entry
  publish: (group: string, view: string, entry: LinkedEntry | null) => void;
}

// Publishes are applied together once per frame: while a group of N views loads, each would
// otherwise change the group's scheme and rebuild every view's colours, N² times (H3 B-L3).
let pending: { group: string; view: string; entry: LinkedEntry | null }[] = [];
let scheduled = false;
const nextFrame = (f: () => void) =>
  typeof requestAnimationFrame === 'function' ? requestAnimationFrame(f) : setTimeout(f, 0);

export const useLinked = create<LinkedStore>()((set) => ({
  entries: {},
  publish: (group, view, entry) => {
    pending.push({ group, view, entry });
    if (scheduled) return;
    scheduled = true;
    nextFrame(() => {
      const batch = pending;
      pending = [];
      scheduled = false;
      set((s) => {
        const entries = { ...s.entries };
        for (const { group: g, view: v, entry: e } of batch) {
          const views = { ...(entries[g] ?? {}) };
          if (e) views[v] = e;
          else delete views[v];
          entries[g] = views;
          if (!Object.keys(views).length) {
            delete entries[g]; // a closed group (B-L6), and its schemes (H3 B-L3)
            for (const k of [...lastScheme.keys()]) if (k.startsWith(`${g}|`)) lastScheme.delete(k);
          }
        }
        return { entries };
      });
    });
  },
}));


/** The categories of all views (natural order) and their preset colours, if those agree. */
export function groupCategories(entries: LinkedEntry[]): {
  labels: string[];
  given: Map<string, string> | null;
  conflict: boolean;
} {
  const seen = new Set<string>();
  const byLabel = new Map<string, string>();
  const byColour = new Map<string, string>();
  let conflict = false;
  let anyGiven = false;
  for (const e of entries) {
    if (!e.labels) continue;
    for (const l of e.labels) seen.add(l);
    if (!e.given || e.given.length !== e.labels.length) continue;
    anyGiven = true;
    e.labels.forEach((l, i) => {
      const hex = normaliseHex(e.given![i]) ?? e.given![i].toLowerCase();
      const had = byLabel.get(l);
      const owner = byColour.get(hex);
      if ((had !== undefined && had !== hex) || (owner !== undefined && owner !== l)) conflict = true;
      byLabel.set(l, hex);
      byColour.set(hex, l);
    });
  }
  // the first dataset's order, then categories only later datasets have, in theirs
  const labels = [...seen];
  return { labels, given: anyGiven && !conflict ? byLabel : null, conflict };
}

export interface GroupColours {
  colourOf: Map<string, RGB>;
  /** More categories than the palette's colours: in-between colours added. */
  extended: boolean;
  /** Preset colours disagreed between the views: palette colours instead. */
  regenerated: boolean;
}

/** A colour for every category of the group. */
export function groupColours(entries: LinkedEntry[], palette: PaletteName): GroupColours {
  const { labels, given, conflict } = groupCategories(entries);
  const order = labels.map((_, i) => i); // already in natural order
  const usePreset = (palette === 'auto' || palette === 'dataset') && given !== null;
  if (usePreset) {
    const colourOf = new Map<string, RGB>();
    const used = new Set<string>();
    for (const l of labels) {
      const hex = given!.get(l);
      if (hex) {
        colourOf.set(l, hexToRgb(hex));
        used.add(hex);
      }
    }
    const rest = labels.filter((l) => !colourOf.has(l));
    if (rest.length) {
      // categories without a preset colour: palette colours not used by the preset ones
      const p = labels.length > 54 ? 'many' : 'default';
      const list = categoryColours(p, assignSlots(labels, order, paletteSize(p, labels.length)), null);
      const free = list.filter((c) => !used.has(rgbToHex(c)));
      rest.forEach((l, k) => colourOf.set(l, free[k % Math.max(free.length, 1)] ?? list[0]));
    }
    return { colourOf, extended: false, regenerated: false };
  }
  // palettes by name over the union; "dataset" without agreeing colours falls back to them
  const resolved = resolvePalette(palette === 'dataset' ? 'auto' : palette, labels.length, null);
  const p = resolved === 'dataset' ? 'default' : resolved;
  const colours = categoryColours(p, assignSlots(labels, order, paletteSize(p, labels.length)), null);
  return {
    colourOf: new Map(labels.map((l, i) => [l, colours[i]])),
    extended: coloursExtended(p, labels.length),
    regenerated: conflict && (palette === 'auto' || palette === 'dataset'),
  };
}

/** One range over the views' ranges. */
export function groupRange(entries: LinkedEntry[]): [number, number] | null {
  const rs = entries.map((e) => e.range).filter((r): r is [number, number] => !!r);
  if (!rs.length) return null;
  return [Math.min(...rs.map((r) => r[0])), Math.max(...rs.map((r) => r[1]))];
}

/** The legend of a linked group: its views' legends together (categories with their counts
 * added up; numbers over one range). The views' colours already agree. */
export function mergeLegends(
  legends: (ColourLegendData | undefined)[],
  na?: { cells: number; datasets: number },
): ColourLegendData | undefined {
  const ls = legends.filter((l): l is ColourLegendData => !!l);
  if (!ls.length) return undefined;
  const first = ls[0];
  if (first.kind === 'numeric') {
    const nums = ls.filter((l): l is Extract<ColourLegendData, { kind: 'numeric' }> => l.kind === 'numeric' && l.name === first.name);
    return { ...first, range: [Math.min(...nums.map((l) => l.range[0])), Math.max(...nums.map((l) => l.range[1]))] };
  }
  const cats = ls.filter((l): l is Extract<ColourLegendData, { kind: 'categories' }> => l.kind === 'categories' && l.name === first.name);
  const at = new Map<string, number>();
  const labels: string[] = [];
  const colours: RGB[] = [];
  const counts: number[] = [];
  const custom: boolean[] = [];
  for (const l of cats) {
    l.labels.forEach((label, i) => {
      let k = at.get(label);
      if (k === undefined) {
        k = labels.length;
        at.set(label, k);
        labels.push(label);
        colours.push(l.colours[i]);
        counts.push(0);
        custom.push(false);
      }
      counts[k] += l.counts[i];
      custom[k] = custom[k] || l.custom[i];
    });
  }
  return {
    kind: 'categories',
    name: first.name,
    labels,
    colours,
    counts,
    order: labels.map((_, i) => i), // the order the members' categories come in (as one legend)
    extended: cats.some((l) => l.extended),
    custom,
    ...(cats.some((l) => l.regenerated) ? { regenerated: true } : {}),
    ...(na && na.datasets ? { na } : {}),
  };
}

export interface GroupScheme {
  colours: GroupColours | null;
  range: [number, number] | null;
}

// Per state of a group's entries: its schemes, made once for all of its views (review B-M3).
const perEntries = new WeakMap<object, Map<string, GroupScheme>>();
// The last scheme handed out per group, colouring and palette: given again while equal, so the
// views' colour tables and layers are not rebuilt when another view publishes the same facts.
const lastScheme = new Map<string, { sig: string; scheme: GroupScheme }>();

/** The colours and number range of a group for one colouring, the same object while unchanged. */
export function groupScheme(
  group: string,
  entries: Record<string, LinkedEntry> | undefined,
  key: string,
  palette: PaletteName,
): GroupScheme | null {
  if (!entries) return null;
  const id = `${group}|${key}|${palette}`;
  let cache = perEntries.get(entries);
  if (!cache) perEntries.set(entries, (cache = new Map()));
  const hit = cache.get(id);
  if (hit) return hit;
  const same = Object.values(entries).filter((e) => e.key === key);
  const scheme: GroupScheme = {
    colours: same.some((e) => e.labels) ? groupColours(same, palette) : null,
    range: groupRange(same),
  };
  const sig = JSON.stringify([
    scheme.range,
    scheme.colours ? [...scheme.colours.colourOf] : null,
    scheme.colours?.extended,
    scheme.colours?.regenerated,
  ]);
  const last = lastScheme.get(id);
  const out = last && last.sig === sig ? last.scheme : scheme;
  lastScheme.delete(id); // re-inserted: the map's order is the order of use
  lastScheme.set(id, { sig, scheme: out });
  // a group's colourings over a session (every gene viewed): only the recent ones are kept
  while (lastScheme.size > 200) lastScheme.delete(lastScheme.keys().next().value!);
  cache.set(id, out);
  return out;
}
