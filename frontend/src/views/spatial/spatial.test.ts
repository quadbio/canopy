import { describe, expect, it } from 'vitest';

import type { Manifest } from '../../api/store';
import { boxesInView, channelSettings, invert, modelMatrix, resolveElements, type SpatialParams, transformBox } from '.';

const I = [
  [1, 0, 0],
  [0, 1, 0],
  [0, 0, 1],
];
const ROT = [
  [0, -1, 10],
  [1, 0, 0],
  [0, 0, 1],
];

const manifest = {
  coordinate_systems: ['global', 'aligned'],
  units: {},
  tables: { table_cells: { path: 'tables/table_cells', n_obs: 3, region: ['cells'] } },
  images: {
    img: {
      path: 'images/img',
      transforms: { global: I, aligned: ROT },
      axes: ['c', 'y', 'x'],
      dtype: 'uint8',
      levels: [[3, 10, 20]],
      channels: [
        { name: 'empty', window: [0, 1], color: '#ff0000' },
        { name: 'dapi', window: [3, 200], color: '#00ff00' },
        { name: 'polyT', window: [5, 90], color: '#0000ff' },
      ],
      rgb: false,
    },
  },
  labels: {},
  shapes: {
    tiles: { path: 'shapes/tiles', transforms: { global: I }, kind: 'polygons', n: 9, n_vertices: 45, table: 'table_tiles' },
    cells: { path: 'shapes/cells', transforms: { global: I }, kind: 'polygons', n: 3, n_vertices: 15, table: 'table_cells' },
  },
  points: {},
} as unknown as Manifest;

const params = (p: Partial<SpatialParams> = {}) =>
  ({ system: null, image: null, channels: {}, cells: null, transcripts: null, genes: [], ...p }) as SpatialParams;

describe('spatial view helpers', () => {
  it('places elements with their affine', () => {
    expect(Array.from(modelMatrix(ROT))).toEqual([0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1, 0, 10, 0, 0, 1]);
    expect(transformBox(ROT, [0, 0, 2, 4])).toEqual([6, 0, 10, 2]);
  });

  it('picks the cells of the view’s table, and nothing when “None” was chosen', () => {
    const el = resolveElements(params(), manifest, 'table_cells');
    expect(el).toMatchObject({ system: 'global', image: 'img', cells: 'cells' });
    expect(resolveElements(params({ cells: '' }), manifest, 'table_cells').cells).toBeNull();
    // an element not placed in the chosen system is not shown
    expect(resolveElements(params({ system: 'aligned' }), manifest, 'table_cells').cells).toBeNull();
  });

  it('starts with the channels that have signal', () => {
    expect(channelSettings(params(), manifest, 'img').map((c) => c.on)).toEqual([false, true, true]);
  });
});

describe('reading in view', () => {
  it('inverts affines and finds the boxes in view', () => {
    const inv = invert(ROT);
    expect(transformBox(inv, transformBox(ROT, [0, 0, 2, 4]))).toEqual([0, 0, 2, 4]);
    const boxes = [0, 0, 10, 10, 20, 20, 30, 30, 5, 5, 25, 25];
    expect(boxesInView(boxes, [12, 12, 18, 18])).toEqual([2]);
    expect(boxesInView(boxes, [0, 0, 100, 100], 1)).toEqual([1, 2]);
  });
});
