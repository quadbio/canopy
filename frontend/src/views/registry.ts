// View types plug in here. A view type supplies:
//  - `useView(spec)`: a React hook that loads data and returns deck.gl layers, the data bounds
//    and a tooltip; it runs inside an invisible per-view component, so hooks are allowed.
//    A view may instead return several `panels` (e.g. one per tissue section): they share the
//    view's data, settings, legend and title, are laid out as a grid inside the view's cell, and
//    pan and zoom together (`link`);
//  - `Panel`: its settings in the right-hand panel; `Legend`: an overlay in its canvas cell;
//  - `create(meta)`: default parameters for a dataset.
// Adding a view type touches no other view: write one module and call `registerView`.

import type { Layer } from '@deck.gl/core';
import type { ComponentType, ReactNode } from 'react';

import type { Cell, Insets, OrthoState } from './layout';
import type { Polygon } from './selection';

import type { DatasetMeta } from '../api/types';
import type { ViewSpec } from '../state/workspace';

export type Bounds = [minX: number, minY: number, maxX: number, maxY: number];

/** One viewport of a view. Its layers' ids must start with `panelLayerId(view, panel, …)`'s
 * prefix, so they are drawn only in this panel. */
export interface PanelOutput {
  id: string;
  title?: string;
  layers: Layer[];
  bounds: Bounds | null;
  /** Text for item `index` of layer `layer` (the top-level layer's id). */
  tooltip?: (index: number, layer: string) => string | null;
  /** Drawn over the panel and redrawn as it pans and zooms (e.g. a plot's axes): gets the
   * panel's view state, its rectangle (canvas pixels) and the room taken in it by the title
   * row and an open legend (to keep clear of). */
  overlay?: (state: OrthoState, rect: Cell, taken: Insets) => ReactNode;
}

/** How the panels of a view follow each other: the same zoom, or the same zoom and the same
 * offset from each panel's own centre. */
export type Link = 'none' | 'zoom' | 'pan+zoom';

/** The deck.gl view id of a panel, and the prefix of its layers' ids. View and panel ids are
 * generated and must not hold `/` or `:` (they separate the parts of these ids). */
export const panelKey = (viewId: string, panelId: string) => {
  if (/[/:]/.test(viewId) || panelId.includes(':')) throw new Error(`invalid view or panel id: ${viewId}/${panelId}`);
  return `${viewId}/${panelId}`;
};
export const panelLayerId = (viewId: string, panelId: string, name: string) => `${panelKey(viewId, panelId)}:${name}`;
/** The view and panel a layer (or sub-layer) id belongs to. */
export function ownerOf(layerId: string): { view: string; panel: string } {
  const key = layerId.split(':')[0];
  const slash = key.indexOf('/');
  return slash < 0 ? { view: key, panel: MAIN_PANEL } : { view: key.slice(0, slash), panel: key.slice(slash + 1) };
}
export const MAIN_PANEL = 'main';

