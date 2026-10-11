// Converting a dataset: the button, the progress of the job, cancel and retry. Used in the
// dataset details and wherever a view needs a converted dataset (e.g. gene expression).

import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/client';
import { jobs } from '../api/endpoints';
import { useDatasetDetail, useJob } from '../api/queries';
import type { Job } from '../api/types';
import { event } from '../telemetry/telemetry';
import { Help } from './controls';

const STAGES: Record<string, string> = {
  read: 'Reading cells and genes',
  obsm: 'Copying embeddings',
  count: 'Counting values per gene',
  reorder: 'Reordering by gene',
  write: 'Writing gene by gene',
  finish: 'Checking and finishing',
  waiting_for_memory: 'Waiting for free memory on the server',
};

const ERRORS: Record<string, string> = {
  cancelled: 'Cancelled.',
  interrupted: 'Interrupted (the server restarted).',
  memory_limit: 'Stopped: it needed more memory than allowed (jobs.memory_gb).',
  duplicate_entries: 'The matrix stores some cells twice for the same gene; it cannot be converted.',
  legacy_layout: 'Files written by anndata before 0.8 cannot be converted yet.',
  store_invalid: 'The converted copy failed its checks; please report this (the server log has details).',
};

export function describeError(code: string | null): string {
  if (!code) return 'Failed.';
  if (ERRORS[code]) return ERRORS[code];
  if (code.startsWith('worker_killed')) return 'The conversion process was stopped by the system (out of memory?).';
  return `Failed (${code}).`;
}

const ACTIVE = new Set(['queued', 'running']);

export function Conversion({ datasetId, layers = [], compact = false }: { datasetId: string; layers?: string[]; compact?: boolean }) {
  const detail = useDatasetDetail(datasetId);
  const qc = useQueryClient();
  const known = detail.data?.conversion?.job ?? null;
  const [jobId, setJobId] = useState<number | null>(null);
  const job = useJob(jobId ?? known?.id ?? null, jobId === null ? known : null);
  const [xSource, setXSource] = useState('X');
  const [problem, setProblem] = useState<string | null>(null);
  const j: Job | null = job.data ?? null;
  const active = !!j && ACTIVE.has(j.state);
  const converted = !!detail.data?.conversion?.converted || j?.state === 'done';

  // When the job finishes, everything read from this dataset is fetched again (now from the
  // converted copy: genes load directly).
  const wasActive = useRef(false);
  useEffect(() => {
    if (active) wasActive.current = true;
    else if (wasActive.current && j) {
      wasActive.current = false;
      event('conversion_finished', null, { state: j.state });
      for (const key of ['dataset', 'meta', 'genes', 'gene']) {
        void qc.invalidateQueries({ predicate: (q) => q.queryKey[0] === key && q.queryKey[1] === datasetId });
      }
    }
  }, [active, j, qc, datasetId]);

  const start = async () => {
    setProblem(null);
    try {
      const started = await jobs.convert(datasetId, xSource);
      setJobId(started.id);
      qc.setQueryData(['job', started.id], started);
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

  if (!detail.data) return null;
  const kind = detail.data.kind;
  if (kind !== 'h5ad' && kind !== 'anndata_zarr') return null;

  if (converted && !active) {
    return (
      <p className="small conversion done" data-testid="conversion">
        ✓ Converted: genes load directly.
      </p>
    );
  }
  if (active && j) {
    const pct = Math.round(j.progress * 100);
    return (
      <div className="conversion" data-testid="conversion">
        <div className="small">
          {j.state === 'queued' && j.stage !== 'waiting_for_memory' ? 'Queued…' : (STAGES[j.stage ?? ''] ?? 'Converting…')}
          {j.state === 'running' && ` · ${pct}%`}
        </div>
        <div className="progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
          <div className="progress-bar" style={{ width: `${pct}%` }} />
        </div>
        <button className="link" onClick={() => void cancel()}>
          Cancel
        </button>
      </div>
    );
  }
  const failed = j && (j.state === 'failed' || j.state === 'interrupted' || j.state === 'cancelled');
  return (
    <div className="conversion" data-testid="conversion">
      {failed && <p className="small error">{describeError(j.error ?? j.state)}</p>}
      {!compact && layers.length > 0 && (
        <label className="field">
          <span className="field-label">Expression values</span>
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
            It runs in the background (minutes for large files); you can keep working. The copy holds one expression
            matrix: X, or the layer you choose.
          </p>
        </Help>
      </div>
      {problem && <p className="small error">{problem}</p>}
    </div>
  );
}
