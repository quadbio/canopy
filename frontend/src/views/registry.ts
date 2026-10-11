// View types plug in here. A view type supplies:
//  - `useView(spec)`: a React hook that loads data and returns deck.gl layers, the data bounds
//    and a tooltip; it runs inside an invisible per-view component, so hooks are allowed;
//  - `Panel`: its settings in the right-hand panel; `Legend`: an overlay in its canvas cell;
//  - `create(meta)`: default parameters for a dataset.
// Adding a view type touches no other view: write one module and call `registerView`.

import type { Layer } from '@deck.gl/core';
import type { ComponentType } from 'react';

import type { DatasetMeta } from '../api/types';
import type { ViewSpec } from '../state/workspace';

export type Bounds = [minX: number, minY: number, maxX: number, maxY: number];

export interface ViewOutput {
  layers: Layer[];
  bounds: Bounds | null;
  status: 'loading' | 'ready' | 'error';
  error?: string;
  /** A short note shown on the view, e.g. that a saved setting no longer applies. */
  notice?: string;
  /** A part of the view failed (e.g. its colouring), the rest is still shown; with a retry. */
  problem?: { message: string; retry?: () => void };
  tooltip?: (index: number) => string | null;
  /** Number of items drawn, for telemetry. */
  n?: number;
  /** Whatever the view's Legend needs, computed once by the view's hook. */
  legend?: unknown;
}

export interface ViewType<P = Record<string, unknown>> {
  type: string;
  label: string;
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

export function viewTypes(): ViewType[] {
  return [...registry.values()] as unknown as ViewType[];
}
