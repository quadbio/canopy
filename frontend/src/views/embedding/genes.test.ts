import { describe, expect, it } from 'vitest';

import type { DatasetMeta, GeneList } from '../../api/types';
import { matchGenes } from '../../ui/GeneSearch';
import { effectiveParams, geneLabel, geneRange, normaliseParams } from '.';

const genes: GeneList = {
  names: ['ENSG01', 'ENSG02', 'ENSG03', 'ENSG04'],
  symbols: ['CD3E', 'CD3EAP', 'XCD3', 'MS4A1'],
  symbol_column: 'gene_symbols',
  duplicates: 0,
  matrices: [],
};

const meta = (access: 'direct' | 'needs_conversion' = 'direct') =>
  ({
    embeddings: [{ key: 'X_umap', dims: 2 }],
    obs: [],
    matrices: [
      { name: 'X', encoding: 'csr', dtype: 'float32', access },
      { name: 'counts', encoding: 'csr', dtype: 'int64', access: 'memory' },
    ],
  }) as unknown as DatasetMeta;

describe('gene search', () => {
  it('ranks exact, then prefix, then substring matches; searches symbols and ids', () => {
    expect(matchGenes(genes, 'cd3').map((m) => m.label)).toEqual(['CD3E', 'CD3EAP', 'XCD3']);
    expect(matchGenes(genes, 'CD3E').map((m) => m.label)).toEqual(['CD3E', 'CD3EAP']);
    expect(matchGenes(genes, 'ensg04')[0]).toEqual({ index: 3, label: 'MS4A1', name: 'ENSG04' });
    expect(matchGenes(genes, '  ')).toEqual([]);
  });

  it('stays fast for 60 k Ensembl ids with symbols', () => {
    const big: GeneList = {
      names: Array.from({ length: 60_000 }, (_, i) => `ENSG${String(i).padStart(11, '0')}`),
      symbols: Array.from({ length: 60_000 }, (_, i) => `GENE${i}`),
      symbol_column: 'gene_symbols',
      duplicates: 0,
      matrices: [],
    };
    matchGenes(big, 'x'); // first call lower-cases the list once
    const t0 = performance.now();
    for (const q of ['e', 'en', 'ens', 'gene1', 'gene12']) expect(matchGenes(big, q).length).toBeGreaterThan(0);
    expect((performance.now() - t0) / 5).toBeLessThan(25);
    expect(matchGenes(big, 'gene12')[0].label).toBe('GENE12');
  });

  it('labels genes by symbol when there is one', () => {
    expect(geneLabel(genes, 0)).toBe('CD3E');
    expect(geneLabel({ ...genes, symbols: null }, 0)).toBe('ENSG01');
  });
});

describe('gene colour range', () => {
  const g = (min: number, max: number, p99: number) => ({ values: new Float32Array(), min, max, p99, nnz: 1 });
  it('runs from 0 to the 99th percentile of expressing cells', () => {
    expect(geneRange(g(0, 50, 8))).toEqual([0, 8]);
    expect(geneRange(g(-2, 3, 2.5))).toEqual([-2, 2.5]);
    expect(geneRange(g(0, 0, 0))).toEqual([0, 1]); // expressed nowhere
  });
});

describe('saved gene colouring', () => {
  const saved = (colorBy: unknown) => normaliseParams({ embedding: 'X_umap', colorBy })!;

  it('keeps a gene that exists, and its matrix', () => {
    const p = saved({ kind: 'gene', name: 'ENSG02', matrix: 'counts' });
    expect(p.colorBy).toEqual({ kind: 'gene', name: 'ENSG02', matrix: 'counts' });
    expect(effectiveParams(p, meta(), genes).notice).toBeUndefined();
  });

  it('drops a gene that is gone, or one whose matrix needs conversion, with a notice', () => {
    const gone = effectiveParams(saved({ kind: 'gene', name: 'ENSG99' }), meta(), genes);
    expect(gone.p.colorBy).toBeNull();
    expect(gone.notice).toMatch(/ENSG99/);
    const conv = effectiveParams(saved({ kind: 'gene', name: 'ENSG01' }), meta('needs_conversion'), genes);
    expect(conv.p.colorBy).toBeNull();
    expect(conv.notice).toMatch(/conversion/);
  });

  it('falls back to X when a saved layer is gone', () => {
    const r = effectiveParams(saved({ kind: 'gene', name: 'ENSG01', matrix: 'lognorm' }), meta(), genes);
    expect(r.p.colorBy).toEqual({ kind: 'gene', name: 'ENSG01' });
  });
});
