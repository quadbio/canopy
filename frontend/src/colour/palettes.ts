// Colour palettes and colour maps, as RGBA lookup tables for the GPU (see gpu.ts).
//
// Categorical palettes are indexed by a label's colour slot (slots.ts), so a label keeps its
// colour across views and datasets; "dataset" uses the dataset's own `uns[<col>_colors]`.
// Colour maps for numbers come from d3-scale-chromatic (the matplotlib maps, exactly).

import {
  interpolateCividis,
  interpolateGreys,
  interpolateInferno,
  interpolateMagma,
  interpolatePlasma,
  interpolateRdBu,
  interpolateTurbo,
  interpolateViridis,
} from 'd3-scale-chromatic';

export type RGB = [number, number, number];

// 54 colours: matplotlib's tab20 (dark tones first, then light), tab20b and tab20c, without the
// greys (grey means "missing") and without black (invisible on a dark canvas).
const TAB54: string[] = [
  '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#bcbd22', '#17becf',
  '#aec7e8', '#ffbb78', '#98df8a', '#ff9896', '#c5b0d5', '#c49c94', '#f7b6d2', '#dbdb8d', '#9edae5',
  '#393b79', '#5254a3', '#6b6ecf', '#9c9ede', '#637939', '#8ca252', '#b5cf6b', '#cedb9c', '#8c6d31',
  '#bd9e39', '#e7ba52', '#e7cb94', '#843c39', '#ad494a', '#d6616b', '#e7969c', '#7b4173', '#a55194',
  '#ce6dbd', '#de9ed6', '#3182bd', '#6baed6', '#9ecae1', '#c6dbef', '#e6550d', '#fd8d3c', '#fdae6b',
  '#fdd0a2', '#31a354', '#74c476', '#a1d99b', '#c7e9c0', '#756bb1', '#9e9ac8', '#bcbddc', '#dadaeb',
];

// Okabe & Ito (2008), without black: distinguishable with the common colour-vision deficiencies.
const OKABE_ITO: string[] = ['#e69f00', '#56b4e9', '#009e73', '#f0e442', '#0072b2', '#d55e00', '#cc79a7'];

/** Many distinct colours for many categories: hues by the golden angle, in three lightness
 * tiers, in OKLCH (so steps look even). Deterministic. */
function manyColours(n: number): RGB[] {
  const tiers: [number, number][] = [
    [0.68, 0.16],
    [0.5, 0.13],
    [0.82, 0.1],
  ];
  return Array.from({ length: n }, (_, i) => {
    const [l, c] = tiers[i % 3];
    const h = (i * 137.508) % 360;
    return oklchToRgb(l, c, h);
  });
}

