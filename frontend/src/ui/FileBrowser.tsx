// File browser (F10, basic): data roots as a folder tree, name search, and a details panel with
// "Open". Everything comes from the catalog index; nothing here touches the file system.

import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import { ApiError } from '../api/client';
import { metaQuery, useDatasetDetail, useFolder, useRoots, useSearch } from '../api/queries';
import type { DatasetEntry } from '../api/types';
import { MAX_VIEWS, rememberKey, useWorkspace } from '../state/workspace';
import { event } from '../telemetry/telemetry';
import { viewTypes } from '../views/registry';
import { formatBytes, formatCount, Help } from './controls';
import { DatasetNotes, type Notice } from './notes';

const KIND_LABEL: Record<string, string> = { h5ad: 'h5ad', anndata_zarr: 'zarr', spatialdata_zarr: 'spatial' };

function DatasetRow({ d, depth }: { d: DatasetEntry; depth: number }) {
  const selected = useWorkspace((s) => s.browserSelection === d.id);
  const select = useWorkspace((s) => s.selectDataset);
  const s = d.summary;
  return (
    <li>
      <button
        className={`tree-row dataset${selected ? ' selected' : ''}${d.state !== 'inspected' ? ' dim' : ''}`}
        style={{ paddingLeft: 8 + depth * 14 }}
        onClick={() => select(d.id)}
        data-testid="dataset-row"
        title={`${d.path}${d.size_bytes !== null ? ` · ${formatBytes(d.size_bytes)}` : ''}`}
        role="treeitem"
        aria-selected={selected}
      >
        <span className={`badge kind-${d.kind ?? 'none'}`}>{d.kind ? KIND_LABEL[d.kind] : d.state}</span>
        <span className="tree-name">{d.name}</span>
        <span className="tree-meta">{s.n_obs !== undefined ? formatCount(s.n_obs) : ''}</span>
      </button>
    </li>
  );
}

function Folder({ root, path, name, depth, count }: { root: string; path: string; name: string; depth: number; count: number }) {
  const [open, setOpen] = useState(depth === 0);
  const q = useFolder(root, path, open);
  return (
    <li>
      <button
        className="tree-row folder"
        style={{ paddingLeft: 8 + depth * 14 }}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        role="treeitem"
      >
        <span className="chevron">{open ? '▾' : '▸'}</span>
        <span className="tree-name">{name}</span>
        <span className="tree-meta">{count}</span>
      </button>
      {open && (
        <ul className="tree" role="group">
          {q.isLoading && <li className="muted small" style={{ paddingLeft: 22 + depth * 14 }}>Loading…</li>}
          {q.error && <li className="error small">{String(q.error.message)}</li>}
          {q.data?.folders.map((f) => (
            <Folder key={f.path} root={root} path={f.path} name={f.name} depth={depth + 1} count={f.n_datasets} />
          ))}
          {q.data?.datasets.map((d) => <DatasetRow key={d.id} d={d} depth={depth + 1} />)}
        </ul>
      )}
    </li>
  );
}

/** "6.1 k rows · one per object in boundaries_cells" */
function describeTable(
  sd: { tables?: Record<string, { n_obs?: number }>; table_refs?: Record<string, { region?: string[] }> } | undefined,
  table: string,
): string {
  const n = sd?.tables?.[table]?.n_obs;
  const region = sd?.table_refs?.[table]?.region ?? [];
  const parts = [n !== undefined ? `${formatCount(n)} rows` : null, region.length ? `one per object in ${region.join(', ')}` : null];
  return parts.filter(Boolean).join(' · ');
}

