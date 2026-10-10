// Application shell: header, file browser (left), the canvas (centre), the selected view's
// settings (right).

import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import { catalog } from '../api/endpoints';
import { useCatalogStatus } from '../api/queries';
import { useWorkspace } from '../state/workspace';
import { FileBrowser } from '../ui/FileBrowser';
import { Canvas } from '../views/Canvas';
import { viewType } from '../views/registry';
import '../views/embedding';

type Theme = 'system' | 'light' | 'dark';

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem('dbx-theme') as Theme) || 'system';
    } catch {
      return 'system';
    }
  });
  useEffect(() => {
    if (theme === 'system') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', theme);
    try {
      localStorage.setItem('dbx-theme', theme);
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
  useEffect(() => {
    if (!scanning) void qc.invalidateQueries({ predicate: (q) => ['roots', 'folder', 'search'].includes(String(q.queryKey[0])) });
  }, [scanning, last?.id, qc]);
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
          <t.Panel spec={spec} />
        </>
      ) : (
        <p className="muted small pad">Select a view to change its settings.</p>
      )}
    </aside>
  );
}

export function App() {
  const [theme, cycleTheme] = useTheme();
  return (
    <div className="app">
      <header className="header">
        <span className="brand">dbx</span>
        <span className="muted small">data browser</span>
        <div className="spacer" />
        <CatalogStatus />
        <button className="ghost" onClick={cycleTheme} title="Theme: system, light or dark">
          Theme: {theme === 'system' ? 'auto' : theme}
        </button>
      </header>
      <FileBrowser />
      <main className="main">
        <Canvas />
      </main>
      <SettingsPanel />
    </div>
  );
}
