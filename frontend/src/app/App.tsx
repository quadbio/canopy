// Application shell: header, file browser (left), the canvas (centre), the selected view's
// settings (right).

import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

import { catalog } from '../api/endpoints';
import { metaQuery, useCatalogStatus } from '../api/queries';
import { useWorkspace, type ViewSpec } from '../state/workspace';
import { event } from '../telemetry/telemetry';
import { FileBrowser } from '../ui/FileBrowser';
import { JobWatcher } from './JobWatcher';
import { Canvas } from '../views/Canvas';
import { viewType } from '../views/registry';

type Theme = 'system' | 'light' | 'dark';

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem('canopy-theme') as Theme) || 'system';
    } catch {
      return 'system';
    }
  });
  useEffect(() => {
    if (theme === 'system') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', theme);
    try {
      localStorage.setItem('canopy-theme', theme);
    } catch {
      // storage unavailable: the choice lasts for this page only
    }
  }, [theme]);
  const next: Record<Theme, Theme> = { system: 'light', light: 'dark', dark: 'system' };
  return [theme, () => setTheme(next[theme])];
}

function CatalogStatus() {
  const status = useCatalogStatus();
  const qc = useQueryClient();
  const scanning = status.data?.scanning;
  const last = status.data?.last_scan;
  const seen = useRef<number | null | undefined>(undefined);
  useEffect(() => {
    // Reload the listings when a scan finishes, not when the status first arrives.
    if (scanning || last === undefined) return;
    if (seen.current !== undefined && seen.current !== (last?.id ?? null)) {
      void qc.invalidateQueries({ predicate: (q) => ['roots', 'folder', 'search'].includes(String(q.queryKey[0])) });
    }
    seen.current = last?.id ?? null;
  }, [scanning, last, qc]);
  const when = last?.finished ? new Date(last.finished * 1000).toLocaleTimeString() : null;
  return (
    <div className="catalog-status">
      <span className="muted small">{scanning ? 'Scanning…' : when ? `Indexed ${when}` : ''}</span>
      <button className="ghost" onClick={() => void catalog.refresh().then(() => status.refetch())} disabled={scanning}>
        Refresh
      </button>
    </div>
  );
}

function SettingsPanel() {
  const spec = useWorkspace((s) => s.views.find((v) => v.id === s.selected) ?? null);
  const t = spec ? viewType(spec.type) : undefined;
  return (
    <aside className="panel" aria-label="View settings">
      {spec && t ? (
        <>
          <h3 className="panel-title" title={spec.title}>
            {spec.title}
          </h3>
          <p className="muted small">{t.label}</p>
          <t.Panel key={spec.id} spec={spec} />
          <ResetView key={`reset-${spec.id}`} spec={spec} />
        </>
      ) : (
        <p className="muted small pad">Select a view to change its settings.</p>
      )}
    </aside>
  );
}

/** Back to the view type's defaults for this dataset (the remembered settings are replaced too). */
function ResetView({ spec }: { spec: ViewSpec }) {
  const qc = useQueryClient();
  const setParams = useWorkspace((s) => s.setParams);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const reset = async () => {
    const t = viewType(spec.type);
    if (!t) return;
    setBusy(true);
    setFailed(null);
    try {
      const meta = await qc.fetchQuery(metaQuery(spec.datasetId, spec.table));
      const params = t.create(meta);
      if (params) {
        setParams(spec.id, params as Record<string, unknown>);
        event('view_reset', null, { type: spec.type });
      }
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="panel-footer">
      <button className="ghost" onClick={() => void reset()} disabled={busy} data-testid="reset-view">
        Reset to defaults
      </button>
      {failed && <p className="error small">{failed}</p>}
    </div>
  );
}

function ResetWorkspace() {
  const reset = useWorkspace((s) => s.reset);
  const n = useWorkspace((s) => s.views.length);
  return (
    <button className="ghost" onClick={reset} disabled={!n} title="Close all views and start empty">
      Close all
    </button>
  );
}

export function App() {
  const [theme, cycleTheme] = useTheme();
  return (
    <div className="app">
      <header className="header">
        <img className="brand-logo" src="/canopy-64.png" srcSet="/canopy-64.png 1x, /canopy-128.png 2x" alt="" width={28} height={28} />
        <span className="brand">Canopy</span>
        <span className="muted small">single-cell and spatial data browser</span>
        <div className="spacer" />
        <CatalogStatus />
        <ResetWorkspace />
        <button className="ghost" onClick={cycleTheme} title="Theme: system, light or dark">
          Theme: {theme === 'system' ? 'auto' : theme}
        </button>
      </header>
      <JobWatcher />
      <FileBrowser />
      <main className="main">
        <Canvas />
      </main>
      <SettingsPanel />
    </div>
  );
}