function Details({ id }: { id: string }) {
  const q = useDatasetDetail(id);
  const addView = useWorkspace((s) => s.addView);
  const recall = useWorkspace((s) => s.recall);
  const queryClient = useQueryClient();
  const [table, setTable] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!q.data) return <div className="details muted">{q.error ? String(q.error.message) : 'Loading…'}</div>;
  const d = q.data;
  const s = d.summary;
  const fp = d.fingerprint as {
    warnings?: Notice[];
    spatialdata?: {
      tables?: Record<string, { n_obs?: number }>;
      table_refs?: Record<string, { region?: string[] }>;
    };
  } | null;
  const tables = Object.keys(fp?.spatialdata?.tables ?? {});
  const chosenTable = d.kind === 'spatialdata_zarr' ? (table ?? tables[0] ?? null) : null;

  const open = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const meta = await queryClient.fetchQuery(metaQuery(d.id, chosenTable));
      for (const t of viewTypes()) {
        const defaults = t.create(meta);
        if (defaults) {
          // Continue with the settings this dataset had last time, if any.
          const saved = recall(rememberKey({ type: t.type, datasetId: d.id, table: chosenTable }));
          const added = addView({
            type: t.type,
            datasetId: d.id,
            table: chosenTable,
            title: chosenTable ? `${d.name} · ${chosenTable}` : d.name,
            params: saved ?? defaults,
          });
          if (added === null) {
            setProblem(`At most ${MAX_VIEWS} views fit in the canvas: close one first.`);
            return;
          }
          event('view_opened', null, { type: t.type, kind: d.kind ?? '', n_obs: meta.n_obs, remembered: saved ? 1 : 0 });
          return;
        }
      }
      setProblem('This dataset has no embedding to show yet.');
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="details" data-testid="details">
      <h3 title={d.path}>{d.name}</h3>
      <p className="muted small path">
        {d.root} / {d.path}
      </p>
      <dl className="facts">
        <dt>Type</dt>
        <dd>{d.kind ?? '–'}</dd>
        <dt>Size</dt>
        <dd>{formatBytes(d.size_bytes)}</dd>
        {s.n_obs !== undefined && (
          <>
            <dt>Cells × genes</dt>
            <dd>
              {formatCount(s.n_obs)} × {formatCount(s.n_vars)}
            </dd>
            <dt>Matrix</dt>
            <dd>{s.x_encoding ?? '–'}</dd>
            <dt>Embeddings</dt>
            <dd>{s.embeddings?.length ? s.embeddings.join(', ') : 'none'}</dd>
          </>
        )}
        {d.kind === 'spatialdata_zarr' && (
          <>
            <dt>Spatial</dt>
            <dd>
              {s.n_images ?? 0} images · {s.n_labels ?? 0} labels · {s.n_shapes ?? 0} shapes · {s.n_points ?? 0} points
            </dd>
          </>
        )}
      </dl>
      {tables.length > 1 && chosenTable && (
        <div className="field">
          <span className="field-label">
            Table
            <Help label="About “Table”">
              <p>
                A spatial dataset can hold several tables. Each one lists a different kind of object with its own
                measurements, one row per object: for example one row per segmented cell, or one row per tile (a
                square bin of the tissue).
              </p>
              <p>Pick the table whose objects you want to plot. You can open the others as separate views.</p>
            </Help>
          </span>
          <select
            className="native-select"
            aria-label="Table"
            value={chosenTable}
            onChange={(e) => setTable(e.target.value)}
          >
            {tables.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
          <span className="muted small">{describeTable(fp?.spatialdata, chosenTable)}</span>
        </div>
      )}
      {d.state !== 'inspected' ? (
        <p className="error small">
          {d.state === 'unsupported' ? 'Not a supported dataset.' : `Could not be read (${d.reason}).`}
        </p>
      ) : (
        <button className="primary" onClick={open} disabled={busy} data-testid="open-dataset">
          {busy ? 'Opening…' : 'Open'}
        </button>
      )}
      {problem && <p className="error small">{problem}</p>}
      <DatasetNotes notices={fp?.warnings ?? []} />
    </div>
  );
}

export function FileBrowser() {
  const roots = useRoots();
  const [query, setQuery] = useState('');
  const [q, setQ] = useState('');
  useEffect(() => {
    const t = setTimeout(() => setQ(query.trim()), 150); // one request per pause, not per key
    return () => clearTimeout(t);
  }, [query]);
  const search = useSearch(q);
  const selected = useWorkspace((s) => s.browserSelection);

  return (
    <aside className="sidebar" aria-label="Datasets">
      <div className="sidebar-search">
        <input
          type="search"
          placeholder="Search datasets…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search datasets"
        />
      </div>
      <div className="sidebar-tree">
        {q ? (
          <ul className="tree" role="tree" aria-label="Search results">
            {search.data?.length === 0 && <li className="muted small pad">No matches.</li>}
            {search.data?.map((d) => <DatasetRow key={d.id} d={d} depth={0} />)}
            {search.data?.length === 100 && (
              <li className="muted small pad">Showing the first 100 matches; type more to narrow down.</li>
            )}
          </ul>
        ) : (
          <ul className="tree" role="tree" aria-label="Data folders">
            {roots.error && <li className="error small pad">{String(roots.error.message)}</li>}
            {roots.data?.length === 0 && <li className="muted small pad">No data roots configured.</li>}
            {roots.data?.map((r) => (
              <Folder key={r.name} root={r.name} path="" name={r.name} depth={0} count={r.n_datasets} />
            ))}
          </ul>
        )}
      </div>
      {selected && <Details key={selected} id={selected} />}
    </aside>
  );
}
