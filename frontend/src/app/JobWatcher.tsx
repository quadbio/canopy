// Follows the server's active jobs for the whole app (polling while any runs). When a job ends,
// everything read from its dataset is fetched again, whichever widgets are on screen: after a
// conversion the views switch to the converted copy without a reload.

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';

import { jobs } from '../api/endpoints';
import { event } from '../telemetry/telemetry';

const PER_DATASET = ['dataset', 'meta', 'genes', 'gene', 'subset', 'obs', 'obs-labels'];

export function JobWatcher() {
  const qc = useQueryClient();
  const active = useQuery({
    queryKey: ['jobs-active'],
    queryFn: jobs.active,
    refetchInterval: (q) => (q.state.data?.length ? 1000 : 10_000),
  });
  const seen = useRef<Map<number, string>>(new Map());
  useEffect(() => {
    const now = new Map((active.data ?? []).map((j) => [j.id, j.dataset]));
    for (const [id, dataset] of seen.current) {
      if (!now.has(id)) {
        event('job_ended_seen', null, { job: id });
        void qc.invalidateQueries({ queryKey: ['job', id] });
        void qc.invalidateQueries({ predicate: (q) => PER_DATASET.includes(String(q.queryKey[0])) && q.queryKey[1] === dataset });
      }
    }
    seen.current = now;
  }, [active.data, qc]);
  return null;
}
