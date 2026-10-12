// Converting a dataset: the button, the progress of the job, cancel and retry. Used in the
// dataset details and wherever a view needs a converted dataset (e.g. gene expression).
// The state shown always comes from the server (the dataset's `conversion` and its latest job);
// the app-wide job watcher (JobWatcher) refreshes everything when a job ends.

import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import { ApiError } from '../api/client';
import { jobs } from '../api/endpoints';
import { useDatasetDetail, useJob } from '../api/queries';
import type { Job } from '../api/types';
import { event } from '../telemetry/telemetry';
import { Help } from './controls';

const STAGES: [string, string][] = [
  ['read', 'Reading cells and genes'],
  ['obsm', 'Copying embeddings'],
  ['count', 'Counting values per gene'],
  ['reorder', 'Reordering by gene'],
  ['write', 'Writing gene by gene'],
  ['finish', 'Checking and finishing'],
];
// SpatialData stores: the tables (as above), then each kind of element.
const SPATIAL_STAGES: [string, string][] = [
  ['tables', 'Converting the tables'],
  ['images', 'Building image zoom levels'],
  ['labels', 'Copying segmentation masks'],
  ['shapes', 'Sorting cell outlines'],
  ['points', 'Sorting transcripts by gene'],
  ['finish', 'Checking and finishing'],
];

const ERRORS: Record<string, string> = {
  cancelled: 'Cancelled.',
  interrupted: 'Interrupted: the conversion process ended unexpectedly.',
  no_heartbeat: 'Interrupted: the conversion process stopped responding.',
  server_stopped: 'Interrupted by a server restart; it will run again.',
  memory_limit: 'Stopped: it needed more memory than allowed (jobs.memory_gb in the server settings).',
  not_enough_disk: 'Not enough free disk space on the server for the converted copy.',
  not_enough_memory: 'It needs more memory than this server allows (memory.budget_gb) or has; the server log has the numbers.',
  stores_full: 'The converted copies would exceed jobs.stores_max_gb: remove old ones (canopy cache prune) or raise the limit.',
  feature_key_ambiguous: 'Could not tell which transcript column holds the genes: name it below and convert again.',
  feature_key_missing: 'The gene column you named is not in the transcripts.',
  manifest_not_finite: 'The data hold values that cannot be stored (NaN); please report this.',
  source_changed: 'The file changed while it was being converted; refresh the index and try again.',
  duplicate_entries: 'The matrix stores some cells twice for the same gene; it cannot be converted.',
  legacy_layout: 'Files written by anndata before 0.8 cannot be converted yet.',
  store_invalid: 'The converted copy failed its checks; please report this (the server log has details).',
};

export function describeError(code: string | null): string {
  if (!code) return 'Failed.';
  if (ERRORS[code]) return ERRORS[code];
  if (code === 'worker_killed:oom') return 'The system stopped the conversion: it ran out of memory (lower jobs.memory_gb or free memory).';
  if (code.startsWith('worker_killed')) return 'The conversion process was stopped by the system (out of memory?).';
  return `Failed (${code}).`;
}

const ACTIVE = new Set(['queued', 'running']);
const gb = (b: unknown) => (typeof b === 'number' ? `${(b / 2 ** 30).toFixed(1)} GB` : '?');

