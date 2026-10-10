// Colour palettes. Categorical: used when a dataset brings no valid `uns[<col>_colors]`.
// Continuous: lookup tables built from published polynomial fits.

export type RGB = [number, number, number];

// 54 colours: matplotlib's tab20 (dark tones first, then light), tab20b and tab20c, without the
// greys (grey means "missing") and without black (invisible on a dark canvas).
export const CATEGORICAL: string[] = [
  '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#bcbd22', '#17becf',
  '#aec7e8', '#ffbb78', '#98df8a', '#ff9896', '#c5b0d5', '#c49c94', '#f7b6d2', '#dbdb8d', '#9edae5',
  '#393b79', '#5254a3', '#6b6ecf', '#9c9ede', '#637939', '#8ca252', '#b5cf6b', '#cedb9c', '#8c6d31',
  '#bd9e39', '#e7ba52', '#e7cb94', '#843c39', '#ad494a', '#d6616b', '#e7969c', '#7b4173', '#a55194',
  '#ce6dbd', '#de9ed6', '#3182bd', '#6baed6', '#9ecae1', '#c6dbef', '#e6550d', '#fd8d3c', '#fdae6b',
  '#fdd0a2', '#31a354', '#74c476', '#a1d99b', '#c7e9c0', '#756bb1', '#9e9ac8', '#bcbddc', '#dadaeb',
];

export const MISSING: RGB = [190, 190, 190];

export function hexToRgb(hex: string): RGB {
  const h = hex.replace('#', '');
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

export function categoryColors(n: number, given: string[] | null): RGB[] {
  if (given && given.length === n) return given.map(hexToRgb);
  return Array.from({ length: n }, (_, i) => hexToRgb(CATEGORICAL[i % CATEGORICAL.length]));
}

/** True when the default palette has to repeat colours for this many categories. */
export function coloursRepeat(n: number, given: string[] | null): boolean {
  return !(given && given.length === n) && n > CATEGORICAL.length;
}

type Poly = [number, number, number][];

// Viridis and magma: 6th-order polynomial fits (Inigo Quilez / mattz), error < 1/255.
const VIRIDIS: Poly = [
  [0.2777273272234177, 0.005407344544966578, 0.3340998053353061],
  [0.1050930431085774, 1.404613529898575, 1.384590162594685],
  [-0.3308618287255563, 0.214847559468213, 0.09509516302823659],
  [-4.634230498983486, -5.799100973351585, -19.33244095627987],
  [6.228269936347081, 14.17993336680509, 56.69055260068105],
  [4.776384997670288, -13.74514537774601, -65.35303263337234],
  [-5.435455855934631, 4.645852612178535, 26.3124352495832],
];
const MAGMA: Poly = [
  [-0.002136485053939582, -0.000749655052795221, -0.005386127855323933],
  [0.2516605407371642, 0.6775232436837668, 2.494026599312351],
  [8.353717279216625, -3.577719514958484, 0.3144679030132573],
  [-27.66873308576866, 14.26473078096533, -13.64921318813922],
  [52.17613981234068, -27.94360607168351, 12.94416944238394],
  [-50.76852536473588, 29.04658282127291, 4.23415299384598],
  [18.65570506591883, -11.48977351997711, -5.601961508734096],
];

export const COLORMAPS = { viridis: VIRIDIS, magma: MAGMA } as const;
export type ColormapName = keyof typeof COLORMAPS;

/** 256-entry RGB lookup table (Uint8Array of 768). */
export function colormapLut(name: ColormapName): Uint8Array {
  const c = COLORMAPS[name];
  const lut = new Uint8Array(256 * 3);
  for (let i = 0; i < 256; i += 1) {
    const t = i / 255;
    for (let ch = 0; ch < 3; ch += 1) {
      let v = c[6][ch];
      for (let k = 5; k >= 0; k -= 1) v = c[k][ch] + t * v;
      lut[i * 3 + ch] = Math.round(Math.min(1, Math.max(0, v)) * 255);
    }
  }
  return lut;
}
