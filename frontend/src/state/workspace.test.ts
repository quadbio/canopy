import { describe, expect, it, vi } from 'vitest';

import type { DatasetMeta } from '../api/types';
import { effectiveParams, normaliseParams } from '../views/embedding';
import { registerView, viewType } from '../views/registry';
import { defaultPointSize } from '../views/embedding';
import { MAX_REMEMBERED, rememberKey, restoreRemembered, restoreViews, safeStorage, useWorkspace } from './workspace';

describe('restoreViews', () => {
  it('drops unknown view types and repairs parameters from older builds', () => {
    const restored = restoreViews([
      { id: 'a', type: 'spatial', datasetId: 'd', table: null, title: 'x', params: {} }, // unknown type
      { id: 'b', type: 'embedding', datasetId: 'd', table: null, title: 'old', params: { embedding: 'X_umap' } },
      { id: 'c', type: 'embedding', datasetId: 'd', params: { embedding: 42 } }, // unusable params
      'garbage',
      null,
    ]);
    expect(restored.map((v) => v.id)).toEqual(['b']);
    expect(restored[0].params).toMatchObject({ embedding: 'X_umap', dims: [0, 1], hidden: [], colorBy: null });
  });

  it('accepts anything without throwing', () => {
    expect(restoreViews(undefined)).toEqual([]);
    expect(restoreViews({ views: 1 })).toEqual([]);
  });
});

describe('embedding parameters', () => {
  it('clamps and fills', () => {
    const p = normaliseParams({ embedding: 'X_pca', dims: [3, 3], pointSize: 99, opacity: 'x', hidden: [1, 'a'] });
    expect(p).toMatchObject({ dims: [0, 1], pointSize: 10, opacity: 0.85, hidden: ['a'], colormap: 'viridis' });
  });

  it('falls back with a notice when a saved column or embedding is gone', () => {
    const meta = {
      embeddings: [{ key: 'X_umap', dims: 2 }],
      obs: [{ name: 'leiden', kind: 'categorical' }],
    } as unknown as DatasetMeta;
    const saved = normaliseParams({ embedding: 'X_tsne', colorBy: 'gone', hidden: ['a'] })!;
    const { p, notice } = effectiveParams(saved, meta);
    expect(p.embedding).toBe('X_umap');
    expect(p.colorBy).toBeNull();
    expect(notice).toMatch(/X_tsne.*gone/);
    expect(effectiveParams(p, meta).notice).toBeUndefined();
  });
});

describe('view registry', () => {
  it('takes a new view type from one module', () => {
    registerView<{ n: number }>({
      type: 'test-dummy',
      label: 'Dummy',
      create: () => ({ n: 1 }),
      normalise: (raw) => (raw && typeof raw === 'object' ? { n: 1 } : null),
      useView: () => ({ layers: [], bounds: null, status: 'ready' }),
      Panel: () => null,
    });
    expect(viewType('test-dummy')?.label).toBe('Dummy');
    expect(restoreViews([{ id: 'z', type: 'test-dummy', datasetId: 'd', params: {} }])).toHaveLength(1);
  });
});

describe('companion plots', () => {
  it('close with the view they follow, the selection moving to a view still open', () => {
    const ws = useWorkspace.getState();
    ws.reset();
    const base = { type: 'embedding', datasetId: 'd1', table: null, title: 'd1' };
    const a = ws.addView({ ...base, params: normaliseParams({ embedding: 'X_umap' })! })!;
    const b = ws.addView({ ...base, params: normaliseParams({ embedding: 'X_umap' })! })!;
    const plot = useWorkspace.getState().addView({ type: 'plot', datasetId: 'd1', table: null, title: 'p', params: { follows: a } })!;
    useWorkspace.getState().select(plot);
    useWorkspace.getState().removeView(a);
    const s = useWorkspace.getState();
    expect(s.views.map((v) => v.id)).toEqual([b]);
    expect(s.selected).toBe(b);
  });
});

describe('remembered settings', () => {
  const base = { type: 'embedding', datasetId: 'd1', table: null, title: 'd1' };

  it('keeps the last settings of a closed view and recalls them validated', () => {
    const ws = useWorkspace.getState();
    ws.reset();
    const id = ws.addView({ ...base, params: normaliseParams({ embedding: 'X_umap' })! })!;
    useWorkspace.getState().updateParams(id, { pointSize: 5, hidden: ['T cell'] });
    useWorkspace.getState().removeView(id);
    const got = useWorkspace.getState().recall(rememberKey(base));
    expect(got).toMatchObject({ embedding: 'X_umap', pointSize: 5, hidden: ['T cell'] });
    expect(useWorkspace.getState().recall(rememberKey({ ...base, table: 'other' }))).toBeNull();
  });

  it('keeps at most MAX_REMEMBERED datasets, dropping the oldest', () => {
    const ws = useWorkspace.getState();
    ws.reset();
    for (let i = 0; i < MAX_REMEMBERED + 5; i++) {
      const id = useWorkspace.getState().addView({ ...base, datasetId: `d${i}`, params: { embedding: 'X_umap' } })!;
      useWorkspace.getState().updateParams(id, { pointSize: 2 });
      useWorkspace.getState().removeView(id);
    }
    const keys = Object.keys(useWorkspace.getState().remembered);
    expect(keys).toHaveLength(MAX_REMEMBERED);
    expect(keys).not.toContain(rememberKey({ ...base, datasetId: 'd0' }));
  });

  it('restores only well-formed entries', () => {
    expect(restoreRemembered({ a: { params: {}, at: 1 }, b: { params: 1, at: 1 }, c: null })).toEqual({ a: { params: {}, at: 1 } });
    expect(restoreRemembered('x')).toEqual({});
  });
});

describe('default point size', () => {
  it('shrinks with the number of cells', () => {
    expect(defaultPointSize(5_000)).toBeGreaterThan(defaultPointSize(100_000));
    expect(defaultPointSize(100_000)).toBeGreaterThan(defaultPointSize(3_000_000));
  });
});

describe('settings storage', () => {
  it('writes at most every few hundred ms, the latest value', () => {
    vi.useFakeTimers();
    const writes: string[] = [];
    const st = safeStorage({ getItem: () => null, setItem: (_k, v) => void writes.push(v), removeItem: () => {} });
    for (let i = 0; i < 20; i += 1) st.setItem('k', String(i));
    expect(st.getItem('k')).toBe('19'); // reads see the pending value
    vi.advanceTimersByTime(400);
    expect(writes).toEqual(['19']);
    vi.useRealTimers();
  });

  it('keeps working in memory when the browser store is full', () => {
    const full = {
      getItem: () => null,
      setItem: () => {
        throw new DOMException('full', 'QuotaExceededError');
      },
      removeItem: () => {},
    };
    const st = safeStorage(full);
    st.setItem('k', 'v');
    expect(() => st.flush()).not.toThrow();
    expect(st.getItem('k')).toBe('v');
  });
});
