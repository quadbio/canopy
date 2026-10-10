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
  tooltip?: (index: number) => string | null;
  /** Number of items drawn, for telemetry. */
  n?: number;
}

export interface ViewType<P = Record<string, unknown>> {
  type: string;
  label: string;
  /** Whether this view can show the dataset, and its default parameters if so. */
  create: (meta: DatasetMeta) => P | null;
  useView: (spec: ViewSpec<P>) => ViewOutput;
  Panel: ComponentType<{ spec: ViewSpec<P> }>;
  Legend?: ComponentType<{ spec: ViewSpec<P> }>;
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
