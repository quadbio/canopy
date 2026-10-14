// File browser (F10, basic): data roots as a folder tree, name search, and a details panel with
// "Open". Everything comes from the catalog index; nothing here touches the file system.
// Datasets ticked in the tree open together as linked views (S7b: one tile, shared settings,
// linked pan and zoom).

import { useQueries, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import { ApiError } from '../api/client';
import { catalog } from '../api/endpoints';
import { metaQuery, useDatasetDetail, useFolder, useRoots, useSearch } from '../api/queries';
import type { DatasetEntry, DatasetMeta } from '../api/types';
import { MAX_GROUP, MAX_VIEWS, rememberKey, type ViewSpec, useWorkspace } from '../state/workspace';
import { defaultColumn } from '../views/colouring';
import { event } from '../telemetry/telemetry';
import { viewTypes } from '../views/registry';
import { formatBytes, formatCount, Help } from './controls';
import { Conversion } from './Conversion';
import { DatasetNotes, type Notice } from './notes';
import { CloseAll } from './CloseAll';
import { useConvertMany } from './convertMany';
import { resetTickAnchor, ticksFor } from './ticks';

const ACTIVE_JOB = new Set(['queued', 'running']);

const KIND_LABEL: Record<string, string> = { h5ad: 'h5ad', anndata_zarr: 'zarr', spatialdata_zarr: 'spatial' };

/** `list`: the tickable datasets of the list this row is in (folder or search), for shift-click. */
function DatasetRow({ d, depth, list }: { d: DatasetEntry; depth: number; list: string[] }) {
  const selected = useWorkspace((s) => s.browserSelection === d.id);
  const select = useWorkspace((s) => s.selectDataset);
  const ticked = useWorkspace((s) => s.ticked.includes(d.id));
  const tick = useWorkspace((s) => s.tick);
  const s = d.summary;
  return (
    <li className="tree-item">
      {d.state === 'inspected' && (
        <input
          type="checkbox"
          className="tick"
          style={{ left: 6 + depth * 14 }}
          checked={ticked}
          // a click (or Space) toggles; with shift, the range from the last clicked tick follows
          onClick={(e) => tick(ticksFor(list, d.id, e.shiftKey), e.currentTarget.checked)}
          onChange={() => undefined}
          aria-label={`Tick ${d.name} to open it linked with others`}
          title="Tick several datasets to open them as linked views (shift-click: all between this and the last one clicked)"
          data-testid="dataset-tick"
        />
      )}
      <button
        className={`tree-row dataset${selected ? ' selected' : ''}${d.state !== 'inspected' ? ' dim' : ''}`}
        style={{ paddingLeft: 26 + depth * 14 }}
        onClick={() => select(d.id)}
        data-testid="dataset-row"
        title={`${d.path}${d.size_bytes !== null ? ` · ${formatBytes(d.size_bytes)}` : ''}`}
        aria-pressed={selected}
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
  const tick = useWorkspace((s) => s.tick);
  const here = (q.data?.datasets ?? []).filter((d) => d.state === 'inspected').map((d) => d.id);
  const allTicked = useWorkspace((s) => here.length > 0 && here.every((id) => s.ticked.includes(id)));
  return (
    <li className="tree-item">
      {open && here.length > 1 && (
        <button
          className="link small tick-all"
          onClick={() => tick(here, !allTicked)}
          title={allTicked ? 'Untick the datasets in this folder' : 'Tick the datasets in this folder, to open them linked'}
          data-testid="tick-folder"
        >
          {allTicked ? 'untick all' : 'tick all'}
        </button>
      )}
      <button
        className="tree-row folder"
        style={{ paddingLeft: 8 + depth * 14 }}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span className="chevron">{open ? '▾' : '▸'}</span>
        <span className="tree-name">{name}</span>
        <span className="tree-meta">{count}</span>
      </button>
      {open && (
        <ul className="tree">
          {q.isLoading && <li className="muted small" style={{ paddingLeft: 22 + depth * 14 }}>Loading…</li>}
          {q.error && <li className="error small">{String(q.error.message)}</li>}
          {q.data?.folders.map((f) => (
            <Folder key={f.path} root={root} path={f.path} name={f.name} depth={depth + 1} count={f.n_datasets} />
          ))}
          {q.data?.datasets.map((d) => <DatasetRow key={d.id} d={d} depth={depth + 1} list={here} />)}
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
  const groupOpen = useWorkspace((s) => s.views.some((v) => v.group));
  if (!q.data) return <div className="details muted">{q.error ? String(q.error.message) : 'Loading…'}</div>;
  const d = q.data;
  const s = d.summary;
  const fp = d.fingerprint as {
    warnings?: Notice[];
    anndata?: { layers?: Record<string, unknown> };
    spatialdata?: {
      tables?: Record<string, { n_obs?: number; layers?: Record<string, unknown> }>;
      table_refs?: Record<string, { region?: string[] }>;
    };
  } | null;
  const tables = Object.keys(fp?.spatialdata?.tables ?? {});
  const chosenTable = d.kind === 'spatialdata_zarr' ? (table ?? tables[0] ?? null) : null;
  // Matrices besides X (gene values can be read from them: "Values from" after picking a gene).
  const layers = Object.keys(
    (chosenTable ? fp?.spatialdata?.tables?.[chosenTable]?.layers : fp?.anndata?.layers) ?? {},
  );

  const open = async (only?: string) => {
    setBusy(true);
    setProblem(null);
    try {
      const meta = await queryClient.fetchQuery(metaQuery(d.id, chosenTable));
      for (const t of viewTypes().filter((x) => !only || x.type === only)) {
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
          event('view_opened', null, { type: t.type, kind: d.kind ?? '', dataset: d.id, n_obs: meta.n_obs, remembered: saved ? 1 : 0 });
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
        <dd>{d.size_bytes === null || d.size_bytes === undefined ? 'a folder (size not measured)' : formatBytes(d.size_bytes)}</dd>
        {s.n_obs !== undefined && (
          <>
            <dt>Cells × genes</dt>
            <dd>
              {formatCount(s.n_obs)} × {formatCount(s.n_vars)}
            </dd>
            <dt>Matrix</dt>
            <dd>{s.x_encoding ?? '–'}</dd>
            <dt>Layers</dt>
            <dd data-testid="layers">{layers.length ? layers.join(', ') : 'none'}</dd>
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
      ) : groupOpen ? (
        <CloseFirst testId="open-blocked">Linked views are open: close them to open a dataset on its own.</CloseFirst>
      ) : (
        <div className="open-row">
          {d.kind === 'spatialdata_zarr' && d.conversion?.converted ? (
            <>
              <button className="primary" onClick={() => void open('spatial')} disabled={busy} data-testid="open-spatial">
                {busy ? 'Opening…' : 'Open spatial view'}
              </button>
              <button className="ghost" onClick={() => void open('embedding')} disabled={busy} data-testid="open-dataset">
                Open embedding
              </button>
            </>
          ) : (
            <button className="primary" onClick={() => void open()} disabled={busy} data-testid="open-dataset">
              {busy ? 'Opening…' : 'Open'}
            </button>
          )}
        </div>
      )}
      {d.state === 'inspected' && d.kind === 'spatialdata_zarr' && d.conversion && !d.conversion.converted && (
        <p className="small muted" data-testid="spatial-needs-conversion">
          The spatial view (image, cells, transcripts) needs a converted copy: choose <b>Convert for full speed</b> below
          first. “Open” shows the table’s embeddings without it.
        </p>
      )}
      {problem && <p className="error small">{problem}</p>}
      {d.state === 'inspected' && <Conversion key={d.id} datasetId={d.id} layers={layers} />}
      <DatasetNotes notices={fp?.warnings ?? []} />
    </div>
  );
}

/** Shared settings for linked views: the first dataset's defaults, coloured by the column most
 * of the datasets have (ties: as the first dataset would choose; review B-M5). */
export function sharedDefaults(metas: DatasetMeta[], first: Record<string, unknown>): Record<string, unknown> {
  const count = new Map<string, number>();
  for (const m of metas) for (const c of m.obs) if (c.kind === 'categorical') count.set(c.name, (count.get(c.name) ?? 0) + 1);
  const most = Math.max(0, ...count.values());
  let col: string | null = null;
  // the best-placed candidate among the columns held by the most datasets (at least two)
  for (const m of metas) {
    if (col || most < 2) break;
    col = defaultColumn({ ...m, obs: m.obs.filter((c) => count.get(c.name) === most) });
  }
  return { ...first, colorBy: col ? { kind: 'obs', name: col } : (first.colorBy ?? null), hidden: [] };
}

/** Why opening is not possible now, and the way out: close all views. */
function CloseFirst({ children, testId }: { children: string; testId: string }) {
  return (
    <div className="close-first" data-testid={testId}>
      <p className="muted small">{children}</p>
      <CloseAll />
    </div>
  );
}

/** `f` over the items, at most `n` at a time (a pool: the next starts as soon as one ends, not
 * after the slowest of a batch; review H3 B-L9); every outcome, in order. */
async function inBatches<T, R>(items: T[], n: number, f: (x: T) => Promise<R>): Promise<PromiseSettledResult<R>[]> {
  const out: PromiseSettledResult<R>[] = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next++;
      try {
        out[i] = { status: 'fulfilled', value: await f(items[i]) };
      } catch (reason) {
        out[i] = { status: 'rejected', reason };
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, worker));
  return out;
}

/** The ticked datasets, opened together as linked views. */
function LinkedOpen() {
  const ticked = useWorkspace((s) => s.ticked);
  const tick = useWorkspace((s) => s.tick);
  const addGroup = useWorkspace((s) => s.addGroup);
  const anyOpen = useWorkspace((s) => s.views.length > 0);
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const waiting = useConvertMany((s) => s.waiting);
  const failed = useConvertMany((s) => s.failed);
  const convertMany = useConvertMany((s) => s.add);
  const stopWaiting = useConvertMany((s) => s.stop);
  const clearTicks = () => {
    tick(ticked, false);
    resetTickAnchor();
  };
  const details = useQueries({
    queries: ticked.map((id) => ({
      queryKey: ['dataset', id],
      queryFn: () => catalog.dataset(id),
      staleTime: 60_000,
      // while it converts: until its converted copy is there
      refetchInterval: (q: { state: { data?: { conversion?: { job: { state: string } | null } | null } } }) =>
        ACTIVE_JOB.has(q.state.data?.conversion?.job?.state ?? '') ? 5_000 : false,
    })),
  }).map((q) => q.data);
  if (!ticked.length) return null;
  const open = async (type: 'embedding' | 'spatial') => {
    setBusy(true);
    setProblem(null);
    try {
      const t = viewTypes().find((x) => x.type === type)!;
      // each dataset's details and meta, a few at a time; one that fails is left out (B-M4)
      const one = async (id: string) => {
        const d = await qc.fetchQuery({ queryKey: ['dataset', id], queryFn: () => catalog.dataset(id), staleTime: 60_000 });
        const tables = Object.keys((d.fingerprint as { spatialdata?: { tables?: object } } | null)?.spatialdata?.tables ?? {});
        const table = d.kind === 'spatialdata_zarr' ? (tables[0] ?? null) : null;
        if (type === 'spatial' && !(d.kind === 'spatialdata_zarr' && d.conversion?.converted)) throw new Error(d.name);
        const meta = await qc.fetchQuery(metaQuery(d.id, table));
        const params = t.create(meta) as Record<string, unknown> | null;
        if (!params) throw new Error(d.name);
        return { meta, member: { type, datasetId: d.id, table, title: d.name, params } };
      };
      const results = await inBatches(ticked, 6, one);
      const members: Omit<ViewSpec, 'id' | 'group'>[] = [];
      const metas: DatasetMeta[] = [];
      const skipped: string[] = [];
      results.forEach((r, k) => {
        if (r.status === 'fulfilled') {
          metas.push(r.value.meta);
          members.push(r.value.member);
        } else {
          const name = qc.getQueryData<{ name: string }>(['dataset', ticked[k]])?.name;
          skipped.push(name ?? (r.reason instanceof Error ? r.reason.message : ticked[k]));
        }
      });
      if (members.length < 2) {
        setProblem(`At least two of the ticked datasets must have ${type === 'spatial' ? 'a converted spatial copy' : 'an embedding'}${skipped.length ? ` (not: ${skipped.join(', ')})` : ''}.`);
        return;
      }
      // the settings are shared: the first dataset's, coloured by a column all of them have;
      // each keeps its own choices where the view type has some (e.g. the embedding)
      const shared = sharedDefaults(metas, members[0].params);
      const own = t.ownKeys ?? [];
      const linked = members.map((m) => ({
        ...m,
        params: { ...shared, ...Object.fromEntries(own.filter((k) => k in m.params).map((k) => [k, m.params[k]])) },
      }));
      if (addGroup(linked) === null) {
        setProblem('Close the open views first.');
        return;
      }
      event('group_opened', null, { type, n: linked.length, skipped: skipped.length });
      if (skipped.length) setProblem(`Not opened: ${skipped.join(', ')}.`);
      tick(ticked, false);
      resetTickAnchor();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const allSpatial = details.every((d) => d?.kind === 'spatialdata_zarr' && d.conversion?.converted);
  // spatial views need a converted copy: the ticked spatial datasets without one, converted at once
  const spatial = details.filter((d) => d?.kind === 'spatialdata_zarr');
  const converting = spatial.filter((d) => waiting.includes(d!.id) || ACTIVE_JOB.has(d!.conversion?.job?.state ?? '')).length;
  const toConvert = spatial
    .filter((d) => !d!.conversion?.converted && !waiting.includes(d!.id) && !ACTIVE_JOB.has(d!.conversion?.job?.state ?? ''))
    .map((d) => d!.id);
  // refused by the server when asked (failedHere), or failed while converting (failedJobs)
  const failedHere = failed.filter((f) => ticked.includes(f.id));
  const failedJobs = spatial.filter((d) => !d!.conversion?.converted && d!.conversion?.job?.state === 'failed');
  const conversions = (toConvert.length > 0 || converting > 0 || failedHere.length > 0 || failedJobs.length > 0) && (
    <div className="convert-ticked" data-testid="convert-ticked">
      {toConvert.length > 0 && (
        <p className="small">
          {toConvert.length} of the ticked spatial datasets {toConvert.length === 1 ? 'has' : 'have'} no converted copy yet (spatial
          views need one).{' '}
          <button className="link" onClick={() => convertMany(toConvert, qc)} data-testid="convert-ticked-button">
            Convert {toConvert.length === 1 ? 'it' : `all ${toConvert.length}`}
          </button>
        </p>
      )}
      {converting > 0 && (
        <p className="muted small" data-testid="convert-ticked-progress" role="status">
          Converting {converting}
          {waiting.length ? ` (${waiting.length} waiting for a place in the server’s queue)` : ''}; at most a few run at a time.
          {waiting.length > 0 && (
            <>
              {' '}
              <button className="link small" onClick={stopWaiting} data-testid="convert-ticked-stop">
                Stop waiting
              </button>
            </>
          )}
        </p>
      )}
      {failedHere.map((f) => (
        <p key={f.id} className="error small" role="status">
          {qc.getQueryData<{ name: string }>(['dataset', f.id])?.name ?? f.id}: {f.message}
        </p>
      ))}
      {failedJobs.map((d) => (
        <p key={d!.id} className="error small" role="status">
          {d!.name}: the conversion failed{d!.conversion?.job?.error ? ` (${d!.conversion.job.error})` : ''}. Its details say more; Convert tries
          again.
        </p>
      ))}
    </div>
  );
  if (anyOpen) {
    return (
      <div className="details linked-open" data-testid="linked-open">
        <h3>
          {ticked.length} dataset{ticked.length === 1 ? '' : 's'} ticked
        </h3>
        <CloseFirst testId="linked-blocked">Linked views open on an empty canvas: close the open views first.</CloseFirst>
        {conversions}
        <button className="link small" onClick={clearTicks} data-testid="untick-all">
          Clear the ticks
        </button>
      </div>
    );
  }
  const tooMany = ticked.length > MAX_GROUP;
  return (
    <div className="details linked-open" data-testid="linked-open">
      <h3>
        {ticked.length} dataset{ticked.length === 1 ? '' : 's'} ticked
        <Help label="About linked views">
          <p>
            Ticked datasets open together as <b>linked views</b>: one tile with a view per dataset, panned and zoomed
            together at one scale, with one legend. Their settings are shared: a change in one (colour, palette, point
            size…) applies to all. Only the embedding is chosen per dataset (its name can differ).
          </p>
          <p>
            A dataset without the chosen column or gene shows its cells as NA (grey). When the datasets bring their own
            colours and these disagree, palette colours are used for all of them.
          </p>
        </Help>
      </h3>
      <div className="open-row">
        {allSpatial && (
          <button className="primary" onClick={() => void open('spatial')} disabled={busy || ticked.length < 2 || tooMany} data-testid="open-linked-spatial">
            {busy ? 'Opening…' : 'Open linked spatial views'}
          </button>
        )}
        <button
          className={allSpatial ? 'ghost' : 'primary'}
          onClick={() => void open('embedding')}
          disabled={busy || ticked.length < 2 || tooMany}
          data-testid="open-linked"
        >
          {busy && !allSpatial ? 'Opening…' : 'Open linked embeddings'}
        </button>
        <button className="ghost" onClick={clearTicks} data-testid="untick-all">
          Clear
        </button>
      </div>
      {conversions}
      {ticked.length < 2 && <p className="muted small">Tick at least one more dataset to open them linked.</p>}
      {tooMany && <p className="error small">At most {MAX_GROUP} datasets can be linked.</p>}
      {problem && <p className="error small">{problem}</p>}
    </div>
  );
}

// The lists are plain lists of buttons: tree roles would promise arrow-key navigation (review
// full-2 C-L1).
export function FileBrowser() {
  const roots = useRoots();
  const [query, setQuery] = useState('');
  const [q, setQ] = useState('');
  useEffect(() => {
    const t = setTimeout(() => setQ(query.trim()), 150); // one request per pause, not per key
    return () => clearTimeout(t);
  }, [query]);
  const search = useSearch(q);
  const found = (search.data ?? []).filter((d) => d.state === 'inspected').map((d) => d.id);
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
          <ul className="tree" aria-label="Search results" data-testid="search-results">
            {search.data?.length === 0 && <li className="muted small pad">No matches.</li>}
            {search.data?.map((d) => <DatasetRow key={d.id} d={d} depth={0} list={found} />)}
            {search.data?.length === 100 && (
              <li className="muted small pad">Showing the first 100 matches; type more to narrow down.</li>
            )}
          </ul>
        ) : (
          <ul className="tree" aria-label="Data folders">
            {roots.error && <li className="error small pad">{String(roots.error.message)}</li>}
            {roots.data?.length === 0 && <li className="muted small pad">No data roots configured.</li>}
            {roots.data?.map((r) => (
              <Folder key={r.name} root={r.name} path="" name={r.name} depth={0} count={r.n_datasets} />
            ))}
          </ul>
        )}
      </div>
      <LinkedOpen />
      {selected && <Details key={selected} id={selected} />}
    </aside>
  );
}