function elapsed(from: number | null): string {
  if (!from) return '';
  const s = Math.max(0, Math.round(Date.now() / 1000 - from));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

function Progress({ job, onCancel, spatial }: { job: Job; onCancel: () => void; spatial: boolean }) {
  const pct = Math.round(job.progress * 100);
  const stages = spatial ? SPATIAL_STAGES : STAGES;
  const step = stages.findIndex(([k]) => k === job.stage);
  let line: string;
  if (job.stage === 'waiting_for_memory') {
    line = `Waiting for free memory on the server (needs ${gb(job.info?.need_bytes)}, ${gb(job.info?.available_bytes)} free)`;
    // after a few minutes: it may never fit while other programs hold the memory
    if (job.created && Date.now() / 1000 - job.created > 180) {
      line += '. It may not fit while other work runs: cancel and lower jobs.memory_gb, or free memory on the server.';
    }
  } else if (job.state === 'queued') {
    line = 'Queued…';
  } else if (step >= 0) {
    line = `Step ${step + 1} of ${stages.length}: ${stages[step][1]} · ${pct}% · ${elapsed(job.started)}`;
  } else {
    line = `Starting… · ${elapsed(job.started)}`;
  }
  return (
    <div className="conversion" data-testid="conversion">
      <div className="small" role="status">
        {line}
      </div>
      <div className="progress" role="progressbar" aria-label="Conversion progress" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
        <div className="progress-bar" style={{ width: `${pct}%` }} />
      </div>
      <button className="link" onClick={onCancel}>
        Cancel
      </button>
    </div>
  );
}

export function Conversion({ datasetId, layers = [], compact = false }: { datasetId: string; layers?: string[]; compact?: boolean }) {
  const detail = useDatasetDetail(datasetId);
  const qc = useQueryClient();
  const known = detail.data?.conversion?.job ?? null;
  const job = useJob(known?.id ?? null, known);
  const [xSource, setXSource] = useState('X');
  const [featureKey, setFeatureKey] = useState('');
  const [problem, setProblem] = useState<string | null>(null);
  const [, tick] = useState(0);
  const j: Job | null = job.data ?? null;
  const active = !!j && ACTIVE.has(j.state);
  const conv = detail.data?.conversion;
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => tick((x) => x + 1), 1000); // elapsed time
    return () => clearInterval(t);
  }, [active]);

  const start = async () => {
    setProblem(null);
    try {
      const started = await jobs.convert(datasetId, xSource, !!conv?.converted, featureKey.trim() || undefined);
      qc.setQueryData(['job', started.id], started);
      await qc.invalidateQueries({ queryKey: ['dataset', datasetId] });
      await qc.invalidateQueries({ queryKey: ['jobs-active'] });
      event('conversion_started', null, { x_source: xSource === 'X' ? 'X' : 'layer' });
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  };
  const cancel = async () => {
    if (!j) return;
    try {
      qc.setQueryData(['job', j.id], await jobs.cancel(j.id));
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : String(e));
    }
  };

  if (!detail.data || !conv) return null;
  const kind = detail.data.kind;
  if (kind !== 'h5ad' && kind !== 'anndata_zarr' && kind !== 'spatialdata_zarr') return null;

  if (active && j) return <Progress job={j} onCancel={() => void cancel()} spatial={kind === 'spatialdata_zarr'} />;
  if (conv.converted) {
    const from = conv.x_source && conv.x_source !== 'X' ? ` (values from ${conv.x_source.replace(/^layers\//, '')})` : '';
    return (
      <p className="small conversion done" data-testid="conversion">
        ✓ Converted{from}: genes load directly.
      </p>
    );
  }
  const failed = j && j.version === detail.data.version && (j.state === 'failed' || j.state === 'interrupted' || j.state === 'cancelled');
  return (
    <div className="conversion" data-testid="conversion">
      {failed && <p className="small error">{describeError(j.error ?? j.state)}</p>}
      {failed && (j.error === 'feature_key_ambiguous' || j.error === 'feature_key_missing') && (
        <label className="field">
          <span className="field-label">Transcript column with the gene names</span>
          <input
            className="text-input"
            value={featureKey}
            placeholder="e.g. gene or target"
            onChange={(e) => setFeatureKey(e.target.value)}
            data-testid="feature-key"
          />
        </label>
      )}
      {!compact && layers.length > 0 && (
        <label className="field">
          <span className="field-label">Expression values to convert</span>
          <select className="native-select" value={xSource} onChange={(e) => setXSource(e.target.value)}>
            <option value="X">X</option>
            {layers.map((l) => (
              <option key={l} value={`layers/${l}`}>
                {l}
              </option>
            ))}
          </select>
        </label>
      )}
      <div className="conversion-row">
        <button className="ghost" onClick={() => void start()} data-testid="convert">
          {failed ? 'Try again' : 'Convert for full speed'}
        </button>
        <Help label="About converting">
          <p>
            Makes a copy of this dataset in the server’s cache, stored gene by gene, so every gene loads in milliseconds
            whatever the file size. The original file is not changed.
          </p>
          <p>
            It runs in the background (minutes for large files); you can keep working, also on other datasets. The copy
            holds one expression matrix (X, or the layer you choose); the other layers stay available as before.
          </p>
          {kind === 'spatialdata_zarr' && (
            <p>
              For spatial data it also prepares the images (zoom levels), cell outlines and transcripts, which the
              spatial view needs.
            </p>
          )}
        </Help>
      </div>
      {problem && <p className="small error">{problem}</p>}
    </div>
  );
}