export interface ViewOutput {
  /** Layers of a view with one panel (ids prefixed with `panelLayerId(view, MAIN_PANEL, …)`). */
  layers: Layer[];
  bounds: Bounds | null;
  /** Several panels instead of `layers`/`bounds`/`tooltip`. */
  panels?: PanelOutput[];
  link?: Link;
  /** Panels fitted at one shared scale (default), so sizes compare; else each fills its panel. */
  sameScale?: boolean;
  /** Panels show the same area (the union of their data) at the same scale, so positions
   * compare (e.g. a UMAP split by condition). */
  sameCoordinates?: boolean;
  /** A chart drawn in the page (e.g. SVG violins or bars) instead of map layers: shown in the
   * view's cell below its title, left of an open legend; nothing is panned or zoomed. */
  content?: ReactNode;
  /** Room kept free inside each panel when fitting (e.g. for axes), besides the title row. */
  margins?: Insets;
  /** What one unit of the coordinates is (e.g. the embedding's name): the views of a linked group
   * with the same key are fitted at one scale, so sizes compare; others each fill their space. */
  scaleKey?: string;
  /** What the data are (e.g. dataset and coordinate system): a view refits when it changes;
   * a change of `bounds` alone (more data arriving) refits only a view the user has not moved.
   * Without it every change of `bounds` counts as new data. */
  fitKey?: string;
  /** y grows downwards on screen (images, spatial coordinates). */
  flipY?: boolean;
  /** idle: waiting for a choice in the settings; `error` then holds the instruction */
  status: 'loading' | 'ready' | 'error' | 'idle';
  error?: string;
  /** The view needs this dataset converted: the cell offers the conversion with its error. */
  needsConversion?: string;
  /** A short note shown on the view, e.g. that a saved setting no longer applies. */
  notice?: string;
  /** A part of the view failed (e.g. its colouring), the rest is still shown; with a retry. */
  problem?: { message: string; retry?: () => void };
  /** Text for item `index` of layer `layer` (the top-level layer's id). */
  tooltip?: (index: number, layer: string) => string | null;
  /** A short note next to the title, e.g. "1 M of 3 M cells". */
  caption?: string;
  /** Number of items drawn, for telemetry. */
  n?: number;
  /** Number of items the view holds (for the canvas's level-of-detail budget). */
  total?: number;
  /** Linked group: cells shown as NA because this dataset lacks the group's column or gene. */
  naCells?: number;
  /** Whatever the view's Legend needs, computed once by the view's hook. */
  legend?: unknown;
  /** Lasso selection (S7c): adds the cells inside `poly` (view coordinates of `panel`) to `mask`
   * (one entry per cell of the view's table; cells hidden in the legend left out) and returns
   * how many it added. `key` says what the coordinates are: a selection's outline is shown
   * while it stays the same. */
  lasso?: { key: string; n: number; select: (panel: string, poly: Polygon, mask: Uint8Array) => Promise<number> };
}

/** The panels of a view output: its `panels`, or one panel from `layers`/`bounds`/`tooltip`. */
export function panelsOf(out: ViewOutput | undefined): PanelOutput[] {
  if (!out) return [];
  if (out.panels) return out.panels;
  return [{ id: MAIN_PANEL, layers: out.layers, bounds: out.bounds, tooltip: out.tooltip }];
}

export interface ViewType<P = Record<string, unknown>> {
  type: string;
  label: string;
  /** Lower first when several view types can show a dataset (default 10). */
  rank?: number;
  /** The name of the "Open" button for this view type (default "Open"). */
  openLabel?: string;
  /** Parameters that stay per dataset in a linked group (e.g. the embedding: names differ
   * between datasets); every other parameter is shared by the group. */
  ownKeys?: readonly string[];
  /** Whether this view can show the dataset, and its default parameters if so. */
  create: (meta: DatasetMeta) => P | null;
  /** Saved parameters (from an older build, or edited by hand) made valid: defaults filled in,
   * wrong types replaced. Null if they cannot be used at all. */
  normalise: (raw: unknown) => P | null;
  useView: (spec: ViewSpec<P>) => ViewOutput;
  Panel: ComponentType<{ spec: ViewSpec<P> }>;
  /** `compact`: the cell is small, so the legend starts folded unless the user unfolded it. */
  Legend?: ComponentType<{ spec: ViewSpec<P>; output: ViewOutput; compact: boolean }>;
}

const registry = new Map<string, ViewType<never>>();

export function registerView<P>(t: ViewType<P>) {
  registry.set(t.type, t as unknown as ViewType<never>);
}

export function viewType<P = Record<string, unknown>>(type: string): ViewType<P> | undefined {
  return registry.get(type) as unknown as ViewType<P> | undefined;
}

/** All view types, the preferred ones first (`rank`). */
export function viewTypes(): ViewType[] {
  return ([...registry.values()] as unknown as ViewType[]).sort((a, b) => (a.rank ?? 10) - (b.rank ?? 10));
}
