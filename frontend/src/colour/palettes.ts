// Colour palettes. Categorical: used when a dataset brings no valid `uns[<col>_colors]`.
// Continuous: lookup tables built from published polynomial fits.

export type RGB = [number, number, number];

// 30 distinct colours: Okabe–Ito first (colour-blind safe), then Tableau/Kelly-style colours.
export const CATEGORICAL: string[] = [
  '#0072b2', '#e69f00', '#009e73', '#cc79a7', '#56b4e9', '#d55e00', '#f0e442', '#000000',
  '#4e79a7', '#f28e2b', '#e15759', '#76b7b2', '#59a14f', '#edc948', '#b07aa1', '#ff9da7',
  '#9c755f', '#bab0ac', '#875692', '#f38400', '#a1caf1', '#be0032', '#c2b280', '#848482',
  '#008856', '#e68fac', '#0067a5', '#f99379', '#604e97', '#f6a600',
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