function oklchToRgb(l: number, c: number, hDeg: number): RGB {
  const h = (hDeg * Math.PI) / 180;
  const a = c * Math.cos(h);
  const b = c * Math.sin(h);
  const l_ = (l + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m_ = (l - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s_ = (l - 0.0894841775 * a - 1.291485548 * b) ** 3;
  const lin = [
    4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
    -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
    -0.0041960863 * l_ - 0.7034186147 * m_ + 1.707614701 * s_,
  ];
  const srgb = lin.map((v) => {
    const x = Math.min(1, Math.max(0, v));
    return Math.round(255 * (x <= 0.0031308 ? 12.92 * x : 1.055 * x ** (1 / 2.4) - 0.055));
  });
  return srgb as RGB;
}

export function hexToRgb(hex: string): RGB {
  const h = hex.replace('#', '');
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

export const MISSING: RGB = [190, 190, 190];

// --- categorical -------------------------------------------------------------------------------

export const PALETTES = ['auto', 'dataset', 'default', 'colourblind', 'many'] as const;
export type PaletteName = (typeof PALETTES)[number];

export const PALETTE_LABELS: Record<PaletteName, string> = {
  auto: 'Automatic',
  dataset: 'From the dataset',
  default: 'Default (54 colours)',
  colourblind: 'Colour-blind safe (7)',
  many: 'Many categories (256)',
};

const MANY = manyColours(256);
const SLOT_PALETTES: Record<'default' | 'colourblind' | 'many', RGB[]> = {
  default: TAB54.map(hexToRgb),
  colourblind: OKABE_ITO.map(hexToRgb),
  many: MANY,
};

/** The palette actually used: "auto" is the dataset's colours when it has a full set, else the
 * default palette, or the many-colour palette when the default is too small. */
export function resolvePalette(
  name: PaletteName,
  n: number,
  given: string[] | null,
): 'dataset' | 'default' | 'colourblind' | 'many' {
  const hasGiven = !!given && given.length === n;
  if (name === 'dataset') return hasGiven ? 'dataset' : 'default';
  if (name !== 'auto') return name;
  if (hasGiven) return 'dataset';
  return n > TAB54.length ? 'many' : 'default';
}

export function paletteSize(p: 'default' | 'colourblind' | 'many'): number {
  return SLOT_PALETTES[p].length;
}

/** Colour of each category: by its slot, or (for "dataset") by its position. */
export function categoryColours(
  palette: 'dataset' | 'default' | 'colourblind' | 'many',
  slots: number[],
  given: string[] | null,
): RGB[] {
  if (palette === 'dataset' && given) return given.map(hexToRgb);
  const list = SLOT_PALETTES[palette === 'dataset' ? 'default' : palette];
  return slots.map((s) => list[s % list.length]);
}

/** True when the palette has to repeat colours for this many categories. */
export function coloursRepeat(palette: 'dataset' | 'default' | 'colourblind' | 'many', n: number): boolean {
  return palette !== 'dataset' && n > SLOT_PALETTES[palette].length;
}

// --- continuous --------------------------------------------------------------------------------

const INTERPOLATORS = {
  viridis: interpolateViridis,
  magma: interpolateMagma,
  plasma: interpolatePlasma,
  inferno: interpolateInferno,
  cividis: interpolateCividis,
  turbo: interpolateTurbo,
  greys: (t: number) => interpolateGreys(0.15 + 0.85 * t), // light grey to black, not white
  'blue-red': (t: number) => interpolateRdBu(1 - t), // diverging: blue below, red above 0
} as const;

export type ColormapName = keyof typeof INTERPOLATORS;
export const COLORMAPS = Object.keys(INTERPOLATORS) as ColormapName[];

export const COLORMAP_LABELS: Record<ColormapName, string> = {
  viridis: 'viridis',
  magma: 'magma',
  plasma: 'plasma',
  inferno: 'inferno',
  cividis: 'cividis (colour-blind safe)',
  turbo: 'turbo (rainbow)',
  greys: 'greys',
  'blue-red': 'blue–red (around 0)',
};

/** Diverging maps are centred on 0: their range is made symmetric. */
export const isDiverging = (name: ColormapName) => name === 'blue-red';

function parseCss(c: string): RGB {
  if (c.startsWith('#')) return hexToRgb(c);
  const m = c.match(/\d+(\.\d+)?/g) ?? ['0', '0', '0'];
  return [Number(m[0]), Number(m[1]), Number(m[2])].map((v) => Math.round(v)) as RGB;
}

const lutCache = new Map<ColormapName, Uint8Array>();

/** 256 RGBA entries. */
export function colormapLut(name: ColormapName): Uint8Array {
  const hit = lutCache.get(name);
  if (hit) return hit;
  const f = INTERPOLATORS[name];
  const lut = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i += 1) {
    const [r, g, b] = parseCss(f(i / 255));
    lut.set([r, g, b, 255], i * 4);
  }
  lutCache.set(name, lut);
  return lut;
}

/** A CSS gradient of a colour map, for colour bars. */
export function colormapGradient(name: ColormapName, steps = 11): string {
  const lut = colormapLut(name);
  const stops = Array.from({ length: steps }, (_, i) => {
    const k = Math.round((i / (steps - 1)) * 255) * 4;
    return `rgb(${lut[k]},${lut[k + 1]},${lut[k + 2]}) ${Math.round((i / (steps - 1)) * 100)}%`;
  });
  return `linear-gradient(to right, ${stops.join(',')})`;
}
